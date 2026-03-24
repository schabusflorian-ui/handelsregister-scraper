#!/usr/bin/env python3
"""
Officer LinkedIn enrichment - cross-references officers with LinkedIn profiles.

Searches for officer LinkedIn profiles via DuckDuckGo/Brave and extracts
career data (headline, previous companies) from search snippets.
Never hits linkedin.com directly.

Run with caffeinate to prevent sleep:
    caffeinate -i python3 run_officer_linkedin.py

Or limit to N iterations:
    python3 run_officer_linkedin.py -n 10

State is saved to data/officer_linkedin_state.json - survives restarts.
"""

import sys
import logging
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))


def main():
    parser = argparse.ArgumentParser(description="Officer LinkedIn enrichment")
    parser.add_argument(
        "--iterations", "-n", type=int, default=None,
        help="Max iterations (default: unlimited)",
    )
    parser.add_argument(
        "--delay", "-d", type=int, default=150,
        help="Seconds between searches (default: 150)",
    )
    parser.add_argument(
        "--engine", "-e", type=str, default="curl",
        choices=["brave", "curl", "rotate"],
        help="Search engine: curl (default), brave, or rotate",
    )
    parser.add_argument(
        "--min-confidence", type=float, default=0.40,
        help="Minimum match confidence (default: 0.40)",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print enrichment stats and exit",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    # Silence noisy third-party loggers (rustls, h2, hyper from primp)
    for noisy_logger in ('rustls', 'h2', 'hyper_util', 'cookie_store', 'primp'):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    db_path = project_root / "handelsregister.db"
    state_file = project_root / "data" / "officer_linkedin_state.json"

    # Ensure data directory exists
    state_file.parent.mkdir(exist_ok=True)

    from persistence.database import Database
    from scheduler.jobs.officer_linkedin_job import OfficerLinkedInEnrichmentJob

    print("=" * 60)
    print("OFFICER LINKEDIN ENRICHMENT")
    print("=" * 60)
    print()
    print(f"Database: {db_path}")
    print(f"State file: {state_file}")
    print(f"Search delay: {args.delay}s")
    print(f"Search engine: {args.engine}")
    print(f"Min confidence: {args.min_confidence}")
    print(f"Iterations: {'unlimited' if args.iterations is None else args.iterations}")
    print()

    db = Database(str(db_path))
    try:
        job = OfficerLinkedInEnrichmentJob(
            db=db,
            state_file=str(state_file),
            search_delay=args.delay,
            search_engine=args.engine,
            min_confidence=args.min_confidence,
        )

        # Stats mode
        if args.stats:
            stats = job.get_stats()
            print("Enrichment Statistics:")
            print(f"  Officers attempted:   {stats.get('total_attempted', 0)}")
            print(f"  With LinkedIn URL:    {stats.get('with_linkedin_url', 0)}")
            print(f"  Remaining to enrich:  {stats.get('remaining', 0)}")
            print(f"  Total searches:       {stats.get('total_searches', 0)}")
            print(f"  Total enriched:       {stats.get('total_enriched', 0)}")
            print(f"  Total no match:       {stats.get('total_no_match', 0)}")
            print(f"  Failed (skipped):     {stats.get('failed_count', 0)}")
            print(f"  Last search:          {stats.get('last_search_at', 'never')}")
            return

        # Show current state
        stats = job.get_stats()
        print(f"Officers remaining to enrich: {stats.get('remaining', 0)}")
        print(f"Already enriched: {stats.get('with_linkedin_url', 0)}")
        print()
        print("Extracts career data from search snippets (no direct LinkedIn access).")
        print("Press Ctrl+C to stop (state is auto-saved)")
        print()

        # Run continuously
        job.run_continuous(max_iterations=args.iterations)

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    finally:
        try:
            stats = job.get_stats()
            print("\n" + "=" * 50)
            print("SESSION STATS")
            print("=" * 50)
            print(f"  Searches run:    {stats.get('total_searches', 0)}")
            print(f"  Officers enriched: {stats.get('total_enriched', 0)}")
            print(f"  No match:        {stats.get('total_no_match', 0)}")
            print(f"  Remaining:       {stats.get('remaining', 0)}")
            print("=" * 50)
        except Exception:
            pass
        db.close()


if __name__ == "__main__":
    main()
