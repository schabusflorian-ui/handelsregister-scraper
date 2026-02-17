#!/usr/bin/env python3
"""
Backfill script to recalculate AI and climate scores for all companies.

After splitting AI/robotics keywords from climate keywords, existing companies
need both scores recalculated. This script:

1. Loads all companies from the database
2. Re-runs the AIRoboticsFilter on each company name
3. Updates ai_robotics_score and climate_score
4. Reports statistics on changes

Usage:
    python scripts/backfill_scores.py
    python scripts/backfill_scores.py --dry-run
    python scripts/backfill_scores.py --db handelsregister.db
"""

import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence.database import Database
from processing.filters import AIRoboticsFilter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def run_backfill(db_path: str = 'handelsregister.db', dry_run: bool = False):
    """Recalculate AI and climate scores for all companies."""
    db = Database(db_path)
    filter_ = AIRoboticsFilter()

    try:
        cursor = db.conn.cursor()

        # Get all companies
        cursor.execute("""
            SELECT id, name, current_status, ai_robotics_score, climate_score,
                   matched_keywords, tech_categories
            FROM companies
            ORDER BY id
        """)
        companies = [dict(row) for row in cursor.fetchall()]

        logger.info("Found %d companies to process", len(companies))

        if not companies:
            logger.info("No companies to process")
            return

        stats = {
            'total': len(companies),
            'ai_changed': 0,
            'climate_changed': 0,
            'climate_gained': 0,  # Companies that got a new climate score > 0
            'ai_lost': 0,         # Companies whose AI score dropped to 0
            'unchanged': 0,
        }

        batch_size = 500
        for i, company in enumerate(companies):
            old_ai = company['ai_robotics_score'] or 0
            old_climate = company['climate_score'] or 0

            # Re-run filter
            result = filter_.filter_company(
                name=company['name'],
                status=company['current_status'] or '',
            )

            new_ai = result.relevance_score
            new_climate = result.climate_score

            ai_changed = new_ai != old_ai
            climate_changed = new_climate != old_climate

            if ai_changed or climate_changed:
                if ai_changed:
                    stats['ai_changed'] += 1
                    if new_ai == 0 and old_ai > 0:
                        stats['ai_lost'] += 1
                if climate_changed:
                    stats['climate_changed'] += 1
                    if new_climate > 0 and old_climate == 0:
                        stats['climate_gained'] += 1

                if not dry_run:
                    cursor.execute("""
                        UPDATE companies
                        SET ai_robotics_score = ?,
                            climate_score = ?,
                            matched_keywords = ?,
                            tech_categories = ?
                        WHERE id = ?
                    """, (
                        new_ai,
                        new_climate,
                        result.matched_keywords,
                        result.tech_categories,
                        company['id'],
                    ))

                # Log significant changes
                if abs(new_ai - old_ai) >= 2 or abs(new_climate - old_climate) >= 2:
                    logger.info(
                        "  %s: AI %d->%d, Climate %d->%d",
                        company['name'][:50], old_ai, new_ai, old_climate, new_climate
                    )
            else:
                stats['unchanged'] += 1

            # Commit in batches
            if not dry_run and (i + 1) % batch_size == 0:
                db.conn.commit()
                logger.info("  Processed %d/%d companies...", i + 1, len(companies))

        # Final commit
        if not dry_run:
            db.conn.commit()

        # === Summary ===
        logger.info("")
        logger.info("=== Backfill Summary ===")
        logger.info("Total companies:     %d", stats['total'])
        logger.info("AI score changed:    %d", stats['ai_changed'])
        logger.info("Climate score set:   %d", stats['climate_gained'])
        logger.info("Climate changed:     %d", stats['climate_changed'])
        logger.info("AI score lost (->0): %d", stats['ai_lost'])
        logger.info("Unchanged:           %d", stats['unchanged'])

        if dry_run:
            logger.info("")
            logger.info("DRY RUN - no changes were made")

    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill AI and climate scores')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--db', default='handelsregister.db',
                        help='Database path')
    args = parser.parse_args()

    run_backfill(db_path=args.db, dry_run=args.dry_run)
