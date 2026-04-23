"""
idea_gap_queries — the query layer that turns the DB into something you
actually consume programmatically.

Creates a set of SQL VIEWS for the idea-discovery goal:

  v_idea_full              one row per company, joined across company_ideas,
                           website_enrichment, idea_clusters (parent + sub),
                           idea_extraction
  v_mechanism_sector_cells explode mechanism × sector tags, one row per
                           (mechanism, sector, company) -> used to derive
                           matrix counts and empty cells
  v_mechanism_totals       how often each mechanism appears overall
  v_sector_totals          how often each sector appears overall
  v_matrix                 (mechanism, sector) -> count + example companies
  v_launch_candidates      solo_buildable=1 AND ai_first_advantage=1 AND
                           moat_type='none' rows — the microbusiness-launch
                           shortlist

CLI commands (all output plain CSV/JSON for machine consumption — no UI):

  --setup-views           create/refresh all views (idempotent)
  --stats                 quick size summary
  --matrix PATH.csv       dump mechanism × sector matrix as CSV
  --empty-cells           list (mechanism, sector) pairs with zero rows
                          where both axes are 'big enough' to mean something
  --sparse-cells N        list pairs with <= N rows
  --launch-candidates     dump v_launch_candidates as JSONL
  --cell MECH SECTOR      list companies in a specific cell

All commands print to stdout unless an output path is given.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from typing import List, Optional

from persistence.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# View definitions
# ---------------------------------------------------------------------------

VIEWS = [
    ("v_idea_full", """
        CREATE VIEW v_idea_full AS
        SELECT ci.id,
               ci.program,
               ci.company,
               ci.batch,
               ci.year_founded,
               ci.country,
               ci.one_liner,
               ci.long_description,
               ci.company_website,
               ci.normalized_website,
               ci.cluster_id,
               c.label            AS cluster_label,
               c.era_class        AS cluster_era,
               c.parent_cluster_id,
               pc.label           AS parent_cluster_label,
               pc.era_class       AS parent_cluster_era,
               ie.problem_statement,
               ie.customer_verticals,
               ie.mechanism_tags,
               ie.sector_tags,
               ie.customer_size,
               ie.business_model,
               ie.solo_buildable,
               ie.solo_buildable_reasoning,
               ie.ai_first_advantage,
               ie.ai_first_reasoning,
               ie.moat_type,
               ie.niche_specificity,
               we.meta_description AS web_meta,
               we.hero_h1          AS web_h1,
               we.hero_text        AS web_hero_text
          FROM company_ideas ci
     LEFT JOIN idea_clusters c      ON c.cluster_id = ci.cluster_id
     LEFT JOIN idea_clusters pc     ON pc.cluster_id = c.parent_cluster_id
     LEFT JOIN idea_extraction ie   ON ie.company_idea_id = ci.id
     LEFT JOIN website_enrichment we ON we.normalized_website = ci.normalized_website
    """),

    # Exploded cells: one row per (company, mechanism, sector) — the raw
    # grain for every matrix question. Guard against NULL/non-JSON with
    # COALESCE + a defensive IS NOT NULL.
    ("v_mechanism_sector_cells", """
        CREATE VIEW v_mechanism_sector_cells AS
        SELECT ie.company_idea_id          AS company_id,
               ci.company                  AS company,
               ci.program                  AS program,
               ci.year_founded             AS year_founded,
               ci.cluster_id               AS cluster_id,
               ie.customer_size            AS customer_size,
               ie.business_model           AS business_model,
               ie.solo_buildable           AS solo_buildable,
               ie.ai_first_advantage       AS ai_first_advantage,
               ie.moat_type                AS moat_type,
               m.value                     AS mechanism,
               s.value                     AS sector
          FROM idea_extraction ie
          JOIN company_ideas ci ON ci.id = ie.company_idea_id
          JOIN json_each(COALESCE(ie.mechanism_tags, '[]')) m
          JOIN json_each(COALESCE(ie.sector_tags, '[]'))    s
         WHERE ie.error IS NULL
    """),

    ("v_mechanism_totals", """
        CREATE VIEW v_mechanism_totals AS
        SELECT mechanism, COUNT(DISTINCT company_id) AS n
          FROM v_mechanism_sector_cells
         GROUP BY mechanism
    """),

    ("v_sector_totals", """
        CREATE VIEW v_sector_totals AS
        SELECT sector, COUNT(DISTINCT company_id) AS n
          FROM v_mechanism_sector_cells
         GROUP BY sector
    """),

    ("v_matrix", """
        CREATE VIEW v_matrix AS
        SELECT mechanism,
               sector,
               COUNT(DISTINCT company_id) AS n,
               GROUP_CONCAT(DISTINCT company) AS example_companies
          FROM v_mechanism_sector_cells
         GROUP BY mechanism, sector
    """),

    # Strict: solo + ai-first + no structural moat at all.
    # Loose:  solo + ai-first, any moat except 'regulatory' or 'capital'
    #         (both disqualifying for a microbusiness launch).
    # v_launch_candidates aliases v_launch_candidates_loose since the strict
    # view was returning near-zero rows (LLM picks 'brand' / 'integration'
    # for most YC-ish companies).
    ("v_launch_candidates_strict", """
        CREATE VIEW v_launch_candidates_strict AS
        SELECT ci.id, ci.program, ci.company, ci.year_founded,
               ci.company_website, ci.cluster_id,
               c.label AS cluster_label, c.era_class AS cluster_era,
               ie.problem_statement, ie.customer_verticals,
               ie.mechanism_tags, ie.sector_tags,
               ie.customer_size, ie.business_model,
               ie.moat_type, ie.niche_specificity,
               ie.ai_first_reasoning, ie.solo_buildable_reasoning
          FROM idea_extraction ie
          JOIN company_ideas ci  ON ci.id = ie.company_idea_id
     LEFT JOIN idea_clusters c   ON c.cluster_id = ci.cluster_id
         WHERE ie.error IS NULL
           AND ie.solo_buildable = 1
           AND ie.ai_first_advantage = 1
           AND ie.moat_type = 'none'
    """),

    ("v_launch_candidates_loose", """
        CREATE VIEW v_launch_candidates_loose AS
        SELECT ci.id, ci.program, ci.company, ci.year_founded,
               ci.company_website, ci.cluster_id,
               c.label AS cluster_label, c.era_class AS cluster_era,
               ie.problem_statement, ie.customer_verticals,
               ie.mechanism_tags, ie.sector_tags,
               ie.customer_size, ie.business_model,
               ie.moat_type, ie.niche_specificity,
               ie.ai_first_reasoning, ie.solo_buildable_reasoning
          FROM idea_extraction ie
          JOIN company_ideas ci  ON ci.id = ie.company_idea_id
     LEFT JOIN idea_clusters c   ON c.cluster_id = ci.cluster_id
         WHERE ie.error IS NULL
           AND ie.solo_buildable = 1
           AND ie.ai_first_advantage = 1
           AND ie.moat_type NOT IN ('regulatory', 'capital')
    """),

    # default alias — the view users type most often
    ("v_launch_candidates", """
        CREATE VIEW v_launch_candidates AS
        SELECT * FROM v_launch_candidates_loose
    """),
]


def setup_views(db: Database) -> None:
    """Drop and recreate all views. Safe to run any time — cheap since
    views are not materialized."""
    cur = db.conn.cursor()
    for name, ddl in VIEWS:
        cur.execute(f"DROP VIEW IF EXISTS {name}")
        cur.execute(ddl)
    db.conn.commit()
    logger.info("created/refreshed %d views", len(VIEWS))


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def stats(db: Database) -> None:
    cur = db.conn.cursor()
    for q, label in [
        ("SELECT COUNT(*) FROM company_ideas",                      "company_ideas"),
        ("SELECT COUNT(*) FROM idea_extraction WHERE error IS NULL","idea_extraction (ok)"),
        ("SELECT COUNT(*) FROM idea_extraction WHERE error NOT NULL","idea_extraction (err)"),
        ("SELECT COUNT(DISTINCT mechanism) FROM v_mechanism_sector_cells","distinct mechanisms"),
        ("SELECT COUNT(DISTINCT sector)    FROM v_mechanism_sector_cells","distinct sectors"),
        ("SELECT COUNT(*)                  FROM v_matrix",          "populated cells"),
        ("SELECT COUNT(*)                  FROM v_launch_candidates","launch_candidates"),
    ]:
        try:
            n = cur.execute(q).fetchone()[0]
        except Exception as e:  # noqa: BLE001
            n = f"ERR: {e}"
        print(f"  {label:<26} {n}")


def dump_matrix_csv(db: Database, path: str) -> None:
    cur = db.conn.cursor()
    rows = cur.execute("""
        SELECT mechanism, sector, n, example_companies
          FROM v_matrix
         ORDER BY mechanism, sector
    """).fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mechanism", "sector", "n", "example_companies"])
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {path}")


def empty_cells(db: Database, min_mech: int, min_sector: int) -> None:
    """Mechanism-sector pairs that never co-occur but where BOTH axes are
    used often enough that the absence is interesting."""
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        WITH big_mechs AS (
          SELECT mechanism FROM v_mechanism_totals WHERE n >= ?
        ),
        big_sectors AS (
          SELECT sector FROM v_sector_totals WHERE n >= ?
        ),
        all_pairs AS (
          SELECT m.mechanism, s.sector
            FROM big_mechs m, big_sectors s
        ),
        filled AS (
          SELECT DISTINCT mechanism, sector FROM v_matrix
        )
        SELECT ap.mechanism, ap.sector,
               (SELECT n FROM v_mechanism_totals WHERE mechanism = ap.mechanism) AS mech_total,
               (SELECT n FROM v_sector_totals   WHERE sector    = ap.sector)    AS sector_total
          FROM all_pairs ap
     LEFT JOIN filled f
            ON f.mechanism = ap.mechanism AND f.sector = ap.sector
         WHERE f.mechanism IS NULL
         ORDER BY mech_total DESC, sector_total DESC
        """,
        (min_mech, min_sector),
    ).fetchall()
    w = csv.writer(sys.stdout)
    w.writerow(["mechanism", "sector", "mech_total", "sector_total"])
    for r in rows:
        w.writerow(r)
    print(f"# {len(rows)} empty cells (min mechanism total={min_mech}, "
          f"min sector total={min_sector})", file=sys.stderr)


