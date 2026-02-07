#!/usr/bin/env python3
"""
Backfill script for enhanced stealth detection.

This script:
1. Re-calculates confidence scores using v2 algorithm
2. Runs emergence matching to link founders to companies
3. Logs all changes to founder_history table

Usage:
    python scripts/backfill_stealth_detection.py
    python scripts/backfill_stealth_detection.py --dry-run
"""

import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence.database import Database
from processing.emergence_matcher import EmergenceMatcher, run_emergence_detection
from sources.linkedin_scraper import (
    STEALTH_KEYWORDS, TRANSITION_KEYWORDS, URGENCY_KEYWORDS,
    TRACTION_KEYWORDS, REPEAT_FOUNDER_KEYWORDS, HIGH_VALUE_COMPANIES
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def recalculate_confidence(founder: dict) -> tuple:
    """
    Recalculate confidence score using v2 algorithm.

    Returns: (new_score, detected_signals)
    """
    # Combine text fields
    text = ' '.join(filter(None, [
        founder.get('headline', ''),
        founder.get('summary', ''),
        founder.get('name', ''),
    ])).lower()

    signals = {
        'stealth': [],
        'transition': [],
        'urgency': [],
        'traction': [],
        'founder': [],
        'background': [],
    }

    # Detect signals
    for kw in STEALTH_KEYWORDS:
        if kw in text:
            signals['stealth'].append(kw)

    for kw in TRANSITION_KEYWORDS:
        if kw in text:
            signals['transition'].append(kw)

    for kw in URGENCY_KEYWORDS:
        if kw in text:
            signals['urgency'].append(kw)

    for kw in TRACTION_KEYWORDS:
        if kw in text:
            signals['traction'].append(kw)

    founder_keywords = ['founder', 'co-founder', 'cofounder', 'gründer', 'ceo', 'entrepreneur']
    for kw in founder_keywords:
        if kw in text:
            signals['founder'].append(kw)

    for company in HIGH_VALUE_COMPANIES:
        if company in text:
            signals['background'].append(company)

    # Check repeat founder
    is_repeat = any(kw in text for kw in REPEAT_FOUNDER_KEYWORDS)

    # Calculate company tier
    tier = 0
    faang = {'google', 'meta', 'facebook', 'amazon', 'apple', 'microsoft'}
    unicorns = {'stripe', 'airbnb', 'uber', 'spotify', 'klarna', 'revolut', 'celonis'}
    for company in signals['background']:
        if company.lower() in faang:
            tier = max(tier, 4)
        elif company.lower() in unicorns:
            tier = max(tier, 3)
        else:
            tier = max(tier, 2)

    # Calculate v2 score
    score = 0.0

    # Tier 1: Direct stealth signals (0.35 max)
    if signals['stealth']:
        if any('stealth' in s for s in signals['stealth']):
            score += 0.25
        else:
            score += min(0.15, len(signals['stealth']) * 0.05)
        score = min(score, 0.35)

    # Tier 2: Career transition (0.20 max)
    tier2 = 0.0
    if signals['transition']:
        tier2 += min(0.10, len(signals['transition']) * 0.04)
    if is_repeat:
        tier2 += 0.10
    if signals['founder']:
        tier2 += min(0.08, len(signals['founder']) * 0.03)
    score += min(0.20, tier2)

    # Tier 3: Background (0.20 max)
    tier3 = tier * 0.04
    if signals['background']:
        tier3 += 0.04
    score += min(0.20, tier3)

    # Tier 4: Urgency (0.15 max)
    tier4 = 0.0
    if signals['urgency']:
        tier4 += min(0.07, len(signals['urgency']) * 0.03)
    if signals['traction']:
        tier4 += min(0.05, len(signals['traction']) * 0.02)
    score += min(0.15, tier4)

    # Tier 5: Location (0.10 max)
    location = (founder.get('location') or '').lower()
    dach_countries = ['germany', 'deutschland', 'austria', 'österreich', 'switzerland', 'schweiz']
    dach_cities = ['berlin', 'munich', 'münchen', 'hamburg', 'frankfurt', 'vienna', 'wien', 'zurich', 'zürich']

    if any(x in location for x in dach_cities):
        score += 0.10
    elif any(x in location for x in dach_countries):
        score += 0.08

    return min(1.0, score), signals


def run_backfill(db_path: str = 'handelsregister.db', dry_run: bool = False):
    """Run the backfill process."""
    db = Database(db_path)

    try:
        cursor = db.conn.cursor()

        # Get all stealth founders
        cursor.execute('SELECT * FROM stealth_founders')
        founders = [dict(row) for row in cursor.fetchall()]

        logger.info(f"Found {len(founders)} stealth founders to process")

        if not founders:
            logger.info("No founders to process")
            return

        # === Step 1: Recalculate confidence scores ===
        logger.info("")
        logger.info("=== Step 1: Recalculating confidence scores ===")

        updated_count = 0
        for founder in founders:
            old_score = founder['confidence_score']
            new_score, signals = recalculate_confidence(founder)

            # Count non-empty signal categories
            signal_count = sum(1 for v in signals.values() if v)

            if abs(new_score - old_score) > 0.01:
                logger.info(f"  {founder['name']}: {old_score:.2f} -> {new_score:.2f} ({signal_count} signal types)")

                if not dry_run:
                    # Update using our new method (which logs changes)
                    import json
                    db.update_stealth_founder(
                        founder_id=founder['id'],
                        confidence_score=new_score,
                        stealth_signals=json.dumps(signals),
                    )
                updated_count += 1
            else:
                logger.info(f"  {founder['name']}: {old_score:.2f} (unchanged)")

        logger.info(f"Updated {updated_count} confidence scores")

        # === Step 2: Run emergence matching ===
        logger.info("")
        logger.info("=== Step 2: Running emergence matching ===")

        matcher = EmergenceMatcher(db, min_name_similarity=0.85)

        # Get unemerged founders
        unemerged = db.get_unemerged_founders(min_confidence=0.2)
        logger.info(f"Checking {len(unemerged)} unemerged founders")

        candidates = []
        auto_linked = []

        for founder in unemerged:
            matches = matcher.find_matches_for_founder(founder, limit=3)

            if matches:
                best = matches[0]
                logger.info(f"  {founder['name']} -> {best['company_name']} (similarity: {best['name_similarity']:.2f})")

                if best['name_similarity'] >= 0.95:
                    if not dry_run:
                        db.mark_founder_emerged(founder['id'], best['company_id'])
                    auto_linked.append({
                        'founder': founder['name'],
                        'company': best['company_name'],
                        'similarity': best['name_similarity'],
                    })
                else:
                    candidates.append({
                        'founder': founder['name'],
                        'company': best['company_name'],
                        'similarity': best['name_similarity'],
                    })
            else:
                logger.info(f"  {founder['name']} -> No match found")

        # === Summary ===
        logger.info("")
        logger.info("=== Backfill Summary ===")
        logger.info(f"Founders processed: {len(founders)}")
        logger.info(f"Confidence scores updated: {updated_count}")
        logger.info(f"Auto-linked to companies: {len(auto_linked)}")
        logger.info(f"Candidates for review: {len(candidates)}")

        if auto_linked:
            logger.info("")
            logger.info("Auto-linked founders:")
            for link in auto_linked:
                logger.info(f"  - {link['founder']} -> {link['company']} ({link['similarity']:.0%})")

        if candidates:
            logger.info("")
            logger.info("Candidates needing review:")
            for c in candidates:
                logger.info(f"  - {c['founder']} -> {c['company']} ({c['similarity']:.0%})")

        if dry_run:
            logger.info("")
            logger.info("DRY RUN - no changes were made")

        # Check founder history
        history = db.get_recent_founder_changes(days=1)
        if history:
            logger.info("")
            logger.info(f"Logged {len(history)} changes to founder_history table")

    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill enhanced stealth detection')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--db', default='handelsregister.db', help='Database path')
    args = parser.parse_args()

    run_backfill(db_path=args.db, dry_run=args.dry_run)
