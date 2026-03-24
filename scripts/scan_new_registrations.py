#!/usr/bin/env python3
"""
New Registration Scanner — Discover tech startups from Handelsregister
using two complementary strategies:

1. **Register number scan** (--mode register, default):
   HRB numbers are sequential per court. We track the highest seen number
   and scan upward to find newly registered companies. This is the most
   efficient mode: 1 request = 1 company, directly targeting the NEWEST
   registrations. Uses a DB-stored high-water mark per court.

2. **PLZ prefix scan** (--mode plz):
   Keyword-free search by city + legal form + postal code prefix. Good for
   initial broad coverage of a city but less efficient (returns alphabetically
   sorted results, not chronological).

Both modes apply BrandNameScorer + AI keyword filter to identify likely
tech startups, then insert qualifying companies into the database.

Usage:
    # Register scan (default) — scan newest companies from watermark
    python scripts/scan_new_registrations.py --dry-run
    python scripts/scan_new_registrations.py --db handelsregister.db

    # Register scan with custom start number (sets watermark)
    python scripts/scan_new_registrations.py --start-number 283000 --dry-run

    # Register scan for specific courts only
    python scripts/scan_new_registrations.py --courts Berlin München --dry-run

    # Find the current highest HRB number (binary search)
    python scripts/scan_new_registrations.py --find-highest --courts Berlin

    # PLZ prefix scan mode (broad coverage)
    python scripts/scan_new_registrations.py --mode plz --dry-run
    python scripts/scan_new_registrations.py --mode plz --cities Berlin München --dry-run
    python scripts/scan_new_registrations.py --mode plz --plz-prefixes 101 102 103 --dry-run

    # Show current watermarks
    python scripts/scan_new_registrations.py --show-watermarks
"""

import argparse
import logging
import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persistence.database import Database
from processing.brand_name_scorer import BrandNameScorer
from processing.filters import AIRoboticsFilter, extract_legal_form
from processing.startup_scorer import StartupScorer
from sources.bundesapi import BundesAPISource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-6s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================================
# Court configs for register number scanning
# ============================================================================

# Court code → config for register number scanning
# HRB numbers are sequential per court; higher = more recently registered
COURT_CONFIGS = {
    "Berlin": {
        "court_code": "F1103",  # Amtsgericht Charlottenburg
        "state": "be",
        "estimated_max_hrb": 285000,  # Approximate current max (Feb 2026)
    },
    "München": {
        "court_code": "D2601",  # Amtsgericht München
        "state": "by",
        "estimated_max_hrb": 310000,  # Approximate current max (Feb 2026)
    },
}

# ============================================================================
# PLZ prefix configs (for --mode plz)
# ============================================================================

# Berlin PLZs: 10115-14199 (prefixes 101-141)
# München PLZs: 80331-81929 (prefixes 803-819)
BERLIN_PLZ_PREFIXES = [str(i) for i in range(101, 142)]
MUNICH_PLZ_PREFIXES = [str(i) for i in range(803, 820)]

CITY_CONFIGS = {
    "Berlin": {
        "state": "be",
        "plz_prefixes": BERLIN_PLZ_PREFIXES,
    },
    "München": {
        "state": "by",
        "plz_prefixes": MUNICH_PLZ_PREFIXES,
    },
}

RECHTSFORM_GMBH = "8"


# ============================================================================
# Shared scoring/insertion logic
# ============================================================================


