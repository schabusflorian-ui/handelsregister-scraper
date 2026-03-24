#!/usr/bin/env python3
"""
Bulk import investors from CSV file.

CSV format (minimal):
  name,type,aliases
  "Sequoia Capital",vc,"Sequoia"
  "Index Ventures",vc,"Index"

CSV format (full):
  name,type,website,headquarters,aliases,legal_entities,partners
  "HV Capital",vc,"https://hvcapital.com","Munich","HV;Holtzbrinck Ventures","HV Capital Manager GmbH","David Kuczek;Christian Saller"

Usage:
  python scripts/import_investors.py investors.csv
  python scripts/import_investors.py investors.csv --to-yaml  # Append to investors.yaml
  python scripts/import_investors.py investors.csv --to-db    # Import directly to database
"""

import argparse
import csv
import sys
from pathlib import Path

import yaml

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def parse_csv(filepath: str) -> list:
    """Parse CSV file into investor records."""
    investors = []

    with open(filepath, encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # Required field
            name = row.get("name", "").strip()
            if not name:
                continue

            investor = {
                "name": name,
                "type": row.get("type", "vc").strip().lower(),
            }

            # Optional fields
            if row.get("website"):
                investor["website"] = row["website"].strip()

            if row.get("headquarters"):
                investor["headquarters"] = row["headquarters"].strip()

            if row.get("stage_focus"):
                investor["stage_focus"] = [s.strip() for s in row["stage_focus"].split(";")]

            if row.get("sector_focus"):
                investor["sector_focus"] = [s.strip() for s in row["sector_focus"].split(";")]

            # Aliases - split by semicolon, always include canonical name
            aliases = [name]  # Include canonical name as first alias
            if row.get("aliases"):
                for alias in row["aliases"].split(";"):
                    alias = alias.strip()
                    if alias and alias != name:
                        aliases.append(alias)
            investor["aliases"] = aliases

            # Legal entities - split by semicolon
            if row.get("legal_entities"):
                investor["legal_entities"] = [e.strip() for e in row["legal_entities"].split(";") if e.strip()]
            else:
                # Auto-generate common legal entity patterns
                investor["legal_entities"] = [f"{name} GmbH", f"{name} Management GmbH"]

            # Partners - split by semicolon
            if row.get("partners"):
                investor["partners"] = [p.strip() for p in row["partners"].split(";") if p.strip()]

            investors.append(investor)

    return investors


def append_to_yaml(investors: list, yaml_path: str):
    """Append investors to existing YAML file."""
    # Load existing
    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    existing_names = {inv["name"].lower() for inv in data.get("investors", [])}

    added = 0
    skipped = 0

    for inv in investors:
        if inv["name"].lower() in existing_names:
            print(f"  Skipping (exists): {inv['name']}")
            skipped += 1
            continue

        data["investors"].append(inv)
        existing_names.add(inv["name"].lower())
        added += 1
        print(f"  Added: {inv['name']}")

    # Write back
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return added, skipped


def import_to_database(investors: list, db_path: str):
    """Import investors directly to database."""
    from persistence.database import Database

    db = Database(db_path)
    conn = db._get_connection()

    added = 0
    skipped = 0

    for inv in investors:
        try:
            # Check if exists
            existing = conn.execute("SELECT id FROM investors WHERE canonical_name = ?", (inv["name"],)).fetchone()

            if existing:
                print(f"  Skipping (exists): {inv['name']}")
                skipped += 1
                continue

            # Insert investor
            cursor = conn.execute(
                """
                INSERT INTO investors (canonical_name, type, website, headquarters_city)
                VALUES (?, ?, ?, ?)
            """,
                (
                    inv["name"],
                    inv["type"],
                    inv.get("website"),
                    inv.get("headquarters"),
                ),
            )

            inv_id = cursor.lastrowid

            # Insert aliases
            for alias in inv.get("aliases", []):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO investor_aliases (investor_id, alias, alias_type)
                    VALUES (?, ?, 'alias')
                """,
                    (inv_id, alias),
                )

            # Insert legal entities
            for entity in inv.get("legal_entities", []):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO investor_legal_entities (investor_id, entity_name, entity_type)
                    VALUES (?, ?, 'legal_entity')
                """,
                    (inv_id, entity),
                )

            # Insert partners
            for partner in inv.get("partners", []):
                conn.execute(
                    """
                    INSERT OR IGNORE INTO investor_aliases (investor_id, alias, alias_type)
                    VALUES (?, ?, 'partner_name')
                """,
                    (inv_id, partner),
                )

            added += 1
            print(f"  Added: {inv['name']}")

        except Exception as e:
            print(f"  Error adding {inv['name']}: {e}")

    conn.commit()
    db.close()

    return added, skipped


def main():
    parser = argparse.ArgumentParser(description="Import investors from CSV")
    parser.add_argument("csv_file", help="Path to CSV file")
    parser.add_argument("--to-yaml", action="store_true", help="Append to config/investors.yaml")
    parser.add_argument("--to-db", action="store_true", help="Import to database")
    parser.add_argument("--db", default="handelsregister.db", help="Database path")
    parser.add_argument("--yaml", default="config/investors.yaml", help="YAML path")
    parser.add_argument("--dry-run", action="store_true", help="Parse and show without importing")

    args = parser.parse_args()

    # Parse CSV
    print(f"Parsing {args.csv_file}...")
    investors = parse_csv(args.csv_file)
    print(f"Found {len(investors)} investors in CSV\n")

    if args.dry_run:
        print("Dry run - showing parsed investors:")
        for inv in investors[:10]:
            print(f"  - {inv['name']} ({inv['type']})")
            print(f"    Aliases: {inv.get('aliases', [])}")
            print(f"    Legal entities: {inv.get('legal_entities', [])}")
        if len(investors) > 10:
            print(f"  ... and {len(investors) - 10} more")
        return

    if args.to_yaml:
        print(f"Appending to {args.yaml}...")
        added, skipped = append_to_yaml(investors, args.yaml)
        print(f"\nDone: {added} added, {skipped} skipped")

    elif args.to_db:
        print(f"Importing to database {args.db}...")
        added, skipped = import_to_database(investors, args.db)
        print(f"\nDone: {added} added, {skipped} skipped")

    else:
        print("Specify --to-yaml or --to-db to import")
        print("\nExample CSV format:")
        print("name,type,aliases,legal_entities,partners")
        print('"Sequoia Capital",vc,"Sequoia","Sequoia Capital Operations LLC",""')


if __name__ == "__main__":
    main()
