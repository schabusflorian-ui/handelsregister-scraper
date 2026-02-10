#!/usr/bin/env python3
"""
Stealth founder discovery - runs 24/7 extracting founders from search snippets.

Run with caffeinate to prevent sleep:
    caffeinate -i python3 run_stealth.py

Or in background:
    caffeinate -i python3 run_stealth.py &> stealth.log &

State is saved to data/stealth_scraper_state.json - survives restarts.
"""

import sys
import logging
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(description="Stealth founder discovery")
    parser.add_argument(
        "--iterations", "-n", type=int, default=None,
        help="Max iterations (default: unlimited)"
    )
    parser.add_argument(
        "--delay", "-d", type=int, default=90,
        help="Seconds between searches (default: 90)"
    )
    parser.add_argument(
        "--reset-state", action="store_true",
        help="Clear state and start fresh"
    )
    parser.add_argument(
        "--engine", "-e", type=str, default="brave",
        choices=["brave", "ddg", "rotate"],
        help="Search engine: brave (default, less blocking), ddg, or rotate"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    db_path = project_root / "handelsregister.db"
    state_file = project_root / "data" / "stealth_scraper_state.json"

    # Ensure data directory exists
    state_file.parent.mkdir(exist_ok=True)

    # Reset state if requested
    if args.reset_state and state_file.exists():
        state_file.unlink()
        print("State reset - starting fresh")

    print("=" * 60)
    print("STEALTH FOUNDER DISCOVERY")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print(f"State file: {state_file}")
    print(f"Search delay: {args.delay}s")
    print(f"Search engine: {args.engine}")
    print(f"Iterations: {'unlimited' if args.iterations is None else args.iterations}")
    print()
    print("Extracts founders from search snippets.")
    print("98 search queries, rotates through all of them.")
    print()
    print("Press Ctrl+C to stop (state is auto-saved)")
    print()

    from scheduler.jobs.slow_stealth_scraper import SlowStealthScraper
    from persistence.database import Database

    db = Database(str(db_path))
    try:
        scraper = SlowStealthScraper(
            db=db,
            state_file=str(state_file),
            search_delay=args.delay,
            scrape_delay=120,  # LinkedIn scraping mostly disabled
            search_engine=args.engine,
        )

        # Show current state
        stats = scraper.get_stats()
        print(f"Resuming from query #{stats.get('query_index', 0) + 1}/98")
        print(f"Total founders so far: {stats.get('total_founders_found', 0)}")
        print()

        # Run continuously
        scraper.run_continuous(max_iterations=args.iterations)

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    finally:
        # Print final stats
        try:
            stats = scraper.get_stats()
            print("\n" + "="*50)
            print("SESSION STATS")
            print("="*50)
            print(f"  Queries run: {stats.get('total_searches', 0)}")
            print(f"  Founders found: {stats.get('total_founders_found', 0)}")
            print(f"  Next query: #{stats.get('query_index', 0) + 1}/98")
            print("="*50)
        except:
            pass
        db.close()


if __name__ == "__main__":
    main()
