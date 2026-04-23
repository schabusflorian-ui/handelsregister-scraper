"""
idea_loader_job — load streaming JSONL files produced by the accelerator
scrapers into SQLite's `company_ideas` table, with normalization applied.

Idempotent: re-running picks up new rows (UNIQUE(program, source_url)) and
skips duplicates. Re-run freely while YC is still scraping.

Usage:
    python3 -m scheduler.jobs.idea_loader_job
    python3 -m scheduler.jobs.idea_loader_job --glob 'data/ideas/ideas_*.jsonl'
    python3 -m scheduler.jobs.idea_loader_job --db custom.db
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import List

from persistence.database import Database
from sources.accelerator_scraper import (
    CompanyIdea,
    save_company_ideas,
)

logger = logging.getLogger(__name__)


def _parse_line(line: str) -> CompanyIdea:
    d = json.loads(line)
    scraped = d.get("scraped_at")
    if isinstance(scraped, str):
        try:
            scraped = datetime.fromisoformat(scraped)
        except ValueError:
            scraped = datetime.now()
    return CompanyIdea(
        program=d.get("program", ""),
        company=d.get("company") or "",
        one_liner=d.get("one_liner"),
        long_description=d.get("long_description"),
        tags=list(d.get("tags") or []),
        company_website=d.get("company_website"),
        batch=d.get("batch"),
        country=d.get("country"),
        year_founded=d.get("year_founded"),
        team_size=d.get("team_size"),
        status=d.get("status"),
        source_url=d.get("source_url", ""),
        raw=d.get("raw") or {},
        scraped_at=scraped or datetime.now(),
    )


def load_jsonl(path: Path) -> List[CompanyIdea]:
    out: List[CompanyIdea] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_parse_line(line))
            except Exception as e:  # noqa: BLE001
                logger.warning("bad line in %s: %s", path, e)
    return out


def report(db: Database) -> None:
    cur = db.conn.cursor()

    print("\n=== company_ideas — totals ===")
    for row in cur.execute(
        "SELECT program, COUNT(*) AS n FROM company_ideas GROUP BY program ORDER BY n DESC"
    ):
        print(f"  {row['program']:<25} {row['n']:>6}")
    tot = cur.execute("SELECT COUNT(*) AS n FROM company_ideas").fetchone()["n"]
    print(f"  {'TOTAL':<25} {tot:>6}")

    print("\n=== cross-program overlap (dedupe by normalized_website) ===")
    rows = cur.execute(
        """
        SELECT normalized_website, COUNT(DISTINCT program) AS programs,
               GROUP_CONCAT(DISTINCT program) AS programs_list,
               GROUP_CONCAT(DISTINCT company) AS names
          FROM company_ideas
         WHERE normalized_website IS NOT NULL
         GROUP BY normalized_website
        HAVING programs > 1
         ORDER BY programs DESC, normalized_website
         LIMIT 20
        """
    ).fetchall()
    if not rows:
        print("  (no cross-program matches yet)")
    else:
        overlap = cur.execute(
            """
            SELECT COUNT(*) AS n FROM (
              SELECT normalized_website
                FROM company_ideas
               WHERE normalized_website IS NOT NULL
               GROUP BY normalized_website
              HAVING COUNT(DISTINCT program) > 1
            )
            """
        ).fetchone()["n"]
        print(f"  {overlap} companies in 2+ programs; first 20:")
        for r in rows:
            print(f"  {r['normalized_website']:<35} {r['programs_list']} "
                  f"| {r['names'][:60]}")

    print("\n=== coverage sanity ===")
    for col in ("one_liner", "long_description", "company_website", "country"):
        filled = cur.execute(
            f"SELECT COUNT(*) AS n FROM company_ideas WHERE {col} IS NOT NULL AND {col} != ''"
        ).fetchone()["n"]
        pct = (filled / tot * 100) if tot else 0
        print(f"  {col:<20} {filled:>6} / {tot} ({pct:.0f}%)")

    print("\n=== top normalized tags ===")
    tag_counts: Counter[str] = Counter()
    for row in cur.execute("SELECT tags_normalized FROM company_ideas WHERE tags_normalized IS NOT NULL"):
        try:
            for t in json.loads(row["tags_normalized"]):
                tag_counts[t] += 1
        except Exception:  # noqa: BLE001
            continue
    for tag, n in tag_counts.most_common(25):
        print(f"  {n:>5}  {tag}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--glob", default="data/ideas/ideas_*.jsonl",
                   help="Glob of JSONL files to load (default: data/ideas/ideas_*.jsonl)")
    p.add_argument("--db", default="handelsregister.db",
                   help="SQLite database path (default: handelsregister.db)")
    p.add_argument("--no-report", action="store_true", help="Skip the summary report")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    paths = sorted(Path(p) for p in globmod.glob(args.glob))
    if not paths:
        logger.error("No files match %s", args.glob)
        sys.exit(1)

    db = Database(args.db)
    try:
        total_seen = 0
        total_inserted = 0
        for path in paths:
            recs = load_jsonl(path)
            total_seen += len(recs)
            n = save_company_ideas(db, recs)
            total_inserted += n
            logger.info("%s: %d records read, %d new inserts", path.name, len(recs), n)
        logger.info("DONE: %d read, %d new inserts (rest were dupes)",
                    total_seen, total_inserted)
        if not args.no_report:
            report(db)
    finally:
        db.close() if hasattr(db, "close") else db.conn.close()


if __name__ == "__main__":
    main()
