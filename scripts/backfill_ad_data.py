#!/usr/bin/env python3
"""
Backfill Stammkapital / Gegenstand / Geschäftsanschrift for companies that
were discovered before AD (Abdruck) capture was wired into the pipeline.

Strategy
--------
For each company missing `capital_amount`:
  1. Search Handelsregister by native_company_number + registry_court
  2. If found, click AD → get PDF → parse → update the row
  3. Respect the shared scheduler rate limiter (60 req/hr)

Design
------
This is a LONG-RUNNING script. At 60 req/hr × 2 req/company (search + AD
click) we backfill ~30 companies per hour — ~250 per day. For 7,300
missing rows that's about 4 weeks of continuous running. Configure runtime
with --max-companies and --max-hours.

Safe to interrupt: uses resume-from-last-processed-id via a checkpoint
file (default: data/ad_backfill_state.json).

Usage
-----
    # One-off test run
    python3 scripts/backfill_ad_data.py --max-companies 10

    # Overnight run, cap at 8 hours
    python3 scripts/backfill_ad_data.py --max-hours 8

    # Priority: registration_scan rows first (most recent, highest value)
    python3 scripts/backfill_ad_data.py --source registration_scan --max-hours 4

    # Resume from checkpoint
    python3 scripts/backfill_ad_data.py --max-hours 8   # picks up where it left off

Install deps locally first:
    pip3 install pypdf --user
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.database import Database
from processing.ad_capture import capture_ad_for_company
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import BundesAPISource

logger = logging.getLogger(__name__)


# Berlin Amtsgericht Charlottenburg / München. Other courts need mapping too —
# we infer from the native_company_number prefix + existing `state` column.
COURT_MAP = {
    "F1103": "F1103",  # Berlin Charlottenburg
    "D2601": "D2601",  # München
}


def _load_state(path: str) -> dict:
    """
    Load backfill checkpoint.

    `next_id_ceiling` is the exclusive upper bound for the next candidate
    query — we fetch rows with `id < next_id_ceiling ORDER BY id DESC`, then
    after processing each row set `next_id_ceiling = row.id`. That way the
    cursor is strictly monotonically decreasing and we never re-select a
    row we've already handled in THIS run. Rows whose capital still isn't
    filled after this run will be picked up on the next run (they remain in
    the `capital_amount IS NULL` candidate set, but below the new ceiling).

    Note: this is a breaking change from the older `last_company_id` state
    files — see `_migrate_state` below for the transition logic.
    """
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            return _migrate_state(data)
        except Exception:
            pass
    return {
        "next_id_ceiling": None,  # None = start from MAX(id)+1 (newest-first)
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
    }


def _migrate_state(state: dict) -> dict:
    """Migrate older `last_company_id`-based state files to `next_id_ceiling`.

    The old script used `WHERE id > last_company_id ORDER BY id DESC` which
    re-selected the same batch every pass. Resetting `next_id_ceiling = None`
    restarts the backfill from the top on next run; counters are preserved.
    """
    if "next_id_ceiling" in state:
        return state
    logger.warning("Migrating old state file — resetting ceiling to MAX(id)+1")
    return {
        "next_id_ceiling": None,
        "processed": state.get("processed", 0),
        "succeeded": state.get("succeeded", 0),
        "failed": state.get("failed", 0),
    }


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def _select_candidates(
    db: Database, source_filter: str | None, limit: int, id_ceiling: int
) -> list[dict]:
    """
    Select companies missing capital_amount, newest-first.

    Uses `id < id_ceiling` (exclusive). Callers update id_ceiling to the
    LOWEST id they processed so far, which ensures monotonic descent through
    the keyspace — we never re-select a row we've already handled.

    Previous version used `id > last_id ORDER BY id DESC`, which re-selected
    the entire batch after processing because `last_id` got set to the min of
    a DESC-ordered batch.
    """
    q = """
        SELECT id, name, native_company_number, registry_court, city, source
        FROM companies
        WHERE capital_amount IS NULL
          AND native_company_number IS NOT NULL
          AND native_company_number != ''
          AND id < ?
    """
    args: list = [id_ceiling]
    if source_filter:
        q += " AND source = ?"
        args.append(source_filter)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)

    cursor = db.conn.cursor()
    cursor.execute(q, args)
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _extract_hrb_number(native_number: str) -> str | None:
    """Extract the numeric HRB from 'HRB 123456' / 'HRA 45678' etc."""
    if not native_number:
        return None
    m = re.search(r"\b(\d+)\b", native_number)
    return m.group(1) if m else None


def _infer_court_code(company: dict) -> str | None:
    """Map the stored registry_court string to the code used in searches."""
    court = (company.get("registry_court") or "").lower()
    if "berlin" in court or "charlottenburg" in court:
        return "F1103"
    if "münchen" in court or "munich" in court:
        return "D2601"
    # Fall back on city
    city = (company.get("city") or "").lower()
    if "berlin" in city:
        return "F1103"
    if "münchen" in city or "munich" in city:
        return "D2601"
    return None


def backfill_one(
    db: Database,
    source: BundesAPISource,
    rate_limiter: PersistentRateLimiter,
    company: dict,
) -> bool:
    """Backfill one company. Returns True if capital/purpose was captured."""
    hrb = _extract_hrb_number(company["native_company_number"])
    court = _infer_court_code(company)
    if not hrb or not court:
        logger.debug("%s: can't extract HRB / court", company["name"])
        return False

    # Search by HRB (1 request)
    if not rate_limiter.acquire(count=1, block=False):
        logger.warning("rate limit empty — skipping %s", company["name"])
        return False

    try:
        results = list(source.search(
            register_number=hrb,
            register_court=court,
            registry_types=["HRB"],
            max_results=1,
        ))
    except Exception as e:  # noqa: BLE001
        logger.debug("search failed for %s: %s", company["name"], e)
        return False

    if not results:
        logger.debug("%s: not found in search", company["name"])
        return False

    # AD PDF (1 request, gated by source's own rate limiter)
    try:
        captured = capture_ad_for_company(
            db, source, company["id"], results[0], rate_limiter=None,
        )
        return captured
    except Exception as e:  # noqa: BLE001
        logger.debug("AD capture failed for %s: %s", company["name"], e)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill AD PDF data (capital, purpose, address)")
    parser.add_argument("--db", default="handelsregister.db", help="SQLite path")
    parser.add_argument("--state-file", default="data/ad_backfill_state.json",
                        help="Resume checkpoint (default: data/ad_backfill_state.json)")
    parser.add_argument("--source", choices=["registration_scan", "bundesapi", "offeneregister", "news"],
                        help="Filter by source column")
    parser.add_argument("--max-companies", type=int, default=None,
                        help="Stop after N companies (default: no limit)")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="Stop after N hours (default: no limit)")
    parser.add_argument("--batch-size", type=int, default=30,
                        help="Candidates to fetch per DB query (default: 30)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(args.db)
    rate_limiter = PersistentRateLimiter(args.db)
    source = BundesAPISource()
    state = _load_state(args.state_file)

    # First run (or after migration): start from top of the id space
    if state["next_id_ceiling"] is None:
        cursor = db.conn.cursor()
        cursor.execute("SELECT MAX(id) FROM companies")
        max_id = cursor.fetchone()[0] or 0
        state["next_id_ceiling"] = max_id + 1

    logger.info("AD backfill starting. Source filter: %s | ceiling: %d",
                args.source or "any", state["next_id_ceiling"])

    deadline = time.time() + args.max_hours * 3600 if args.max_hours else None

    try:
        while True:
            if args.max_companies and state["processed"] >= args.max_companies:
                logger.info("Reached --max-companies limit")
                break
            if deadline and time.time() > deadline:
                logger.info("Reached --max-hours limit")
                break

            candidates = _select_candidates(
                db, args.source, args.batch_size, state["next_id_ceiling"]
            )
            if not candidates:
                logger.info("No more candidates below ceiling=%d", state["next_id_ceiling"])
                break

            for co in candidates:
                if args.max_companies and state["processed"] >= args.max_companies:
                    break
                if deadline and time.time() > deadline:
                    break

                logger.info("[%d] %s (%s, %s) id=%d",
                            state["processed"] + 1,
                            co["name"][:50],
                            co["native_company_number"],
                            co.get("source"),
                            co["id"])

                ok = backfill_one(db, source, rate_limiter, co)
                state["processed"] += 1
                state["succeeded" if ok else "failed"] += 1
                # Drop ceiling to this row's id — next query will only
                # return rows STRICTLY below this, guaranteeing progress
                # even if this row's capital wasn't updated (e.g., the PDF
                # didn't parse cleanly). The row stays in the DB candidate
                # set (capital still NULL) but the cursor has moved past.
                state["next_id_ceiling"] = co["id"]

                # Persist checkpoint every 10
                if state["processed"] % 10 == 0:
                    _save_state(args.state_file, state)

                # Rate limiter handles the 60/hr cap internally; a small jitter
                # between companies keeps the portal session healthy.
                time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        _save_state(args.state_file, state)
        db.close()

        logger.info("=" * 60)
        logger.info("BACKFILL SUMMARY")
        logger.info("  Processed: %d", state["processed"])
        logger.info("  Succeeded: %d (capital/purpose captured)", state["succeeded"])
        logger.info("  Failed:    %d", state["failed"])
        logger.info("  Checkpoint: %s (next_id_ceiling=%d)",
                    args.state_file, state["next_id_ceiling"])


if __name__ == "__main__":
    main()
