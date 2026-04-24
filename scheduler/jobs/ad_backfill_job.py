"""
AD (Abdruck) Backfill Job — scheduled pipeline version of
`scripts/backfill_ad_data.py`.

Purpose
-------
`scripts/backfill_ad_data.py` was a standalone CLI intended for
SSH-tethered use. In practice Railway SSH sessions drop every 1–4 minutes,
so the tethered approach couldn't sustain continuous progress. This job
replaces it: runs inside the long-lived Railway service, processes a
small, rate-limit-aware batch every N minutes, and persists state in the
same `ad_backfill_state.json` file.

Design
------
- Monotonic ceiling-based pagination (`id < next_id_ceiling ORDER BY id
  DESC`), same as the CLI script — no re-selection, no lost rows.
- Rate limiter: blocks up to 5 min for a token per row (not 1h — the
  scheduler has a tight budget per run, and any unused budget rolls to the
  next run anyway).
- Per-run cap: `max_companies` bounds wallclock so the job doesn't hold
  scheduler threads indefinitely.
- Shared state file with the CLI, so you can alternate between a tethered
  run and scheduler runs without losing progress.

Usage (called by scheduler/scheduler.py on an interval trigger):

    from scheduler.jobs.ad_backfill_job import ADBackfillJob
    job = ADBackfillJob(db, rate_limiter, max_companies=5,
                       source_filter="registration_scan")
    stats = job.run()
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from persistence.database import Database
from processing.ad_capture import capture_ad_for_company
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import BundesAPISource

logger = logging.getLogger(__name__)

# Shared with scripts/backfill_ad_data.py — a single source of truth so the
# scheduler job and the manual CLI don't fight over the same keyspace.
DEFAULT_STATE_FILE = "/data/ad_backfill_state.json"


class ADBackfillJob:
    """
    Backfill Stammkapital / Gegenstand / Geschäftsanschrift by fetching
    and parsing the AD (Abdruck) PDF for companies where `capital_amount
    IS NULL`.

    Each call processes up to `max_companies` rows, respecting the shared
    rate limiter. State persists in a JSON file on the data volume so runs
    resume where the last one left off.
    """

    def __init__(
        self,
        db: Database,
        rate_limiter: PersistentRateLimiter,
        max_companies: int = 5,
        source_filter: Optional[str] = "registration_scan",
        state_file: str = DEFAULT_STATE_FILE,
        acquire_timeout: float = 300.0,  # 5 min max block for a token
    ):
        self.db = db
        self.rate_limiter = rate_limiter
        self.max_companies = max_companies
        self.source_filter = source_filter
        self.state_file = state_file
        self.acquire_timeout = acquire_timeout

        self._source: Optional[BundesAPISource] = None
        self._state = self._load_state()

    # ---------------------------------------------------------------- state
    def _load_state(self) -> Dict[str, Any]:
        """Load state; tolerate older CLI-produced state schemas."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                if "next_id_ceiling" not in data:
                    # migrate from older last_company_id schema
                    data = {
                        "next_id_ceiling": None,
                        "processed": data.get("processed", 0),
                        "succeeded": data.get("succeeded", 0),
                        "failed": data.get("failed", 0),
                    }
                return data
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not load state from %s: %s", self.state_file, e)
        return {
            "next_id_ceiling": None,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
        }

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not save state: %s", e)

    # ------------------------------------------------------------ selection
    def _select_candidates(self, id_ceiling: int, limit: int) -> List[Dict[str, Any]]:
        q = """
            SELECT id, name, native_company_number, registry_court, city, source
            FROM companies
            WHERE capital_amount IS NULL
              AND native_company_number IS NOT NULL
              AND native_company_number != ''
              AND id < ?
        """
        args: List[Any] = [id_ceiling]
        if self.source_filter:
            q += " AND source = ?"
            args.append(self.source_filter)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)

        cursor = self.db.conn.cursor()
        cursor.execute(q, args)
        cols = [c[0] for c in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------ per-company
    @staticmethod
    def _extract_hrb(native_number: str) -> Optional[str]:
        if not native_number:
            return None
        m = re.search(r"\b(\d+)\b", native_number)
        return m.group(1) if m else None

    @staticmethod
    def _infer_court_code(company: Dict[str, Any]) -> Optional[str]:
        court = (company.get("registry_court") or "").lower()
        city = (company.get("city") or "").lower()
        if "berlin" in court or "charlottenburg" in court or "berlin" in city:
            return "F1103"
        if "münchen" in court or "munich" in court or "münchen" in city or "munich" in city:
            return "D2601"
        return None

    def _backfill_one(self, company: Dict[str, Any]) -> bool:
        hrb = self._extract_hrb(company["native_company_number"])
        court = self._infer_court_code(company)
        if not hrb or not court:
            logger.debug("%s: missing HRB/court; skip", company["name"])
            return False

        if not self.rate_limiter.acquire(count=1, block=True, timeout=self.acquire_timeout):
            logger.warning("AD backfill: rate limit wait timed out; skipping %s",
                           company["name"])
            return False

        try:
            results = list(self._source.search(
                register_number=hrb,
                register_court=court,
                registry_types=["HRB"],
                max_results=1,
            ))
        except Exception as e:  # noqa: BLE001
            logger.debug("AD backfill search failed for %s: %s", company["name"], e)
            return False

        if not results:
            return False

        try:
            return capture_ad_for_company(
                self.db, self._source, company["id"], results[0], rate_limiter=None,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("AD capture failed for %s: %s", company["name"], e)
            return False

    # --------------------------------------------------------------- run
    def run(self) -> Dict[str, Any]:
        """
        Process up to `max_companies` rows. Returns stats dict — the same
        fields as `scheduler/jobs/backfill_job.run()` so `_log_job_completion`
        works.
        """
        self._source = BundesAPISource()

        # Initialise ceiling on first ever run
        if self._state["next_id_ceiling"] is None:
            cursor = self.db.conn.cursor()
            cursor.execute("SELECT MAX(id) FROM companies")
            max_id = cursor.fetchone()[0] or 0
            self._state["next_id_ceiling"] = max_id + 1

        run_stats = {
            "companies_found": 0,  # for scheduler logging compat
            "companies_new": 0,
            "progress_percent": 0.0,
            "ad_processed": 0,
            "ad_succeeded": 0,
            "ad_failed": 0,
            "ad_ceiling_end": self._state["next_id_ceiling"],
        }

        logger.info(
            "AD backfill job run: source=%s, max=%d, ceiling=%d",
            self.source_filter or "any",
            self.max_companies,
            self._state["next_id_ceiling"],
        )

        candidates = self._select_candidates(
            self._state["next_id_ceiling"], self.max_companies
        )
        if not candidates:
            logger.info("AD backfill: no candidates below ceiling=%d",
                        self._state["next_id_ceiling"])
            return run_stats

        for co in candidates:
            logger.info("AD backfill [%d] %s (%s) id=%d",
                        self._state["processed"] + 1,
                        co["name"][:50],
                        co["native_company_number"],
                        co["id"])

            ok = self._backfill_one(co)
            self._state["processed"] += 1
            run_stats["ad_processed"] += 1
            if ok:
                self._state["succeeded"] += 1
                run_stats["ad_succeeded"] += 1
                run_stats["companies_new"] += 1
                run_stats["companies_found"] += 1
            else:
                self._state["failed"] += 1
                run_stats["ad_failed"] += 1

            # Monotonic descent — drop the ceiling regardless of success,
            # so a failing row doesn't permanently block the backfill.
            self._state["next_id_ceiling"] = co["id"]
            run_stats["ad_ceiling_end"] = co["id"]

            # Small inter-company jitter (keeps the portal session healthy)
            time.sleep(0.5)

        self._save_state()

        logger.info(
            "AD backfill job done: %d/%d succeeded this run; "
            "total succeeded=%d / processed=%d",
            run_stats["ad_succeeded"],
            run_stats["ad_processed"],
            self._state["succeeded"],
            self._state["processed"],
        )
        return run_stats


def run_ad_backfill_job(
    db_path: str,
    max_companies: int = 5,
    source_filter: Optional[str] = "registration_scan",
    state_file: str = DEFAULT_STATE_FILE,
) -> Dict[str, Any]:
    """Convenience for standalone / manual invocation."""
    db = Database(db_path)
    rate_limiter = PersistentRateLimiter(db_path)
    try:
        job = ADBackfillJob(
            db=db,
            rate_limiter=rate_limiter,
            max_companies=max_companies,
            source_filter=source_filter,
            state_file=state_file,
        )
        return job.run()
    finally:
        db.close()
