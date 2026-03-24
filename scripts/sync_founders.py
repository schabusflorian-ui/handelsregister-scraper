#!/usr/bin/env python3
"""
Sync stealth founders between databases.

Export: python scripts/sync_founders.py --export founders.json
Import: python scripts/sync_founders.py --import founders.json
"""

import argparse
import json
import sqlite3
from datetime import datetime


def export_founders(db_path: str, output_file: str):
    """Export stealth founders to JSON."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM stealth_founders")
    founders = [dict(row) for row in cursor.fetchall()]

    with open(output_file, "w") as f:
        json.dump(
            {
                "exported_at": datetime.now().isoformat(),
                "count": len(founders),
                "founders": founders,
            },
            f,
            indent=2,
        )

    print(f"Exported {len(founders)} founders to {output_file}")
    conn.close()


def import_founders(db_path: str, input_file: str):
    """Import stealth founders from JSON (merge, don't overwrite)."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    with open(input_file) as f:
        data = json.load(f)

    founders = data.get("founders", [])
    imported = 0
    skipped = 0

    for f in founders:
        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO stealth_founders (
                    linkedin_url, name, headline, location, summary,
                    current_company, previous_companies, detection_source,
                    search_query, stealth_signals, confidence_score,
                    first_seen_at, last_checked_at, profile_changed,
                    company_id, emerged_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    f.get("linkedin_url"),
                    f.get("name"),
                    f.get("headline"),
                    f.get("location"),
                    f.get("summary"),
                    f.get("current_company"),
                    f.get("previous_companies"),
                    f.get("detection_source"),
                    f.get("search_query"),
                    f.get("stealth_signals"),
                    f.get("confidence_score"),
                    f.get("first_seen_at"),
                    f.get("last_checked_at"),
                    f.get("profile_changed"),
                    f.get("company_id"),
                    f.get("emerged_at"),
                    f.get("created_at"),
                ),
            )

            if cursor.rowcount > 0:
                imported += 1
            else:
                skipped += 1

        except Exception as e:
            print(f"Error importing {f.get('name')}: {e}")
            skipped += 1

    conn.commit()
    conn.close()

    print(f"Imported {imported} new founders, skipped {skipped} duplicates")


def main():
    parser = argparse.ArgumentParser(description="Sync stealth founders")
    parser.add_argument("--export", metavar="FILE", help="Export founders to JSON file")
    parser.add_argument("--import", dest="import_file", metavar="FILE", help="Import founders from JSON file")
    parser.add_argument("--db", default="handelsregister.db", help="Database path")
    args = parser.parse_args()

    if args.export:
        export_founders(args.db, args.export)
    elif args.import_file:
        import_founders(args.db, args.import_file)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