def score_and_collect(
    result,
    brand_scorer: BrandNameScorer,
    ai_filter: AIRoboticsFilter,
    db: Optional[Database],
    dry_run: bool,
    stats: Dict,
    all_candidates: List,
):
    """
    Score a single search result and add to candidates if it passes.

    Returns True if the result was a candidate, False otherwise.
    """
    # Skip deleted/dissolved companies
    if result.status and result.status in ("deleted", "dissolved"):
        return False

    # Check if already in DB (by native company number)
    if not dry_run and db:
        existing = db.get_company_by_native_number(result.native_company_number)
        if existing:
            stats["already_in_db"] += 1
            return False

    # Extract legal form
    legal_form = extract_legal_form(result.name)

    # Score with BrandNameScorer
    brand_result = brand_scorer.score(result.name, city=result.city)

    # Also check AI keyword filter
    filter_result = ai_filter.filter_company(
        name=result.name,
        status=result.status or "currently registered",
    )

    if brand_result.is_likely_tech_startup:
        stats["passed_brand"] += 1
        all_candidates.append(
            {
                "result": result,
                "method": "brand_heuristic",
                "brand_result": brand_result,
                "filter_result": filter_result,
                "legal_form": legal_form,
            }
        )
        return True
    elif filter_result.passes:
        stats["passed_ai_keyword"] += 1
        all_candidates.append(
            {
                "result": result,
                "method": "ai_keyword",
                "brand_result": brand_result,
                "filter_result": filter_result,
                "legal_form": legal_form,
            }
        )
        return True
    else:
        stats["skipped_low_score"] += 1
        return False


def display_candidates(all_candidates: List):
    """Display all candidates found."""
    logger.info("")
    logger.info("=== Candidates Found: %d ===", len(all_candidates))
    logger.info("")

    for candidate in all_candidates:
        result = candidate["result"]
        method = candidate["method"]
        brand = candidate["brand_result"]
        filt = candidate["filter_result"]
        legal = candidate["legal_form"] or "?"

        if method == "brand_heuristic":
            signals = ", ".join(brand.signals[:4])
            logger.info(
                "  [BRAND  ] %s (%s, %s) score=%d [%s]",
                result.name,
                legal,
                result.city or "?",
                brand.total_score,
                signals,
            )
        else:
            kw = ", ".join(filt.matched_keywords[:3])
            logger.info(
                "  [KEYWORD] %s (%s, %s) AI=%d brand=%d kw=[%s]",
                result.name,
                legal,
                result.city or "?",
                filt.relevance_score,
                brand.total_score,
                kw,
            )


def insert_candidates(
    all_candidates: List,
    db: Database,
    startup_scorer: StartupScorer,
    stats: Dict,
):
    """Insert candidates into database."""
    logger.info("")
    logger.info("Inserting into database...")
    for candidate in all_candidates:
        result = candidate["result"]
        brand = candidate["brand_result"]
        filt = candidate["filter_result"]

        # Double-check dedup
        existing = db.get_company_by_native_number(result.native_company_number)
        if existing:
            stats["already_in_db"] += 1
            continue

        # Compute startup score
        ai_score = filt.relevance_score if filt.passes else 0
        startup_result = startup_scorer.score_company(
            name=result.name,
            legal_form=candidate["legal_form"],
            city=result.city,
            ai_relevance_score=ai_score,
        )
        classification = startup_scorer.classify(
            startup_result,
            ai_relevance_score=ai_score,
        )

        company_id = db.insert_company(
            company_number=f"regscan_{hash(result.native_company_number) & 0xFFFFFFFF:08x}",
            name=result.name,
            source="registration_scan",
            native_company_number=result.native_company_number,
            current_status=result.status or "currently registered",
            registry_court=result.registry_court,
            registry_type=result.registry_type,
            legal_form=candidate["legal_form"],
            city=result.city,
            state=result.state,
            ai_robotics_score=ai_score,
            climate_score=filt.climate_score if filt.passes else 0,
            matched_keywords=filt.matched_keywords if filt.passes else None,
            tech_categories=filt.tech_categories if filt.passes else None,
            startup_score=startup_result.total_score,
            startup_classification=classification,
            brand_name_score=brand.total_score,
        )

        priority = 0 if brand.is_likely_tech_startup else 2
        db.add_to_enrichment_queue(
            company_id,
            priority=priority,
            reason="new_from_registration_scan",
        )
        stats["inserted"] += 1


# ============================================================================
# Mode 1: Register Number Scan (default, most efficient)
# ============================================================================


