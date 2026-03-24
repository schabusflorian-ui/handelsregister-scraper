"""
Registration Scan Job — Discover newest companies by sequential HRB number.

HRB register numbers are assigned sequentially per court. This job scans
upward from a stored high-water mark to find newly registered companies,
then applies BrandNameScorer + AI keyword filter to identify tech startups.

Efficiency: 1 request = 1 company (vs keyword search: 2 requests per keyword).
Coverage: Catches brand-name startups that keywords miss.

Auto-bootstraps on first run: if no watermark exists for a court, runs a
binary search (~15 requests) to find the current frontier.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from persistence.database import Database
from processing.brand_name_scorer import BrandNameScorer
from processing.filters import AIRoboticsFilter, extract_legal_form
from processing.startup_scorer import StartupScorer
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import BundesAPISource

logger = logging.getLogger(__name__)


class RegistrationScanJob:
    """
    Job to discover newly registered companies by scanning sequential
    HRB register numbers from a high-water mark.

    Uses the shared PersistentRateLimiter to stay within the 60 req/hr
    limit alongside other scheduler jobs.
    """

    # Court configs — HRB numbers are sequential per court
    COURT_CONFIGS = {
        "Berlin": {
            "court_code": "F1103",  # Amtsgericht Charlottenburg
            "state": "be",
            "estimated_max_hrb": 285000,
        },
        "München": {
            "court_code": "D2601",  # Amtsgericht München
            "state": "by",
            "estimated_max_hrb": 310000,
        },
    }

    def __init__(
        self,
        db: Database,
        rate_limiter: PersistentRateLimiter,
        max_requests: int = 40,
        courts: Optional[List[str]] = None,
        consecutive_misses: int = 10,
    ):
        """
        Initialize registration scan job.

        Args:
            db: Database instance (caller manages lifecycle)
            rate_limiter: Shared persistent rate limiter
            max_requests: Maximum requests for this job run
            courts: Courts to scan (default: Berlin, München)
            consecutive_misses: Stop scanning a court after N empty lookups
        """
        self.db = db
        self.rate_limiter = rate_limiter
        self.max_requests = max_requests
        self.courts = courts or ["Berlin", "München"]
        self.consecutive_misses = consecutive_misses

        self.ai_filter = AIRoboticsFilter()
        self.startup_scorer = StartupScorer()
        self.brand_scorer = BrandNameScorer()
        self.source = None  # Lazily initialized

    def run(self) -> Dict[str, Any]:
        """
        Execute the registration scan.

        Iterates configured courts, scans sequential HRB numbers from
        watermark, scores and inserts qualifying companies.

        Returns:
            Statistics dict with keys: companies_found, companies_new,
            requests_used, courts_scanned, empty_lookups
        """
        self.source = BundesAPISource()

        stats = {
            "companies_found": 0,
            "companies_new": 0,
            "requests_used": 0,
            "courts_scanned": 0,
            "empty_lookups": 0,
        }

        requests_remaining = self.max_requests

        for court_name in self.courts:
            config = self.COURT_CONFIGS.get(court_name)
            if not config:
                logger.warning("Unknown court: %s (known: %s)", court_name, list(self.COURT_CONFIGS.keys()))
                continue

            if requests_remaining <= 0:
                logger.info("Request budget exhausted, stopping")
                break

            # Allocate requests evenly across remaining courts
            remaining_courts = len(self.courts) - stats["courts_scanned"]
            court_budget = max(5, requests_remaining // max(1, remaining_courts))

            court_stats, court_requests = self._scan_court(
                court_name,
                config,
                court_budget,
            )

            stats["companies_found"] += court_stats["found"]
            stats["companies_new"] += court_stats["new"]
            stats["empty_lookups"] += court_stats["empty"]
            stats["requests_used"] += court_requests
            stats["courts_scanned"] += 1
            requests_remaining -= court_requests

        logger.info(
            "Registration scan complete: %d found, %d new, %d requests used",
            stats["companies_found"],
            stats["companies_new"],
            stats["requests_used"],
        )

        return stats

    def _scan_court(
        self,
        court_name: str,
        config: Dict,
        max_requests: int,
    ) -> Tuple[Dict, int]:
        """
        Scan one court from its watermark upward.

        Returns:
            (court_stats, requests_used)
        """
        court_code = config["court_code"]
        court_stats = {"found": 0, "new": 0, "empty": 0}
        requests_used = 0

        # Load or bootstrap watermark
        watermark = self.db.get_scan_watermark(court_code, "HRB")

        if watermark == 0:
            logger.info("No watermark for %s (%s), auto-bootstrapping...", court_name, court_code)

            # Reserve up to 15 requests for binary search
            bootstrap_budget = min(15, max_requests)
            highest = self._find_highest_hrb(
                court_code,
                config["estimated_max_hrb"],
                bootstrap_budget,
            )
            requests_used += bootstrap_budget

            if highest > 0:
                self.db.set_scan_watermark(court_code, highest, "HRB")
                watermark = highest
                logger.info("Bootstrapped watermark for %s: HRB %d", court_name, highest)
            else:
                logger.warning("Could not find frontier for %s, skipping", court_name)
                return court_stats, requests_used

            # Reduce remaining budget
            max_requests -= bootstrap_budget
            if max_requests <= 0:
                return court_stats, requests_used

        current_number = watermark + 1
        misses = 0
        highest_seen = watermark

        logger.info("Scanning %s from HRB %d (budget: %d requests)", court_name, current_number, max_requests)

        while requests_used < max_requests and misses < self.consecutive_misses:
            # Check shared rate limiter
            rate_state = self.rate_limiter.get_state()
            if rate_state.tokens_available < 1:
                logger.info("Insufficient rate limit tokens (%.1f), pausing", rate_state.tokens_available)
                break

            if not self.rate_limiter.acquire(count=1, block=False):
                logger.info("Could not acquire rate limit token, pausing")
                break

            try:
                results = list(
                    self.source.search(
                        register_number=str(current_number),
                        register_court=court_code,
                        registry_types=["HRB"],
                        max_results=1,
                    )
                )
                requests_used += 1
            except Exception as e:
                logger.error("Error looking up HRB %d at %s: %s", current_number, court_name, e)
                requests_used += 1
                misses += 1
                current_number += 1
                continue

            if results:
                misses = 0
                result = results[0]
                court_stats["found"] += 1
                highest_seen = current_number

                logger.info("  HRB %d: %s | %s", current_number, result.name[:60], result.city or "?")

                is_new = self._score_and_insert(result)
                if is_new:
                    court_stats["new"] += 1
            else:
                misses += 1
                court_stats["empty"] += 1
                logger.debug("  HRB %d: empty (miss %d/%d)", current_number, misses, self.consecutive_misses)

            current_number += 1

        # Log reason for stopping
        if misses >= self.consecutive_misses:
            logger.info(
                "  %s: frontier reached (HRB %d, %d consecutive misses)", court_name, current_number - 1, misses
            )
        elif requests_used >= max_requests:
            logger.info("  %s: budget exhausted", court_name)

        # Update watermark
        if highest_seen > watermark:
            self.db.set_scan_watermark(
                court_code=court_code,
                last_scanned_number=highest_seen,
                registry_type="HRB",
                scanned_count=court_stats["found"] + court_stats["empty"],
                found_count=court_stats["found"],
            )
            logger.info(
                "  %s: watermark %d → %d (%d found, %d new)",
                court_name,
                watermark,
                highest_seen,
                court_stats["found"],
                court_stats["new"],
            )

        return court_stats, requests_used

    def _find_highest_hrb(
        self,
        court_code: str,
        estimated_max: int,
        max_requests: int = 15,
    ) -> int:
        """
        Binary search for the current highest HRB number at a court.

        Args:
            court_code: Registry court code (e.g., 'F1103')
            estimated_max: Upper bound estimate
            max_requests: Max requests for binary search

        Returns:
            Highest HRB number found, or 0 if search failed
        """
        low = 1
        high = estimated_max
        requests_used = 0
        highest_found = 0

        while low <= high and requests_used < max_requests:
            mid = (low + high) // 2

            # Check rate limiter
            if not self.rate_limiter.acquire(count=1, block=False):
                logger.warning("Rate limit hit during binary search")
                break

            try:
                results = list(
                    self.source.search(
                        register_number=str(mid),
                        register_court=court_code,
                        registry_types=["HRB"],
                        max_results=1,
                    )
                )
                requests_used += 1
            except Exception as e:
                logger.error("Binary search error at HRB %d: %s", mid, e)
                requests_used += 1
                high = mid - 1
                continue

            if results:
                highest_found = max(highest_found, mid)
                low = mid + 1
                logger.debug("  HRB %d EXISTS (%s)", mid, results[0].name[:40])
            else:
                high = mid - 1
                logger.debug("  HRB %d does NOT exist", mid)

        logger.info("Binary search result: highest HRB = %d (%d requests)", highest_found, requests_used)
        return highest_found

    def _score_and_insert(self, result) -> bool:
        """
        Score a search result and insert if it qualifies as a potential startup.

        Returns True if company was newly inserted.
        """
        # Skip deleted/dissolved
        if result.status and result.status in ("deleted", "dissolved"):
            return False

        # Dedup by native company number
        existing = self.db.get_company_by_native_number(result.native_company_number)
        if existing:
            return False

        # Extract legal form
        legal_form = extract_legal_form(result.name)

        # Score with BrandNameScorer
        brand_result = self.brand_scorer.score(result.name, city=result.city)

        # Check AI keyword filter
        filter_result = self.ai_filter.filter_company(
            name=result.name,
            status=result.status or "currently registered",
        )

        # Must pass at least one filter
        if not brand_result.is_likely_tech_startup and not filter_result.passes:
            return False

        # Compute startup score
        ai_score = filter_result.relevance_score if filter_result.passes else 0
        climate_score = filter_result.climate_score if filter_result.passes else 0
        startup_result = self.startup_scorer.score_company(
            name=result.name,
            legal_form=legal_form,
            city=result.city,
            ai_relevance_score=ai_score,
            climate_score=climate_score,
        )
        classification = self.startup_scorer.classify(
            startup_result,
            ai_relevance_score=ai_score,
            climate_score=climate_score,
        )

        # Insert company
        company_id = self.db.insert_company(
            company_number=f"regscan_{hash(result.native_company_number) & 0xFFFFFFFF:08x}",
            name=result.name,
            source="registration_scan",
            native_company_number=result.native_company_number,
            current_status=result.status or "currently registered",
            registry_court=result.registry_court,
            registry_type=result.registry_type,
            legal_form=legal_form,
            city=result.city,
            state=result.state,
            ai_robotics_score=ai_score,
            climate_score=filter_result.climate_score if filter_result.passes else 0,
            matched_keywords=filter_result.matched_keywords if filter_result.passes else None,
            tech_categories=filter_result.tech_categories if filter_result.passes else None,
            startup_score=startup_result.total_score,
            startup_classification=classification,
            brand_name_score=brand_result.total_score,
        )

        # Queue for enrichment — high priority for brand-name startups
        priority = 0 if brand_result.is_likely_tech_startup else 2
        method = "brand" if brand_result.is_likely_tech_startup else "keyword"
        self.db.add_to_enrichment_queue(
            company_id,
            priority=priority,
            reason="new_from_registration_scan",
        )

        logger.info(
            "  NEW [%s] %s (brand=%d, AI=%d, startup=%s)",
            method.upper(),
            result.name[:50],
            brand_result.total_score,
            ai_score,
            classification,
        )

        return True


def run_registration_scan_job(
    db_path: str,
    max_requests: int = 40,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function to run registration scan job standalone.

    Args:
        db_path: Path to SQLite database
        max_requests: Maximum requests per run
        dry_run: Don't save to database (not yet implemented for this job)

    Returns:
        Statistics dict
    """
    db = Database(db_path)
    rate_limiter = PersistentRateLimiter(db_path)

    try:
        job = RegistrationScanJob(
            db=db,
            rate_limiter=rate_limiter,
            max_requests=max_requests,
        )
        return job.run()
    finally:
        db.close()
