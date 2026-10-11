#!/usr/bin/env python3
"""
On-demand LinkedIn deep-profile enrichment for top stealth_entities.

Reads is_person=1 entities with founder_confidence >= min_fc that have a
primary_linkedin, and attempts to scrape the full profile via
sources/linkedin_scraper.LinkedInProfileScraper. Stores education,
past employers, and skills in stealth_entities.enrichment_* columns.

HONEST CAVEAT — LinkedIn aggressively blocks scraping without a session
cookie. Expect most attempts to fail with "login required" HTML; the
script marks those with last_enriched_at and skips re-trying for 30d.

Usage:
    # Try top 50 persons
    python3 scripts/enrich_profiles.py --limit 50 --min-fc 50

    # Dry run (no DB writes)
    python3 scripts/enrich_profiles.py --limit 10 --dry-run

    # With a session cookie file (if you've exported one from your browser)
    LINKEDIN_SESSION_COOKIE="li_at=..." python3 scripts/enrich_profiles.py

Follow-up option: this framework stays useful if LinkedIn ever becomes
scrapable again (API access, proxy pool, third-party enrichment API). The
columns are populated — switching source is a 10-line change.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from persistence.database import Database  # noqa: E402
from sources.linkedin_scraper import LinkedInProfileScraper  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization helpers — turn LinkedInProfile dataclass into JSON strings
# ---------------------------------------------------------------------------


def _ser_education(profile) -> str | None:
    """profile.education is List[str] today (e.g. 'MIT - BSc Computer Science').
    Store as JSON array."""
    if not profile.education:
        return None
    return json.dumps([str(e).strip() for e in profile.education if e])


def _ser_past_employers(profile) -> str | None:
    """Build from profile.experience (List[WorkExperience]) + previous_companies."""
    rows = []
    for ex in profile.experience or []:
        rows.append(
            {
                "company": ex.company,
                "title": ex.title,
                "duration": ex.duration,
                "current": ex.is_current,
            }
        )
    for p in profile.previous_companies or []:
        if not any(r.get("company") == p for r in rows):
            rows.append({"company": p, "title": None, "duration": None, "current": False})
    return json.dumps(rows) if rows else None


def _ser_skills(profile) -> str | None:
    if not profile.skills:
        return None
    return json.dumps([str(s).strip() for s in profile.skills if s])


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def enrich_batch(db, *, limit: int, min_fc: float, dry_run: bool, delay: float) -> dict:
    cur = db.conn.cursor()
    rows = cur.execute(
        """SELECT id, primary_name, primary_linkedin, founder_confidence, last_enriched_at
             FROM stealth_entities
            WHERE is_person = 1
              AND primary_linkedin IS NOT NULL
              AND founder_confidence >= ?
              AND (last_enriched_at IS NULL
                   OR last_enriched_at < date('now', '-30 days'))
            ORDER BY founder_confidence DESC, composite_score DESC
            LIMIT ?""",
        (min_fc, limit),
    ).fetchall()

    scraper = LinkedInProfileScraper()
    stats = {"attempted": 0, "enriched": 0, "login_wall": 0, "errors": 0}

    for r in rows:
        url = r["primary_linkedin"]
        logger.info("[%d/%d] %s — %s", stats["attempted"] + 1, len(rows), r["primary_name"], url)
        stats["attempted"] += 1

        try:
            profile = scraper.scrape_profile(url)
        except Exception as e:  # noqa: BLE001
            logger.warning("  scrape error: %s", e)
            stats["errors"] += 1
            if not dry_run:
                cur.execute(
                    "UPDATE stealth_entities SET last_enriched_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), r["id"]),
                )
                db.conn.commit()
            time.sleep(delay)
            continue

        # Login-wall detection — most common failure mode
        if (
            profile is None
            or not profile.experience
            and not profile.education
            and (profile.summary or "").startswith("By clicking Continue")
        ):
            logger.warning("  login wall — got no usable data")
            stats["login_wall"] += 1
            if not dry_run:
                cur.execute(
                    "UPDATE stealth_entities SET last_enriched_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), r["id"]),
                )
                db.conn.commit()
            time.sleep(delay)
            continue

        edu = _ser_education(profile)
        past = _ser_past_employers(profile)
        skills = _ser_skills(profile)
        logger.info(
            "  OK — edu=%s past=%s skills=%s", "y" if edu else "-", "y" if past else "-", "y" if skills else "-"
        )
        stats["enriched"] += 1

        if not dry_run:
            cur.execute(
                """UPDATE stealth_entities SET
                     enrichment_education       = ?,
                     enrichment_past_employers  = ?,
                     enrichment_skills          = ?,
                     last_enriched_at           = ?
                   WHERE id = ?""",
                (edu, past, skills, datetime.now(timezone.utc).isoformat(), r["id"]),
            )
            db.conn.commit()
        time.sleep(delay)

    return stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument(
        "--min-fc", type=float, default=50.0, help="Only enrich entities with founder_confidence >= this (default 50)"
    )
    p.add_argument("--delay", type=float, default=15.0, help="Seconds between profile fetches (default 15 — be polite)")
    p.add_argument("--dry-run", action="store_true", help="No DB writes")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    db = Database(args.db)
    try:
        stats = enrich_batch(
            db,
            limit=args.limit,
            min_fc=args.min_fc,
            dry_run=args.dry_run,
            delay=args.delay,
        )
        print(json.dumps(stats, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