def find_highest_hrb(
    source: BundesAPISource,
    court_code: str,
    court_name: str,
    estimated_max: int = 285000,
    max_requests: int = 15,
) -> int:
    """
    Binary search to find the current highest HRB number for a court.

    HRB numbers are sequential — if HRB N exists but HRB N+1 doesn't,
    then N is (approximately) the highest. We use binary search to
    narrow down within ~15 requests.

    Args:
        source: BundesAPISource instance
        court_code: Registry court code (e.g., 'F1103')
        court_name: Court name for logging
        estimated_max: Upper bound estimate for binary search
        max_requests: Maximum requests to use for binary search

    Returns:
        Highest HRB number found (approximate — within a small range)
    """
    logger.info("Binary search for highest HRB at %s (%s)...", court_name, court_code)

    low = 1
    high = estimated_max
    requests_used = 0
    highest_found = 0

    while low <= high and requests_used < max_requests:
        mid = (low + high) // 2
        logger.info("  Checking HRB %d (range %d-%d)...", mid, low, high)

        try:
            results = list(
                source.search(
                    register_number=str(mid),
                    register_court=court_code,
                    registry_types=["HRB"],
                    max_results=1,
                )
            )
            requests_used += 1
        except Exception as e:
            logger.error("  Error checking HRB %d: %s", mid, e)
            requests_used += 1
            # On error, try narrowing from the other side
            high = mid - 1
            continue

        if results:
            # HRB mid exists — highest is at least mid
            highest_found = max(highest_found, mid)
            low = mid + 1
            logger.info("  HRB %d EXISTS (%s)", mid, results[0].name[:50])
        else:
            # HRB mid doesn't exist — highest is below mid
            high = mid - 1
            logger.info("  HRB %d does NOT exist", mid)

    logger.info("  => Highest HRB found: %d (used %d requests)", highest_found, requests_used)
    return highest_found


