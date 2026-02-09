#!/usr/bin/env python3
"""
Simple wrapper to run the stealth founder job.
Run from anywhere: python3 run_stealth.py

Uses the slow scraper which extracts data from search snippets
(no LinkedIn scraping needed - avoids 999 blocks).
"""

import sys
import logging
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    db_path = project_root / "handelsregister.db"

    print("=" * 60)
    print("STEALTH FOUNDER DISCOVERY (Snippet-based)")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print()
    print("This extracts founders from DuckDuckGo search snippets.")
    print("No LinkedIn scraping = no 999 blocks!")
    print()
    print("Running 5 iterations (press Ctrl+C to stop)...")
    print()

    from scheduler.jobs.slow_stealth_scraper import SlowStealthScraper
    from persistence.database import Database

    db = Database(str(db_path))
    try:
        scraper = SlowStealthScraper(
            db=db,
            search_delay=30,   # 30 seconds between searches
            scrape_delay=60,   # Skip LinkedIn scraping mostly
        )

        # Run 5 iterations (quick test)
        scraper.run_continuous(max_iterations=5)

        # Print final stats
        stats = scraper.get_stats()
        print("\n" + "="*50)
        print("FINAL STATS")
        print("="*50)
        print(f"  Queries run: {stats.get('queries_run', 0)}")
        print(f"  URLs found: {stats.get('urls_found', 0)}")
        print(f"  Founders stored: {stats.get('founders_stored', 0)}")
        print("="*50)

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        db.close()


if __name__ == "__main__":
    main()
