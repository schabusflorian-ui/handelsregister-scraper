#!/usr/bin/env python3
"""
Simple wrapper to run the stealth founder job.
Run from anywhere: python3 run_stealth.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from scheduler.jobs.stealth_founder_job import StealthFounderJob
from persistence.database import Database


def main():
    db_path = project_root / "handelsregister.db"

    print(f"Database: {db_path}")
    print("Starting stealth founder discovery...\n")

    db = Database(str(db_path))
    try:
        job = StealthFounderJob(
            db=db,
            max_queries=5,
            max_profiles_to_scrape=20,
            min_confidence=0.3,
        )
        result = job.run()

        print("\n" + "="*50)
        print("Results:")
        print(f"  Queries run: {result.get('queries_run', 0)}")
        print(f"  URLs found: {result.get('urls_found', 0)}")
        print(f"  New URLs: {result.get('new_urls', 0)}")
        print(f"  Profiles scraped: {result.get('profiles_scraped', 0)}")
        print(f"  Founders stored: {result.get('founders_stored', 0)}")
        print(f"  High confidence: {result.get('high_confidence', 0)}")
        if result.get('errors'):
            print(f"  Errors: {result['errors']}")
        print("="*50)

    finally:
        db.close()


if __name__ == "__main__":
    main()