def run_register_scan(
    db_path: str = "handelsregister.db",
    courts: Optional[List[str]] = None,
    start_number: Optional[int] = None,
    max_requests: int = 50,
    consecutive_misses: int = 10,
    dry_run: bool = False,
):
    """
    Scan for new companies by sequential HRB register number.

    This is the most efficient scan mode:
    - 1 request = 1 company lookup
    - Directly targets newest registrations (highest HRB numbers)
    - Uses DB-stored high-water mark to avoid re-scanning

    Strategy:
    1. Load watermark (last scanned HRB number) from DB per court
    2. Start scanning from watermark + 1, incrementing by 1
    3. For each found company, apply BrandNameScorer + AI filter
    4. Stop after N consecutive misses (default: 10 = reached the frontier)
    5. Update watermark in DB

    Args:
        db_path: Path to SQLite database
        courts: List of court names to scan (default: Berlin, München)
        start_number: Override start number (sets watermark)
        max_requests: Maximum portal requests per run
        consecutive_misses: Stop after this many consecutive empty results
        dry_run: If True, show results without DB changes
    """
    db = None if dry_run else Database(db_path)
    source = BundesAPISource()
    ai_filter = AIRoboticsFilter()
    startup_scorer = StartupScorer()
    brand_scorer = BrandNameScorer()

    if courts is None:
        courts = ["Berlin", "München"]

    stats = {
        "searches_performed": 0,
        "total_results": 0,
        "already_in_db": 0,
        "passed_brand": 0,
        "passed_ai_keyword": 0,
        "skipped_low_score": 0,
        "inserted": 0,
        "empty_lookups": 0,
    }

    all_candidates = []
    total_requests_left = max_requests

    for court_name in courts:
        config = COURT_CONFIGS.get(court_name)
        if not config:
            logger.warning("Unknown court: %s (known: %s)", court_name, list(COURT_CONFIGS.keys()))
            continue

        court_code = config["court_code"]

        # Determine start number
        if start_number is not None:
            watermark = start_number - 1  # Will start scanning from start_number
            logger.info("Using explicit start number: HRB %d", start_number)
        elif db:
            watermark = db.get_scan_watermark(court_code, "HRB")
            if watermark == 0:
                logger.info(
                    "No watermark for %s (%s) — use --start-number or --find-highest first", court_name, court_code
                )
                continue
            logger.info("Watermark for %s: HRB %d", court_name, watermark)
        else:
            # Dry run without DB — need explicit start number
            logger.warning("Dry run without watermark — use --start-number to set start position")
            continue

        current_number = watermark + 1
        misses = 0
        court_scanned = 0
        court_found = 0
        highest_seen = watermark

        logger.info("")
        logger.info("=== Scanning %s from HRB %d ===", court_name, current_number)

        while total_requests_left > 0 and misses < consecutive_misses:
            try:
                results = list(
                    source.search(
                        register_number=str(current_number),
                        register_court=court_code,
                        registry_types=["HRB"],
                        max_results=1,
                    )
                )
                stats["searches_performed"] += 1
                total_requests_left -= 1
                court_scanned += 1
            except Exception as e:
                logger.error("  Error looking up HRB %d: %s", current_number, e)
                stats["searches_performed"] += 1
                total_requests_left -= 1
                current_number += 1
                misses += 1
                continue

            if results:
                misses = 0  # Reset miss counter
                result = results[0]
                stats["total_results"] += 1
                court_found += 1
                highest_seen = current_number

                logger.info(
                    "  HRB %d: %s | %s",
                    current_number,
                    result.name[:60],
                    result.city or "?",
                )

                score_and_collect(
                    result,
                    brand_scorer,
                    ai_filter,
                    db,
                    dry_run,
                    stats,
                    all_candidates,
                )
            else:
                misses += 1
                stats["empty_lookups"] += 1
                logger.debug("  HRB %d: empty (miss %d/%d)", current_number, misses, consecutive_misses)

            current_number += 1

        # Report for this court
        if misses >= consecutive_misses:
            logger.info(
                "  Reached %d consecutive misses at HRB %d — frontier reached", consecutive_misses, current_number - 1
            )
        if total_requests_left <= 0:
            logger.info("  Reached max requests limit")

        logger.info(
            "  %s: scanned %d numbers, found %d companies, watermark: %d → %d",
            court_name,
            court_scanned,
            court_found,
            watermark,
            highest_seen,
        )

        # Update watermark
        if not dry_run and db and highest_seen > watermark:
            db.set_scan_watermark(
                court_code=court_code,
                last_scanned_number=highest_seen,
                registry_type="HRB",
                scanned_count=court_scanned,
                found_count=court_found,
            )
            logger.info("  Updated watermark for %s to HRB %d", court_name, highest_seen)

        if total_requests_left <= 0:
            break

    # Display and insert candidates
    display_candidates(all_candidates)

    if not dry_run and db:
        insert_candidates(all_candidates, db, startup_scorer, stats)

    # Print summary
    logger.info("")
    logger.info("=== Summary (Register Scan) ===")
    logger.info("Requests used:          %d / %d", stats["searches_performed"], max_requests)
    logger.info("Companies found:        %d", stats["total_results"])
    logger.info("Empty lookups:          %d", stats["empty_lookups"])
    logger.info("Already in DB:          %d", stats["already_in_db"])
    logger.info("---")
    logger.info("Passed brand heuristic: %d", stats["passed_brand"])
    logger.info("Passed AI keywords:     %d", stats["passed_ai_keyword"])
    logger.info("Skipped (low score):    %d", stats["skipped_low_score"])
    if not dry_run:
        logger.info("Inserted:               %d", stats["inserted"])
    else:
        logger.info("")
        logger.info("DRY RUN - no changes made")

    if db:
        db.close()

    return stats


# ============================================================================
# Mode 2: PLZ Prefix Scan (broad coverage)
# ============================================================================


