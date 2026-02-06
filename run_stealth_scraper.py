#!/usr/bin/env python3
"""
Continuous stealth founder scraper for DACH region.

Runs at ~1 request per minute to avoid rate limiting.
Searches for LinkedIn profiles and scrapes them for stealth signals.

Usage:
    python run_stealth_scraper.py                    # Run with defaults (60s search, 90s scrape)
    python run_stealth_scraper.py --fast             # Faster for testing (30s/45s)
    python run_stealth_scraper.py --slow             # Slower to avoid blocks (120s/180s)
    python run_stealth_scraper.py --iterations 10   # Run only 10 iterations
"""

import argparse
import logging
from logging.handlers import RotatingFileHandler
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduler.jobs.slow_stealth_scraper import SlowStealthScraper
from persistence.database import Database


def setup_logging(verbose: bool = False):
    """Configure logging with rotation to prevent huge log files."""
    level = logging.DEBUG if verbose else logging.INFO

    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s %(levelname)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # Rotating file handler - max 10MB per file, keep 5 backups (50MB total)
    os.makedirs('data', exist_ok=True)
    file_handler = RotatingFileHandler(
        'data/stealth_scraper.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def main():
    parser = argparse.ArgumentParser(description='Continuous stealth founder scraper')
    parser.add_argument('--fast', action='store_true', help='Faster delays for testing (30s/45s)')
    parser.add_argument('--slow', action='store_true', help='Slower delays to avoid blocks (120s/180s)')
    parser.add_argument('--search-delay', type=int, default=60, help='Seconds between searches (default: 60)')
    parser.add_argument('--scrape-delay', type=int, default=90, help='Seconds between scrapes (default: 90)')
    parser.add_argument('--iterations', type=int, default=None, help='Max iterations (default: unlimited)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    parser.add_argument('--stats', action='store_true', help='Show current stats and exit')
    args = parser.parse_args()

    # Ensure data directory exists
    os.makedirs('data', exist_ok=True)

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    # Determine delays
    if args.fast:
        search_delay, scrape_delay = 30, 45
    elif args.slow:
        search_delay, scrape_delay = 120, 180
    else:
        search_delay, scrape_delay = args.search_delay, args.scrape_delay

    # Connect to database
    db = Database('handelsregister.db')

    try:
        scraper = SlowStealthScraper(
            db=db,
            search_delay=search_delay,
            scrape_delay=scrape_delay,
        )

        # Show stats only
        if args.stats:
            stats = scraper.get_stats()
            print("\nStealth Scraper Stats (DACH Region):")
            print("=" * 50)
            print(f"  Founders in DB:      {stats['total_in_db']}")
            print(f"  High confidence:     {stats['high_confidence']}")
            print(f"  Emerged from stealth:{stats.get('emerged', 0)}")
            print(f"  Profile changed:     {stats.get('profile_changed', 0)}")
            print(f"  Needs re-check:      {stats.get('needs_recheck', 0)}")
            print("-" * 50)
            print(f"  Pending URLs:        {stats['pending_urls']}")
            print(f"  Total searches:      {stats['total_searches']}")
            print(f"  Total scrapes:       {stats['total_scrapes']}")
            print(f"  Founders found:      {stats['total_founders_found']}")
            print(f"  Skipped (non-DACH):  {stats['skipped_non_german']}")
            print(f"  Skipped (low conf):  {stats['skipped_low_confidence']}")
            print("-" * 50)
            print(f"  Current query:       {stats['current_query'][:50]}...")
            print(f"  Last search:         {stats['last_search_at']}")
            print(f"  Last scrape:         {stats['last_scrape_at']}")
            return

        # Run continuous scraper
        logger.info("=" * 60)
        logger.info("STEALTH FOUNDER SCRAPER - DACH REGION")
        logger.info("=" * 60)
        logger.info(f"Search delay: {search_delay}s, Scrape delay: {scrape_delay}s")
        logger.info(f"Iterations: {'unlimited' if args.iterations is None else args.iterations}")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)

        scraper.run_continuous(max_iterations=args.iterations)

    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        db.close()
        logger.info("Database connection closed")


if __name__ == '__main__':
    main()
