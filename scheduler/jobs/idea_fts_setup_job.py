"""
idea_fts_setup_job — build SQLite FTS5 virtual tables over the idea
pipeline's text columns so Datasette exposes full-text search (?_search=).

Three FTS tables:
  company_ideas_fts       — company, one_liner, long_description
  idea_extraction_fts     — problem_statement, solo_buildable_reasoning,
                            ai_first_reasoning, customer_verticals,
                            mechanism_tags, sector_tags
  website_enrichment_fts  — title, meta_description, hero_h1, hero_text

Triggers keep each FTS table in sync with its source on INSERT/UPDATE/DELETE.
Safe to re-run — drops + recreates. Also populates from current rows.
"""

from __future__ import annotations
import argparse
import logging

from persistence.database import Database

logger = logging.getLogger(__name__)

FTS = {
    "company_ideas_fts": {
        "source": "company_ideas",
        "rowid_col": "id",
        "columns": ["company", "one_liner", "long_description"],
    },
    "idea_extraction_fts": {
        "source": "idea_extraction",
        "rowid_col": "company_idea_id",
        "columns": ["problem_statement", "solo_buildable_reasoning",
                    "ai_first_reasoning", "customer_verticals",
                    "mechanism_tags", "sector_tags"],
    },
    "website_enrichment_fts": {
        "source": "website_enrichment",
        "rowid_col": "rowid",   # no integer PK; use implicit rowid
        "columns": ["title", "meta_description", "hero_h1", "hero_text"],
    },
}


def _ddl_for(name: str, cfg: dict) -> list[str]:
    source = cfg["source"]
    rowid = cfg["rowid_col"]
    cols = cfg["columns"]
    col_list = ", ".join(cols)
    col_list_new = ", ".join(f"new.{c}" for c in cols)
    col_list_old = ", ".join(f"old.{c}" for c in cols)

    return [
        f"DROP TRIGGER IF EXISTS {name}_ai",
        f"DROP TRIGGER IF EXISTS {name}_au",
        f"DROP TRIGGER IF EXISTS {name}_ad",
        f"DROP TABLE IF EXISTS {name}",
        (f"CREATE VIRTUAL TABLE {name} USING fts5("
         f"{col_list}, "
         f"content='{source}', content_rowid='{rowid}', "
         f"tokenize='porter unicode61')"),
        (f"INSERT INTO {name}(rowid, {col_list}) "
         f"SELECT {rowid}, {col_list} FROM {source}"),
        # Keep FTS in sync with the source table
        (f"CREATE TRIGGER {name}_ai AFTER INSERT ON {source} BEGIN "
         f"  INSERT INTO {name}(rowid, {col_list}) "
         f"  VALUES (new.{rowid}, {col_list_new}); "
         f"END"),
        (f"CREATE TRIGGER {name}_au AFTER UPDATE ON {source} BEGIN "
         f"  INSERT INTO {name}({name}, rowid, {col_list}) "
         f"  VALUES ('delete', old.{rowid}, {col_list_old}); "
         f"  INSERT INTO {name}(rowid, {col_list}) "
         f"  VALUES (new.{rowid}, {col_list_new}); "
         f"END"),
        (f"CREATE TRIGGER {name}_ad AFTER DELETE ON {source} BEGIN "
         f"  INSERT INTO {name}({name}, rowid, {col_list}) "
         f"  VALUES ('delete', old.{rowid}, {col_list_old}); "
         f"END"),
    ]


def run(db_path: str) -> None:
    db = Database(db_path)
    try:
        cur = db.conn.cursor()
        for name, cfg in FTS.items():
            logger.info("building FTS table %s (source=%s)", name, cfg["source"])
            for stmt in _ddl_for(name, cfg):
                cur.execute(stmt)
            n = cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            logger.info("  %s: %d rows", name, n)
        db.conn.commit()
    finally:
        db.conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    run(args.db)


if __name__ == "__main__":
    main()