def run_plz_scan(
    db_path: str = "handelsregister.db",
    cities: Optional[List[str]] = None,
    plz_prefixes: Optional[List[str]] = None,
    max_requests: int = 20,
    dry_run: bool = False,
):
    """
    Scan Handelsregister for tech startups using keyword-free search
    by city + legal form + postal code prefix.

    Strategy:
    1. Search for GmbH companies by city + postal code prefix (no keywords)
    2. Apply BrandNameScorer to identify likely tech startups
    3. Also check AI keyword filter (catch companies with keywords)
    4. Insert qualifying companies into DB with source='registration_scan'
    """
    db = None if dry_run else Database(db_path)
    source = BundesAPISource()
    ai_filter = AIRoboticsFilter()
    startup_scorer = StartupScorer()
    brand_scorer = BrandNameScorer()

    if cities is None:
        cities = ["Berlin", "München"]

    stats = {
        "searches_performed": 0,
        "total_results": 0,
        "already_in_db": 0,
        "passed_brand": 0,
        "passed_ai_keyword": 0,
        "skipped_low_score": 0,
        "inserted": 0,
    }

    all_candidates = []

    for city_name in cities:
        config = CITY_CONFIGS.get(city_name)
        if not config:
            logger.warning("Unknown city: %s (known: %s)", city_name, list(CITY_CONFIGS.keys()))
            continue

        prefixes = plz_prefixes or config["plz_prefixes"]
        state_code = config["state"]

        logger.info("=== Scanning %s (%d PLZ prefixes) ===", city_name, len(prefixes))

        for plz_prefix in prefixes:
            if stats["searches_performed"] >= max_requests:
                logger.info("Reached max requests (%d), stopping", max_requests)
                break

            logger.info("  Searching PLZ %s* in %s...", plz_prefix, city_name)

            try:
                results = list(
                    source.search(
                        city=city_name,
                        legal_form_code=RECHTSFORM_GMBH,
                        postal_code=f"{plz_prefix}*",
                        states=[state_code],
                        registry_types=["HRB"],
                        max_results=100,
                        results_per_page=100,
                    )
                )
                stats["searches_performed"] += 1
            except Exception as e:
                logger.error("  Error searching PLZ %s*: %s", plz_prefix, e)
                stats["searches_performed"] += 1
                continue

            logger.info("  Found %d results", len(results))
            stats["total_results"] += len(results)

            for result in results:
                score_and_collect(
                    result,
                    brand_scorer,
                    ai_filter,
                    db,
                    dry_run,
                    stats,
                    all_candidates,
                )

        if stats["searches_performed"] >= max_requests:
            break

    # Display and insert candidates
    display_candidates(all_candidates)

    if not dry_run and db:
        insert_candidates(all_candidates, db, startup_scorer, stats)

    # Print summary
    logger.info("")
    logger.info("=== Summary (PLZ Scan) ===")
    logger.info("Searches performed:     %d / %d", stats["searches_performed"], max_requests)
    logger.info("Total results fetched:  %d", stats["total_results"])
    logger.info("Already in DB:          %d", stats["already_in_db"])
    logger.info("---")
    logger.info("Passed brand heuristic: %d", stats["passed_brand"])
    logger.info("Passed AI keywords:     %d", stats["passed_ai_keyword"])
    logger.info("Skipped (low score):    %d", stats["skipped_low_score"])
    if not dry_run:
        logger.info("Inserted:               %d", stats["inserted"])
    else:
        logger.info("")
        logger.info("DRY RUN - no changes made")

    if db:
        db.close()

    return stats


