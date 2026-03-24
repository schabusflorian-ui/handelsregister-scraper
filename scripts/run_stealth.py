#!/usr/bin/env python3
"""
Stealth founder discovery - runs 24/7 extracting founders from search snippets.

Run with caffeinate to prevent sleep:
    caffeinate -i python3 run_stealth.py

Or in background:
    caffeinate -i python3 run_stealth.py &> stealth.log &

State is saved to data/stealth_scraper_state.json - survives restarts.
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(description="Stealth founder discovery")
    parser.add_argument("--iterations", "-n", type=int, default=None, help="Max iterations (default: unlimited)")
    parser.add_argument("--delay", "-d", type=int, default=180, help="Seconds between searches (default: 180)")
    parser.add_argument("--reset-state", action="store_true", help="Clear state and start fresh")
    parser.add_argument(
        "--engine",
        "-e",
        type=str,
        default="ddgs",
        choices=["ddgs", "serper", "curl", "brave", "ddg", "rotate"],
        help="Search engine: ddgs (default, Bing-backed), serper (Google API), curl, brave, ddg, or rotate",
    )
    parser.add_argument("--report", action="store_true", help="Print query yield report and exit (no scraping)")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Only find recently indexed profiles (past month). Use for incremental runs.",
    )
    parser.add_argument(
        "--officers",
        action="store_true",
        help="Enable Handelsregister officer cross-reference (off by default, enable once new registrations flow in)",
    )
    parser.add_argument(
        "--sync", action="store_true", help="Sync with Railway before starting (pull companies/officers, push founders)"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Clean up existing founders (fix names, recalculate scores, remove junk) and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Silence noisy third-party loggers (rustls, h2, hyper from primp)
    for noisy_logger in ("rustls", "h2", "hyper_util", "cookie_store", "primp"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    db_path = project_root / "handelsregister.db"
    state_file = project_root / "data" / "stealth_scraper_state.json"

    # Ensure data directory exists
    state_file.parent.mkdir(exist_ok=True)

    # Reset state if requested
    if args.reset_state and state_file.exists():
        state_file.unlink()
        print("State reset - starting fresh")

    # Sync with Railway if requested
    if args.sync:
        try:
            from scripts.sync_db import sync

            sync(str(db_path), pull=True, push=True, days=30)
        except Exception as e:
            print(f"Sync failed: {e}")
            print("Continuing without sync...\n")

    print("=" * 60)
    print("STEALTH FOUNDER DISCOVERY")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print(f"State file: {state_file}")
    print(f"Search delay: {args.delay}s")
    print(f"Search engine: {args.engine}")
    print(f"Fresh mode: {'ON (past month only)' if args.fresh else 'OFF (all time)'}")
    print(f"Officer crossref: {'ON' if args.officers else 'OFF (use --officers to enable)'}")
    print(f"Railway sync: {'done' if args.sync else 'OFF (use --sync to pull companies from Railway)'}")
    print(f"Iterations: {'unlimited' if args.iterations is None else args.iterations}")
    print()
    from scheduler.jobs.slow_stealth_scraper import STEALTH_QUERIES, SlowStealthScraper

    print("Extracts founders from search snippets.")
    print(f"{len(STEALTH_QUERIES)} search queries, rotates through all of them.")
    print()
    print("Press Ctrl+C to stop (state is auto-saved)")
    print()
    from persistence.database import Database

    db = Database(str(db_path))
    try:
        scraper = SlowStealthScraper(
            db=db,
            state_file=str(state_file),
            search_delay=args.delay,
            scrape_delay=120,  # LinkedIn scraping mostly disabled
            search_engine=args.engine,
            fresh_mode=args.fresh,
            include_officers=args.officers,
        )

        # Report mode: print query yield stats and exit
        if args.report:
            scraper.print_query_report()
            db.close()
            return

        # Cleanup mode: fix existing data and exit
        if args.cleanup:
            scraper.cleanup_existing_founders()
            db.close()
            return

        # Show current state
        stats = scraper.get_stats()
        print(f"Resuming from query #{stats.get('query_index', 0) + 1}/{len(STEALTH_QUERIES)}")
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
            print("\n" + "=" * 50)
            print("SESSION STATS")
            print("=" * 50)
            print(f"  Queries run: {stats.get('total_searches', 0)}")
            print(f"  Founders found: {stats.get('total_founders_found', 0)}")
            print(f"  Next query: #{stats.get('query_index', 0) + 1}/{len(STEALTH_QUERIES)}")
            print("=" * 50)
        except:
            pass
        db.close()


if __name__ == "__main__":
    main()
