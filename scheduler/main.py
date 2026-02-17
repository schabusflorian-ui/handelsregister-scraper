#!/usr/bin/env python3
"""
Scheduler entry point for continuous Handelsregister monitoring.

Usage:
    python -m scheduler.main                    # Start scheduler
    python -m scheduler.main --run-now          # Start and run jobs immediately
    python -m scheduler.main --discovery-only   # Run single discovery job
    python -m scheduler.main --backfill-only    # Run single backfill job
    python -m scheduler.main --regscan-only     # Run single registration scan job
    python -m scheduler.main --status           # Show status
"""

import sys
import argparse
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scheduler.scheduler import run_scheduler, HandelsregisterScheduler
from scheduler.jobs.discovery_job import run_discovery_job
from scheduler.jobs.backfill_job import run_backfill_job
from scheduler.jobs.registration_scan_job import run_registration_scan_job
from scheduler.rate_limiter import print_rate_limit_status


def main():
    parser = argparse.ArgumentParser(
        description='Handelsregister Scheduler - Continuous monitoring'
    )
    parser.add_argument(
        '--db', default='handelsregister.db',
        help='Database file path'
    )
    parser.add_argument(
        '--discovery-interval', type=int, default=2,
        help='Hours between discovery job runs (default: 2)'
    )
    parser.add_argument(
        '--run-now', action='store_true',
        help='Run jobs immediately when starting'
    )
    parser.add_argument(
        '--discovery-only', action='store_true',
        help='Run single discovery job and exit'
    )
    parser.add_argument(
        '--backfill-only', action='store_true',
        help='Run single backfill job and exit'
    )
    parser.add_argument(
        '--regscan-only', action='store_true',
        help='Run single registration scan job and exit'
    )
    parser.add_argument(
        '--status', action='store_true',
        help='Show scheduler status and exit'
    )
    parser.add_argument(
        '--rate-status', action='store_true',
        help='Show rate limiter status and exit'
    )
    parser.add_argument(
        '--max-requests', type=int, default=20,
        help='Maximum requests per job run (default: 20)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Run without saving to database'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose logging'
    )

    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    # Silence noisy third-party loggers (rustls, h2, hyper from primp)
    for noisy_logger in ('rustls', 'h2', 'hyper_util', 'cookie_store', 'primp'):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # Handle status commands
    if args.rate_status:
        print_rate_limit_status(args.db)
        return

    if args.status:
        from persistence.database import Database
        from scheduler.rate_limiter import PersistentRateLimiter

        db = Database(args.db)
        rate_limiter = PersistentRateLimiter(args.db)

        print("\n=== Handelsregister Scheduler Status ===\n")

        # Rate limit status
        rate_state = rate_limiter.get_state()
        print("Rate Limiter:")
        print(f"  Tokens available: {rate_state.tokens_available:.1f} / 60")
        print(f"  Requests this hour: {rate_state.requests_this_hour}")
        print(f"  Can make request: {'Yes' if rate_state.can_request else 'No'}")

        # Database stats
        stats = db.get_statistics()
        print("\nDatabase:")
        print(f"  Total companies: {stats.get('total_companies', 0):,}")
        print(f"  From bundesapi: {stats.get('companies_by_source', {}).get('bundesapi', 0):,}")

        # Backfill progress
        try:
            conn = db._get_connection()
            total = conn.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM backfill_state WHERE status = 'completed'"
            ).fetchone()[0]
            print("\nBackfill Progress:")
            print(f"  {completed}/{total} combinations ({completed/total*100:.1f}%)" if total > 0 else "  Not started")
            conn.close()
        except:
            print("\nBackfill Progress: Not initialized")

        # Registration scan watermarks
        try:
            scan_states = db.get_all_scan_states()
            if scan_states:
                print("\nRegistration Scan Watermarks:")
                for s in scan_states:
                    print(f"  {s['court_code']} ({s['registry_type']}): "
                          f"last_scanned={s['last_scanned_number']}, "
                          f"total_scanned={s['total_scanned']}, "
                          f"total_found={s['total_found']}, "
                          f"last_scan={s['last_scan_at'] or 'never'}")
            else:
                print("\nRegistration Scan: No watermarks (will auto-bootstrap on first run)")
        except:
            print("\nRegistration Scan: Not initialized")

        # Recent jobs
        try:
            conn = db._get_connection()
            recent_jobs = conn.execute("""
                SELECT job_type, started_at, status, companies_new, requests_used
                FROM job_runs
                ORDER BY id DESC
                LIMIT 5
            """).fetchall()

            if recent_jobs:
                print("\nRecent Jobs:")
                for job in recent_jobs:
                    print(f"  {job['started_at'][:16]} - {job['job_type']}: "
                          f"{job['companies_new']} new, {job['requests_used']} requests, "
                          f"status={job['status']}")
            conn.close()
        except:
            pass

        db.close()
        return

    # Handle single job runs
    if args.discovery_only:
        print("Running single discovery job...")
        stats = run_discovery_job(
            db_path=args.db,
            max_requests=args.max_requests,
            dry_run=args.dry_run,
        )
        print(f"\nResults:")
        print(f"  Companies found: {stats['companies_found']}")
        print(f"  New companies: {stats['companies_new']}")
        print(f"  Requests used: {stats['requests_used']}")
        print(f"  Keywords completed: {stats['keywords_completed']}/{stats['keywords_total']}")
        return

    if args.backfill_only:
        print("Running single backfill job...")
        stats = run_backfill_job(
            db_path=args.db,
            max_requests=args.max_requests,
            dry_run=args.dry_run,
        )
        print(f"\nResults:")
        print(f"  Combinations processed: {stats['combinations_processed']}")
        print(f"  Companies found: {stats['companies_found']}")
        print(f"  New companies: {stats['companies_new']}")
        print(f"  Requests used: {stats['requests_used']}")
        print(f"  Overall progress: {stats['progress_percent']:.1f}%")
        return

    if args.regscan_only:
        print("Running single registration scan job...")
        stats = run_registration_scan_job(
            db_path=args.db,
            max_requests=args.max_requests,
        )
        print(f"\nResults:")
        print(f"  Companies found: {stats['companies_found']}")
        print(f"  New companies: {stats['companies_new']}")
        print(f"  Requests used: {stats['requests_used']}")
        print(f"  Courts scanned: {stats['courts_scanned']}")
        print(f"  Empty lookups: {stats['empty_lookups']}")
        return

    # Start full scheduler
    print("Starting Handelsregister Scheduler")
    print("=" * 50)
    print(f"Database: {args.db}")
    print(f"Discovery interval: {args.discovery_interval} hours")
    print(f"Max requests per job: {args.max_requests}")
    print()

    run_scheduler(
        db_path=args.db,
        discovery_interval=args.discovery_interval,
        run_discovery_now=args.run_now,
        run_backfill_now=args.run_now,
    )


if __name__ == '__main__':
    main()