# ============================================================================
# CLI
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Scan Handelsregister for new tech startup registrations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register scan (default) — scan newest HRB numbers
  %(prog)s --start-number 283000 --dry-run
  %(prog)s --courts Berlin --max-requests 50

  # Find the frontier (highest HRB number)
  %(prog)s --find-highest --courts Berlin

  # PLZ prefix scan (broad coverage)
  %(prog)s --mode plz --cities Berlin --dry-run

  # Show current watermarks
  %(prog)s --show-watermarks
        """,
    )

    # Mode selection
    parser.add_argument(
        "--mode",
        choices=["register", "plz"],
        default="register",
        help='Scan mode: "register" scans sequential HRB numbers (default), "plz" scans by postal code prefix',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show results without inserting into database",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=50,
        help="Maximum portal requests per run (default: 50)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="handelsregister.db",
        help="Path to SQLite database (default: handelsregister.db)",
    )

    # Register scan options
    reg_group = parser.add_argument_group("register scan options")
    reg_group.add_argument(
        "--courts",
        nargs="+",
        default=None,
        help="Courts to scan (default: Berlin München)",
    )
    reg_group.add_argument(
        "--start-number",
        type=int,
        default=None,
        help="Override start HRB number (sets the scanning position)",
    )
    reg_group.add_argument(
        "--consecutive-misses",
        type=int,
        default=10,
        help="Stop after N consecutive empty HRB lookups (default: 10)",
    )
    reg_group.add_argument(
        "--find-highest",
        action="store_true",
        help="Binary search to find the current highest HRB number per court",
    )
    reg_group.add_argument(
        "--show-watermarks",
        action="store_true",
        help="Show current scan watermarks and exit",
    )

    # PLZ scan options
    plz_group = parser.add_argument_group("PLZ scan options")
    plz_group.add_argument(
        "--cities",
        nargs="+",
        default=None,
        help="Cities to scan in PLZ mode (default: Berlin München)",
    )
    plz_group.add_argument(
        "--plz-prefixes",
        nargs="+",
        default=None,
        help="Override PLZ prefixes (e.g., 101 102 103)",
    )

    args = parser.parse_args()

    # Handle --show-watermarks
    if args.show_watermarks:
        db = Database(args.db)
        states = db.get_all_scan_states()
        if not states:
            logger.info("No scan watermarks found. Run --find-highest first.")
        else:
            logger.info("=== Scan Watermarks ===")
            for s in states:
                court_name = "?"
                for name, cfg in COURT_CONFIGS.items():
                    if cfg["court_code"] == s["court_code"]:
                        court_name = name
                        break
                logger.info(
                    "  %s (%s %s): last_scanned=%d, total_scanned=%d, total_found=%d, last_scan=%s",
                    court_name,
                    s["court_code"],
                    s["registry_type"],
                    s["last_scanned_number"],
                    s["total_scanned"],
                    s["total_found"],
                    s["last_scan_at"] or "never",
                )
        db.close()
        return

    # Handle --find-highest
    if args.find_highest:
        source = BundesAPISource()
        courts = args.courts or ["Berlin", "München"]
        db = None if args.dry_run else Database(args.db)

        for court_name in courts:
            config = COURT_CONFIGS.get(court_name)
            if not config:
                logger.warning("Unknown court: %s", court_name)
                continue

            highest = find_highest_hrb(
                source=source,
                court_code=config["court_code"],
                court_name=court_name,
                estimated_max=config["estimated_max_hrb"],
                max_requests=min(args.max_requests, 20),
            )

            if db and highest > 0:
                current_wm = db.get_scan_watermark(config["court_code"], "HRB")
                if highest > current_wm:
                    db.set_scan_watermark(
                        court_code=config["court_code"],
                        last_scanned_number=highest,
                        registry_type="HRB",
                    )
                    logger.info("Set watermark for %s to HRB %d", court_name, highest)
                else:
                    logger.info("Existing watermark %d >= found %d, keeping existing", current_wm, highest)

        if db:
            db.close()
        return

    # Run the selected scan mode
    if args.mode == "register":
        run_register_scan(
            db_path=args.db,
            courts=args.courts,
            start_number=args.start_number,
            max_requests=args.max_requests,
            consecutive_misses=args.consecutive_misses,
            dry_run=args.dry_run,
        )
    elif args.mode == "plz":
        run_plz_scan(
            db_path=args.db,
            cities=args.cities,
            plz_prefixes=args.plz_prefixes,
            max_requests=args.max_requests,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
