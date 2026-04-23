"""
reddit_ideas_job — fetch maker-focused subreddits via the public JSON endpoint,
stream results as JSONL into data/ideas/, then load into company_ideas.

Incremental: for each subreddit, reads MAX(raw_json.created_utc) from
existing rows with program='Reddit r/<sub>' and stops paging once it sees
an older post. Falls back to a full /new scan on first run.

Idempotent: UNIQUE(program, source_url) absorbs any overlap.

Usage:
    python3 -m scheduler.jobs.reddit_ideas_job
    python3 -m scheduler.jobs.reddit_ideas_job --subs SideProject microsaas
    python3 -m scheduler.jobs.reddit_ideas_job --no-load  # write JSONL only
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from persistence.database import Database
from sources.accelerator_scraper import CompanyIdea, save_company_ideas
from sources.reddit_ideas_scraper import (
    DEFAULT_SUBREDDITS,
    RedditIdeasScraper,
)

logger = logging.getLogger(__name__)


def _last_seen_ts_per_sub(db: Database, subs: tuple[str, ...]) -> dict:
    """Max created_utc per subreddit across existing company_ideas rows.

    Returns {} if the table doesn't exist yet. A single query with GROUP BY
    is cheaper than N queries.
    """
    cur = db.conn.cursor()
    try:
        rows = cur.execute(
            """
            SELECT program,
                   MAX(CAST(json_extract(raw_json, '$.created_utc') AS INTEGER)) AS ts
              FROM company_ideas
             WHERE program LIKE 'Reddit r/%'
             GROUP BY program
            """
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("last_seen_ts_per_sub skipped (%s)", e)
        return {}
    out: dict[str, int] = {}
    for row in rows:
        prog = row["program"] if hasattr(row, "keys") else row[0]
        ts = row["ts"] if hasattr(row, "keys") else row[1]
        if prog and ts:
            # "Reddit r/SideProject" -> "SideProject"
            if prog.startswith("Reddit r/"):
                out[prog[len("Reddit r/") :]] = int(ts)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--outdir", default="data/ideas")
    p.add_argument(
        "--subs",
        nargs="+",
        default=list(DEFAULT_SUBREDDITS),
        help=f"Subreddits (default: {' '.join(DEFAULT_SUBREDDITS)})",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max pages per subreddit on first run (100 posts/page)",
    )
    p.add_argument("--max", type=int, default=None, help="Cap total records")
    p.add_argument("--no-load", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(args.db)
    try:
        per_sub_ts = _last_seen_ts_per_sub(db, tuple(args.subs))
        if per_sub_ts:
            logger.info("Reddit: resuming per-sub from last_seen:")
            for sub, ts in per_sub_ts.items():
                logger.info(
                    "  r/%s  last=%d (%s UTC)",
                    sub,
                    ts,
                    datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                )
        else:
            logger.info("Reddit: first run for these subs — full /new scan")

        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jsonl_path = outdir / f"ideas_reddit_{stamp}.jsonl"

        all_records: list[CompanyIdea] = []
        with jsonl_path.open("a", buffering=1) as fh:

            def _write(rec: CompanyIdea):
                d = asdict(rec)
                d["scraped_at"] = rec.scraped_at.isoformat()
                fh.write(
                    json.dumps(d, default=str, ensure_ascii=False) + "\n"
                )

            # Run one scraper per subreddit so we can pass a per-sub
            # stop_before_ts. A single call with mixed stop points wouldn't
            # work, since the scraper uses a single `stop_before_ts` across
            # all subs.
            for sub in args.subs:
                stop_ts = per_sub_ts.get(sub)
                remaining = (
                    max(0, args.max - len(all_records)) if args.max else None
                )
                if remaining == 0:
                    break
                scraper = RedditIdeasScraper(
                    subreddits=(sub,),
                    max_records=remaining,
                )
                recs = scraper.scrape(
                    on_record=_write,
                    stop_before_ts=stop_ts,
                    max_pages_per_sub=args.max_pages,
                )
                all_records.extend(recs)

        logger.info(
            "Reddit: %d records written to %s",
            len(all_records),
            jsonl_path,
        )

        if args.no_load:
            return

        inserted = save_company_ideas(db, all_records)
        logger.info(
            "Reddit: %d new rows inserted (rest were duplicates)", inserted
        )
    finally:
        if hasattr(db, "close"):
            db.close()
        else:
            db.conn.close()


if __name__ == "__main__":
    main()
