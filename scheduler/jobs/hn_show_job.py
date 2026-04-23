"""
hn_show_job — fetch Show HN launches via the Algolia HN Search API,
stream them as JSONL into data/ideas/, then load into company_ideas.

Incremental: reads MAX(raw_json.created_at_i) from existing Show HN rows
and fetches strictly-newer posts. Falls back to an N-day window on first run.

Idempotent: UNIQUE(program, source_url) on company_ideas absorbs overlap,
so it is safe to re-run with an overlapping window.

Usage:
    python3 -m scheduler.jobs.hn_show_job
    python3 -m scheduler.jobs.hn_show_job --since-days 7
    python3 -m scheduler.jobs.hn_show_job --no-load   # just write JSONL
    python3 -m scheduler.jobs.hn_show_job --db custom.db
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from persistence.database import Database
from sources.accelerator_scraper import CompanyIdea, save_company_ideas
from sources.hn_show_scraper import ShowHNScraper

logger = logging.getLogger(__name__)


def _last_seen_ts(db: Database) -> Optional[int]:
    """Max created_at_i across Show HN rows already in company_ideas.

    Returns None if the table doesn't exist yet (first run) or has no
    Show HN rows.
    """
    cur = db.conn.cursor()
    try:
        row = cur.execute(
            """
            SELECT MAX(CAST(json_extract(raw_json, '$.created_at_i') AS INTEGER)) AS ts
              FROM company_ideas
             WHERE program = 'Show HN'
            """
        ).fetchone()
    except Exception as e:  # noqa: BLE001
        logger.debug("last_seen_ts skipped (%s)", e)
        return None
    if not row:
        return None
    ts = row["ts"] if hasattr(row, "keys") else row[0]
    return int(ts) if ts else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--outdir",
        default="data/ideas",
        help="Directory for JSONL output (default: data/ideas)",
    )
    p.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Initial lookback window on first run (default: 30 days)",
    )
    p.add_argument(
        "--max", type=int, default=None, help="Cap records (default: unlimited)"
    )
    p.add_argument(
        "--no-load",
        action="store_true",
        help="Write JSONL only; skip the DB load step",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(args.db)
    try:
        last_ts = _last_seen_ts(db)
        if last_ts:
            since_ts = last_ts
            logger.info(
                "Show HN: resuming from last_seen_ts=%d (%s UTC)",
                since_ts,
                datetime.fromtimestamp(since_ts, timezone.utc).isoformat(),
            )
        else:
            since_ts = int(
                (
                    datetime.now(timezone.utc)
                    - timedelta(days=args.since_days)
                ).timestamp()
            )
            logger.info(
                "Show HN: first run, fetching last %d days (since %s UTC)",
                args.since_days,
                datetime.fromtimestamp(since_ts, timezone.utc).isoformat(),
            )

        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jsonl_path = outdir / f"ideas_hn_show_{stamp}.jsonl"

        scraper = ShowHNScraper(max_records=args.max)

        with jsonl_path.open("a", buffering=1) as fh:

            def _write(rec: CompanyIdea):
                d = asdict(rec)
                d["scraped_at"] = rec.scraped_at.isoformat()
                fh.write(
                    json.dumps(d, default=str, ensure_ascii=False) + "\n"
                )

            records = scraper.scrape(on_record=_write, since_ts=since_ts)

        logger.info(
            "Show HN: %d records written to %s", len(records), jsonl_path
        )

        if args.no_load:
            return

        inserted = save_company_ideas(db, records)
        logger.info(
            "Show HN: %d new rows inserted (rest were duplicates)", inserted
        )
    finally:
        if hasattr(db, "close"):
            db.close()
        else:
            db.conn.close()


if __name__ == "__main__":
    main()
