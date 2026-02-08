#!/usr/bin/env python3
"""
Database Sync Script - Local to Railway

Syncs stealth founders collected locally to Railway deployment.
Uses SQLite export/import to transfer data without overwriting Railway data.

Usage:
    python scripts/sync_to_railway.py --export    # Export local stealth founders to JSON
    python scripts/sync_to_railway.py --stats     # Show local vs remote stats

Then use Railway CLI to import:
    railway run python scripts/sync_to_railway.py --import data/stealth_export.json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.database import Database


def export_stealth_founders(db_path: str, output_path: str) -> dict:
    """Export stealth founders to JSON file."""
    db = Database(db_path)

    try:
        cursor = db.conn.cursor()

        # Export stealth_founders table
        cursor.execute('''
            SELECT * FROM stealth_founders
            ORDER BY created_at DESC
        ''')

        columns = [desc[0] for desc in cursor.description]
        founders = []

        for row in cursor.fetchall():
            founder = dict(zip(columns, row))
            founders.append(founder)

        # Export founder_history table
        cursor.execute('''
            SELECT * FROM founder_history
            ORDER BY changed_at DESC
        ''')

        columns = [desc[0] for desc in cursor.description]
        history = []

        for row in cursor.fetchall():
            entry = dict(zip(columns, row))
            history.append(entry)

        export_data = {
            'exported_at': datetime.now().isoformat(),
            'source': db_path,
            'stealth_founders': founders,
            'founder_history': history,
            'stats': {
                'total_founders': len(founders),
                'total_history': len(history),
            }
        }

        # Write to file
        with open(output_path, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)

        print(f"Exported {len(founders)} founders and {len(history)} history entries")
        print(f"Output: {output_path}")

        return export_data

    finally:
        db.close()


def import_stealth_founders(db_path: str, input_path: str, dry_run: bool = False) -> dict:
    """Import stealth founders from JSON file (merge, don't overwrite)."""

    with open(input_path, 'r') as f:
        data = json.load(f)

    db = Database(db_path)

    try:
        cursor = db.conn.cursor()

        imported = 0
        skipped = 0
        history_imported = 0

        # Import stealth founders (upsert by linkedin_url)
        for founder in data.get('stealth_founders', []):
            linkedin_url = founder.get('linkedin_url')
            if not linkedin_url:
                continue

            # Check if exists
            cursor.execute('SELECT id FROM stealth_founders WHERE linkedin_url = ?', (linkedin_url,))
            existing = cursor.fetchone()

            if existing:
                skipped += 1
                continue

            if not dry_run:
                cursor.execute('''
                    INSERT INTO stealth_founders (
                        linkedin_url, name, headline, location, summary,
                        current_company, previous_companies, detection_source,
                        search_query, stealth_signals, confidence_score,
                        first_seen_at, last_checked_at, profile_changed,
                        company_id, emerged_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    founder.get('linkedin_url'),
                    founder.get('name'),
                    founder.get('headline'),
                    founder.get('location'),
                    founder.get('summary'),
                    founder.get('current_company'),
                    founder.get('previous_companies'),
                    founder.get('detection_source'),
                    founder.get('search_query'),
                    founder.get('stealth_signals'),
                    founder.get('confidence_score'),
                    founder.get('first_seen_at'),
                    founder.get('last_checked_at'),
                    founder.get('profile_changed', 0),
                    founder.get('company_id'),
                    founder.get('emerged_at'),
                    founder.get('created_at'),
                ))

            imported += 1

        # Import founder history
        for entry in data.get('founder_history', []):
            founder_id = entry.get('founder_id')
            changed_at = entry.get('changed_at')

            if not founder_id or not changed_at:
                continue

            # Check if exists (by founder_id + changed_at)
            cursor.execute('''
                SELECT id FROM founder_history
                WHERE founder_id = ? AND changed_at = ?
            ''', (founder_id, changed_at))

            if cursor.fetchone():
                continue

            if not dry_run:
                cursor.execute('''
                    INSERT INTO founder_history (
                        founder_id, field_name, old_value, new_value,
                        change_type, changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    entry.get('founder_id'),
                    entry.get('field_name'),
                    entry.get('old_value'),
                    entry.get('new_value'),
                    entry.get('change_type'),
                    entry.get('changed_at'),
                ))

            history_imported += 1

        if not dry_run:
            db.conn.commit()

        action = "Would import" if dry_run else "Imported"
        print(f"{action} {imported} founders ({skipped} already existed)")
        print(f"{action} {history_imported} history entries")

        return {
            'imported': imported,
            'skipped': skipped,
            'history_imported': history_imported,
        }

    finally:
        db.close()


def show_stats(db_path: str):
    """Show database statistics."""
    db = Database(db_path)

    try:
        cursor = db.conn.cursor()

        # Stealth founders stats
        cursor.execute('SELECT COUNT(*) FROM stealth_founders')
        total = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM stealth_founders WHERE confidence_score >= 0.5')
        high_conf = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM stealth_founders WHERE emerged_at IS NOT NULL')
        emerged = cursor.fetchone()[0]

        # History stats
        cursor.execute('SELECT COUNT(*) FROM founder_history')
        history = cursor.fetchone()[0]

        # Recent activity
        cursor.execute('''
            SELECT created_at FROM stealth_founders
            ORDER BY created_at DESC LIMIT 1
        ''')
        row = cursor.fetchone()
        last_added = row[0] if row else 'N/A'

        print(f"\nDatabase: {db_path}")
        print("=" * 40)
        print(f"  Stealth founders:  {total}")
        print(f"  High confidence:   {high_conf}")
        print(f"  Emerged:           {emerged}")
        print(f"  History entries:   {history}")
        print(f"  Last added:        {last_added}")

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description='Sync stealth founders to Railway')
    parser.add_argument('--export', action='store_true', help='Export local DB to JSON')
    parser.add_argument('--import', dest='import_file', help='Import JSON to DB')
    parser.add_argument('--stats', action='store_true', help='Show database stats')
    parser.add_argument('--dry-run', action='store_true', help='Dry run (no changes)')
    parser.add_argument('--db', default='handelsregister.db', help='Database path')
    parser.add_argument('--output', default='data/stealth_export.json', help='Export output path')

    args = parser.parse_args()

    # Use environment variable if set (for Railway)
    db_path = os.environ.get('DATABASE_PATH', args.db)

    if args.export:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        export_stealth_founders(db_path, args.output)
    elif args.import_file:
        import_stealth_founders(db_path, args.import_file, dry_run=args.dry_run)
    elif args.stats:
        show_stats(db_path)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
