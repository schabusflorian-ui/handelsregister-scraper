#!/usr/bin/env python3
"""
Export stealth founders from local database for syncing to Railway.

Usage:
    # Export to JSON file
    python scripts/export_founders.py --output founders.json

    # Output base64 (for easy copy-paste)
    python scripts/export_founders.py --base64

    # Export only high-confidence founders
    python scripts/export_founders.py --min-confidence 0.5 --base64

    # Export founders discovered in last N days
    python scripts/export_founders.py --days 7 --base64
"""

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.database import Database


def export_founders(
    db_path: str = "handelsregister.db",
    min_confidence: float = 0.0,
    days: int = None,
    limit: int = None,
) -> list:
    """Export stealth founders from database."""
    db = Database(db_path)

    try:
        conditions = []
        params = []

        if min_confidence > 0:
            conditions.append("confidence_score >= ?")
            params.append(min_confidence)

        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            conditions.append("first_seen_at >= ?")
            params.append(cutoff)

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        limit_clause = f"LIMIT {limit}" if limit else ""

        query = f"""
            SELECT
                linkedin_url,
                name,
                headline,
                location,
                summary,
                current_company,
                previous_companies,
                detection_source,
                search_query,
                stealth_signals,
                confidence_score,
                first_seen_at,
                last_checked_at
            FROM stealth_founders
            WHERE {where_clause}
            ORDER BY confidence_score DESC, first_seen_at DESC
            {limit_clause}
        """

        rows = db.conn.execute(query, params).fetchall()
        founders = [dict(row) for row in rows]

        return founders

    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Export stealth founders for sync")
    parser.add_argument("--db", default="handelsregister.db", help="Database path (default: handelsregister.db)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--base64", "-b", action="store_true", help="Output as base64 encoded JSON (easier to paste)")
    parser.add_argument("--min-confidence", type=float, default=0.0, help="Minimum confidence score (default: 0.0)")
    parser.add_argument("--days", type=int, help="Only export founders discovered in last N days")
    parser.add_argument("--limit", type=int, help="Maximum number of founders to export")
    parser.add_argument("--pretty", action="store_true", help="Pretty print JSON (not compatible with --base64)")

    args = parser.parse_args()

    # Export founders
    founders = export_founders(
        db_path=args.db,
        min_confidence=args.min_confidence,
        days=args.days,
        limit=args.limit,
    )

    if not founders:
        print("No founders found matching criteria", file=sys.stderr)
        sys.exit(1)

    print(f"Exported {len(founders)} founders", file=sys.stderr)

    # Format output
    if args.base64:
        json_str = json.dumps(founders, ensure_ascii=False)
        output = base64.b64encode(json_str.encode()).decode()
    elif args.pretty:
        output = json.dumps(founders, indent=2, ensure_ascii=False)
    else:
        output = json.dumps(founders, ensure_ascii=False)

    # Write output
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
