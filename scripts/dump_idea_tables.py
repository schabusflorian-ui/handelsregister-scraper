"""
Dump the idea-discovery tables from the local SQLite DB into a gzipped
SQL file suitable for seeding a remote deployment (e.g. Railway).

Produces a self-contained file:
    PRAGMA foreign_keys=OFF;
    DROP TABLE IF EXISTS company_ideas;
    ...
    BEGIN TRANSACTION;
    CREATE TABLE ...
    INSERT INTO ...
    ...
    COMMIT;

Restore on the target DB via POST to /admin/ideas/seed (see
web/routers/ideas.py `admin_ideas_seed`).

Usage:
    python3 scripts/dump_idea_tables.py                     # default paths
    python3 scripts/dump_idea_tables.py --db mydb.db --out seed.sql.gz
"""

import argparse
import gzip
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

IDEA_TABLES = [
    "company_ideas",
    "idea_extraction",
    "idea_extraction_tag_backup",
    "idea_clusters",
    "website_enrichment",
    # Added for the gap-ranking + tag-normalization pipeline. gap_feedback
    # is intentionally NOT dumped — Railway owns user thumbs-up/down state.
    "tag_alias",
    "idea_gap_ranking",
]


def _sqlite_dump(db_path: Path, tables: list[str]) -> bytes:
    sqlite = shutil.which("sqlite3")
    if not sqlite:
        raise SystemExit("sqlite3 binary not found on PATH")
    cmd = [sqlite, str(db_path), ".dump " + " ".join(tables)]
    out = subprocess.run(cmd, check=True, capture_output=True)
    return out.stdout


def dump(db_path: Path, out_path: Path) -> int:
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    logger.info("dumping tables from %s: %s", db_path, ", ".join(IDEA_TABLES))
    sql = _sqlite_dump(db_path, IDEA_TABLES)

    preamble = (
        "PRAGMA foreign_keys=OFF;\n"
        + "".join(f"DROP TABLE IF EXISTS {t};\n" for t in IDEA_TABLES)
        + "\n"
    ).encode()

    body = preamble + sql
    logger.info("uncompressed size: %.1f MB", len(body) / 1024 / 1024)

    with gzip.open(out_path, "wb", compresslevel=9) as f:
        f.write(body)

    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info("wrote %s (%.1f MB compressed)", out_path, size_mb)
    return out_path.stat().st_size


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="handelsregister.db",
                   help="source SQLite DB (default: handelsregister.db)")
    p.add_argument("--out", default="data/ideas/idea_seed.sql.gz",
                   help="output path (default: data/ideas/idea_seed.sql.gz)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dump(Path(args.db), out_path)
    print(f"\nTo seed Railway:\n"
          f"  curl -fSL -X POST \\\n"
          f"    -F \"file=@{out_path}\" \\\n"
          f"    https://fabulous-fascination-production-4638.up.railway.app/admin/ideas/seed")


if __name__ == "__main__":
    main()
