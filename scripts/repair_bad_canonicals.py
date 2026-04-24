"""
Repair canonical tags that the older plural-strip rule mangled
(subscription-saa → subscription-saas, devop → devops, etc.).

Rewrites idea_extraction.mechanism_tags / sector_tags in place + updates
tag_alias rows that reference the bad form. Idempotent: running twice
is a no-op once applied.

Usage:
    python3 scripts/repair_bad_canonicals.py --dry-run
    python3 scripts/repair_bad_canonicals.py
"""

import argparse
import json
import logging
import sqlite3
from typing import Dict

logger = logging.getLogger(__name__)


# Observed bad canonical → correct canonical. Scope differs per axis
# because e.g. "developer" is a legit mechanism (customer-support for
# devs) but in sector should be "developers" per the seed vocab.
REPAIRS_MECH = {
    "subscription-saa": "subscription-saas",
    "devop":            "devops",
    "sale":             "sales",
    "logistic":         "logistics",
    "analytic":         "analytics",
    "sport":            "sports",
    "operation":        "operations",
    "finance-op":       "finance-ops",
    "retail-saa":       "retail-saas",
    "b2b-saa":          "b2b-saas",
    "healthcare-saa":   "healthcare-saas",
    "consumer-saa":     "consumer-saas",
    "enterprise-saa":   "enterprise-saas",
}

REPAIRS_SECTOR = {
    **REPAIRS_MECH,
    "developer":            "developers",
    "creator":              "creators",
    "designer":             "designers",
    "founder":              "founders",
    "freelancer":           "freelancers",
    "prosumer-freelancer":  "prosumer-freelancers",
}


def _rewrite(tags_json: str, repair: Dict[str, str]) -> str:
    """Apply repair map to a JSON-array tag column. Preserves order,
    drops dupes that emerge after merging."""
    try:
        tags = json.loads(tags_json)
    except Exception:
        return tags_json
    out, seen = [], set()
    for t in tags:
        fixed = repair.get(t, t)
        if fixed not in seen:
            seen.add(fixed)
            out.append(fixed)
    return json.dumps(out)


def run(db_path: str, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # --- idea_extraction rewrite ---
    for col, repair in (("mechanism_tags", REPAIRS_MECH),
                        ("sector_tags",    REPAIRS_SECTOR)):
        hit_tokens = [f'"{bad}"' for bad in repair]
        hit_clause = " OR ".join(f"{col} LIKE ?" for _ in hit_tokens)
        params = [f"%{tok}%" for tok in hit_tokens]
        rows = cur.execute(
            f"SELECT company_idea_id, {col} FROM idea_extraction "
            f"WHERE {col} IS NOT NULL AND error IS NULL AND ({hit_clause})",
            params,
        ).fetchall()
        changed = 0
        for r in rows:
            old = r[col]
            new = _rewrite(old, repair)
            if new != old:
                if not dry_run:
                    cur.execute(
                        f"UPDATE idea_extraction SET {col} = ? "
                        f"WHERE company_idea_id = ?",
                        (new, r["company_idea_id"]),
                    )
                changed += 1
        logger.info("%s: %d rows %s",
                    col, changed, "would change" if dry_run else "rewritten")

    # --- tag_alias canonicalization ---
    for axis, repair in (("mechanism", REPAIRS_MECH),
                         ("sector",    REPAIRS_SECTOR)):
        bad = list(repair.keys())
        qmarks = ",".join("?" * len(bad))
        rows = cur.execute(
            f"SELECT raw_tag, canonical, occurrences FROM tag_alias "
            f"WHERE axis = ? AND canonical IN ({qmarks})",
            (axis, *bad),
        ).fetchall()
        for r in rows:
            new = repair[r["canonical"]]
            if not dry_run:
                cur.execute(
                    "UPDATE tag_alias SET canonical = ? "
                    "WHERE axis = ? AND raw_tag = ?",
                    (new, axis, r["raw_tag"]),
                )
        logger.info("tag_alias [%s]: %d canonical rewrites", axis, len(rows))

    if not dry_run:
        conn.commit()
    conn.close()
    logger.info("done (%s)", "dry-run" if dry_run else "applied")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    run(args.db, args.dry_run)


if __name__ == "__main__":
    main()
