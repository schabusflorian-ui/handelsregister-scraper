"""
idea_opportunity_score_job — compute an `opportunity_score` (0–100) for
every extracted idea so the shortlist can be ranked.

Signals (each contributes 0–25 points, summed to 0–100):

  niche_specificity    narrow=25, medium=12, broad=3
                       Why: narrow niches are more defensibly-scoped
                       for a solo/tiny-team launch.

  era_class            rebuild_candidate=25 (AI-rewrite opportunity)
                       hot=15  (recent, still forming)
                       steady=8, legacy/unknown=3
                       Uses the cluster's era_class.

  moat_type            none=25, brand=18, integration=15,
                       domain_expertise=12, data=8, network=5
                       Why: microbusiness candidates don't have
                       pre-existing network or data moats. "none" is
                       GOOD here — means the space is still contestable.

  launch_shape         solo_buildable=1 AND ai_first_advantage=1 → 25
                       solo_buildable=1 AND ai_first_advantage=0 → 12
                       solo_buildable=0 AND ai_first_advantage=1 → 8
                       else → 0
                       The core "can I actually build this" filter.

  + recency bonus      +0 for founded_year < 2022 (nothing)
                       +5 for 2022-2024 (current)
                       +10 for 2025+ (frontier, newer than most data)
                       Applied only when solo+ai_first are BOTH true.
                       Keeps the ceiling at 100.

Written to `company_ideas.opportunity_score` (column created on first
run). Also writes `opportunity_breakdown` JSON with per-signal scores
for transparency in the UI.

Usage:
  python3 -m scheduler.jobs.idea_opportunity_score_job
  python3 -m scheduler.jobs.idea_opportunity_score_job --report-only
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Dict

from persistence.database import Database

logger = logging.getLogger(__name__)


NICHE = {"narrow": 25, "medium": 12, "broad": 3}
ERA = {
    "rebuild_candidate": 25,
    "hot": 15,
    "steady": 8,
    "legacy": 3,
    "unknown": 3,
    None: 3,
}
MOAT = {
    "none": 25, "brand": 18, "integration": 15,
    "domain_expertise": 12, "data": 8, "network": 5,
    "regulatory": 0, "capital": 0,  # disqualifying
    None: 8,
}


def _launch_shape_score(solo: int, ai_first: int) -> int:
    if solo == 1 and ai_first == 1: return 25
    if solo == 1 and ai_first == 0: return 12
    if solo == 0 and ai_first == 1: return 8
    return 0


def _recency_bonus(year: int, solo: int, ai_first: int) -> int:
    if not year or solo != 1 or ai_first != 1:
        return 0
    if year >= 2025: return 10
    if year >= 2022: return 5
    return 0


def compute_score(row: Dict) -> Dict:
    niche = NICHE.get(row.get("niche_specificity"), 3)
    era = ERA.get(row.get("era_class"), 3)
    moat = MOAT.get(row.get("moat_type"), 8)
    shape = _launch_shape_score(row.get("solo_buildable") or 0,
                                row.get("ai_first_advantage") or 0)
    bonus = _recency_bonus(row.get("year_founded") or 0,
                           row.get("solo_buildable") or 0,
                           row.get("ai_first_advantage") or 0)
    total = niche + era + moat + shape + bonus
    return {
        "score": min(total, 100),
        "breakdown": {
            "niche":   niche,
            "era":     era,
            "moat":    moat,
            "shape":   shape,
            "recency": bonus,
        },
    }


def _ensure_columns(db: Database) -> None:
    cur = db.conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(company_ideas)")}
    if "opportunity_score" not in cols:
        cur.execute("ALTER TABLE company_ideas ADD COLUMN opportunity_score INTEGER")
    if "opportunity_breakdown" not in cols:
        cur.execute("ALTER TABLE company_ideas ADD COLUMN opportunity_breakdown TEXT")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ideas_opp_score "
        "ON company_ideas(opportunity_score DESC)"
    )
    db.conn.commit()


def run(db_path: str) -> None:
    db = Database(db_path)
    _ensure_columns(db)
    cur = db.conn.cursor()
    # Pull the data in one query — ~10K rows with extraction data
    rows = cur.execute(
        """
        SELECT ci.id, ci.year_founded,
               ie.solo_buildable, ie.ai_first_advantage,
               ie.moat_type, ie.niche_specificity,
               c.era_class
          FROM company_ideas ci
          JOIN idea_extraction ie ON ie.company_idea_id = ci.id
                                  AND ie.error IS NULL
     LEFT JOIN idea_clusters c ON c.cluster_id = ci.cluster_id
        """
    ).fetchall()
    logger.info("scoring %d rows", len(rows))

    # Write in a single transaction; ~10K UPDATEs in <2s on laptop.
    with db.conn:
        for r in rows:
            result = compute_score(dict(r))
            cur.execute(
                "UPDATE company_ideas SET opportunity_score = ?, "
                "opportunity_breakdown = ? WHERE id = ?",
                (result["score"], json.dumps(result["breakdown"]), r["id"]),
            )

    report(db_path)
    db.conn.close()


def report(db_path: str) -> None:
    db = Database(db_path)
    cur = db.conn.cursor()
    print("\n=== opportunity_score distribution ===")
    for r in cur.execute(
        """
        SELECT CASE
                 WHEN opportunity_score >= 90 THEN '90-100 (top)'
                 WHEN opportunity_score >= 75 THEN '75-89  (strong)'
                 WHEN opportunity_score >= 60 THEN '60-74  (worth looking)'
                 WHEN opportunity_score >= 40 THEN '40-59  (meh)'
                 WHEN opportunity_score IS NULL THEN 'NULL (no extraction)'
                 ELSE '0-39   (skip)'
               END AS bucket,
               COUNT(*) AS n
          FROM company_ideas
         GROUP BY bucket
         ORDER BY MIN(COALESCE(opportunity_score, -1)) DESC
        """
    ):
        print(f"  {r['bucket']:<24} {r['n']:>6}")

    print("\n=== top 10 scoring ideas ===")
    for r in cur.execute(
        """
        SELECT ci.company, ci.program, ci.year_founded, ci.opportunity_score,
               ie.niche_specificity, ie.moat_type, c.era_class
          FROM company_ideas ci
          JOIN idea_extraction ie ON ie.company_idea_id = ci.id
     LEFT JOIN idea_clusters c   ON c.cluster_id = ci.cluster_id
         WHERE ci.opportunity_score IS NOT NULL
           AND ci.company IS NOT NULL AND ci.company != ''
         ORDER BY ci.opportunity_score DESC, ci.year_founded DESC
         LIMIT 10
        """
    ):
        print(f"  {r['opportunity_score']:>3}  {r['company'][:45]:<47} "
              f"{(r['year_founded'] or '-'):4}  "
              f"{(r['niche_specificity'] or '-'):<8}  "
              f"moat={r['moat_type'] or '-':<18}  "
              f"era={r['era_class'] or '-'}")
    db.conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.report_only:
        report(args.db)
    else:
        run(args.db)


if __name__ == "__main__":
    main()