def sparse_cells(db: Database, max_n: int) -> None:
    """Populated but sparse cells — could be underexploited niches."""
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        SELECT mechanism, sector, n, example_companies
          FROM v_matrix
         WHERE n > 0 AND n <= ?
         ORDER BY n, mechanism, sector
        """,
        (max_n,),
    ).fetchall()
    w = csv.writer(sys.stdout)
    w.writerow(["mechanism", "sector", "n", "example_companies"])
    w.writerows(rows)
    print(f"# {len(rows)} sparse cells (n <= {max_n})", file=sys.stderr)


def launch_candidates(db: Database) -> None:
    cur = db.conn.cursor()
    rows = cur.execute("SELECT * FROM v_launch_candidates ORDER BY year_founded DESC").fetchall()
    cols = [d[0] for d in cur.description]
    for r in rows:
        print(json.dumps({k: r[k] for k in cols}, default=str, ensure_ascii=False))
    print(f"# {len(rows)} launch candidates", file=sys.stderr)


def cell(db: Database, mechanism: str, sector: str) -> None:
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        SELECT company, program, year_founded, customer_size, business_model,
               moat_type
          FROM v_mechanism_sector_cells
         WHERE mechanism = ? AND sector = ?
         ORDER BY year_founded DESC
        """,
        (mechanism, sector),
    ).fetchall()
    w = csv.writer(sys.stdout)
    w.writerow(["company", "program", "year_founded", "customer_size",
                "business_model", "moat_type"])
    w.writerows(rows)
    print(f"# {len(rows)} companies in cell ({mechanism} × {sector})",
          file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--setup-views", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--matrix", metavar="PATH.csv")
    p.add_argument("--empty-cells", action="store_true")
    p.add_argument("--sparse-cells", type=int, metavar="N")
    p.add_argument("--launch-candidates", action="store_true")
    p.add_argument("--cell", nargs=2, metavar=("MECH", "SECTOR"))
    p.add_argument("--min-mech", type=int, default=10,
                   help="For --empty-cells: minimum mechanism total to consider")
    p.add_argument("--min-sector", type=int, default=10,
                   help="For --empty-cells: minimum sector total to consider")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    db = Database(args.db)
    try:
        # setup_views is always safe — run unless user passed --stats only
        setup_views(db)
        if args.stats:
            stats(db)
        if args.matrix:
            dump_matrix_csv(db, args.matrix)
        if args.empty_cells:
            empty_cells(db, args.min_mech, args.min_sector)
        if args.sparse_cells is not None:
            sparse_cells(db, args.sparse_cells)
        if args.launch_candidates:
            launch_candidates(db)
        if args.cell:
            cell(db, args.cell[0], args.cell[1])
        if not any([args.stats, args.matrix, args.empty_cells,
                    args.sparse_cells is not None, args.launch_candidates,
                    args.cell]):
            # default action: just set up views and print stats
            stats(db)
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
