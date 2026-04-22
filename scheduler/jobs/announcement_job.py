"""
Announcement Monitoring Job - Monitor Registerbekanntmachungen for new startups.

This job:
1. Fetches recent announcements from handelsregister.de
2. Auto-discovers new AI/robotics startups from "Einreichung neuer Dokumente"
3. Tracks capital raises for companies already in our database
4. Links announcements to existing companies

Runs daily to catch new registrations and capital events.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from persistence.database import Database
from processing.brand_name_scorer import BrandNameScorer
from processing.filters import AIRoboticsFilter
from processing.startup_scorer import StartupScorer
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import Announcement, BundesAPISource

logger = logging.getLogger(__name__)


class AnnouncementMonitoringJob:
    """
    Job to monitor Registerbekanntmachungen for AI/robotics startups.

    Features:
    - Fetches announcements from the last N days
    - Discovers new AI startups from "Einreichung neuer Dokumente"
    - Detects capital raises for tracked companies
    - Links announcements to existing companies in database
    """

    # Announcement types that indicate new companies
    NEW_COMPANY_TYPES = ["neueintragung", "sonstiges"]

    # Announcement types that might indicate capital changes
    CAPITAL_TYPES = ["kapitalerhoehung", "kapitalherabsetzung", "sonstiges"]

    def __init__(
        self,
        db: Database,
        rate_limiter: Optional[PersistentRateLimiter] = None,
        max_requests: int = 10,
        lookback_days: int = 7,
        inline_website_lookup: bool = False,
        inline_website_min_score: int = 3,
    ):
        """
        Initialize announcement monitoring job.

        Args:
            db: Database instance
            rate_limiter: Optional rate limiter (creates internal one if not provided)
            max_requests: Maximum requests per job run
            lookback_days: Number of days to look back for announcements
            inline_website_lookup: If True, attempt a fast domain-guess
                website lookup for each newly inserted high-signal company.
                Leaves full scrape to WebsiteScrapeJob.
            inline_website_min_score: Minimum ai_robotics_score to trigger
                the inline lookup (default 3)
        """
        self.db = db
        self.rate_limiter = rate_limiter
        self.max_requests = max_requests
        self.lookback_days = lookback_days
        self.inline_website_lookup = inline_website_lookup
        self.inline_website_min_score = inline_website_min_score

        self.filter = AIRoboticsFilter()
        self.startup_scorer = StartupScorer()
        self.brand_scorer = BrandNameScorer()
        self.source = None
        self._website_finder = None

    def _get_date_range(self) -> tuple:
        """Get date range for announcement search."""
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_days)

        return (
            start_date.strftime("%d.%m.%Y"),
            end_date.strftime("%d.%m.%Y"),
        )

    def _is_ai_relevant(self, company_name: str, purpose: Optional[str] = None) -> tuple:
        """
        Check if company name/purpose suggests AI/robotics/tech relevance.

        Returns:
            (is_relevant: bool, filter_result: FilterResult or None)
        """
        filter_result = self.filter.filter_company(
            name=company_name,
            purpose=purpose,
            status="currently registered",
        )
        return (filter_result.passes, filter_result)

    def _find_existing_company(self, native_company_number: str) -> Optional[Dict]:
        """Find existing company by native company number."""
        if not native_company_number:
            return None

        # Try exact match first
        company = self.db.get_company_by_native_number(native_company_number)
        if company:
            return company

        # Try with registry type prefix variations
        # e.g., "HRB 12345" might be stored as "District court München HRB 12345"
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM companies
            WHERE native_company_number LIKE ?
            LIMIT 1
        """,
            (f"%{native_company_number}%",),
        )

        row = cursor.fetchone()
        return dict(row) if row else None

    def _process_new_company_announcement(
        self,
        ann: Announcement,
        stats: Dict[str, Any],
    ) -> Optional[int]:
        """
        Process an announcement that might indicate a new AI startup.

        Returns:
            company_id if new company was added, None otherwise
        """
        # Check if AI relevant — include purpose text for better classification
        is_relevant, filter_result = self._is_ai_relevant(ann.company_name, purpose=ann.purpose)

        if not is_relevant:
            return None

        # Check if already in database
        existing = self._find_existing_company(ann.native_company_number)
        if existing:
            # Backfill missing fields from announcement data
            updates = {}
            if ann.purpose and not existing.get("purpose"):
                updates["purpose"] = ann.purpose
            if ann.announcement_date and not existing.get("registration_date"):
                updates["registration_date"] = ann.announcement_date
            # Only Neueintragung announcements carry the true HR registration date.
            if (
                ann.announcement_type == "neueintragung"
                and ann.announcement_date
                and not existing.get("first_registered_date")
            ):
                updates["first_registered_date"] = ann.announcement_date
            if ann.capital_new and not existing.get("capital_amount"):
                updates["capital_amount"] = ann.capital_new
            if ann.postal_code and not existing.get("postal_code"):
                updates["postal_code"] = ann.postal_code
            if ann.street and not existing.get("street"):
                updates["street"] = ann.street
            rep_rules = getattr(ann, "representation_rules", None)
            if rep_rules and not existing.get("representation_rules"):
                updates["representation_rules"] = rep_rules
            if updates:
                self.db.update_company(existing["id"], **updates)
            stats["already_tracked"] += 1
            return existing["id"]

        # Calculate startup score
        startup_result = self.startup_scorer.score_company(
            name=ann.company_name,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            purpose=getattr(ann, "purpose", None),
            capital_amount=getattr(ann, "capital_new", None),
            tech_categories=filter_result.tech_categories,
        )
        classification = self.startup_scorer.classify(
            startup_result,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            tech_categories=filter_result.tech_categories,
        )

        # Calculate brand name score
        brand_result = self.brand_scorer.score(ann.company_name, city=ann.city)

        # Extract registry type from native number
        registry_type = ""
        if ann.native_company_number:
            for rt in ["HRB", "HRA", "GnR", "PR", "VR", "GsR"]:
                if rt in ann.native_company_number:
                    registry_type = rt
                    break

        # Insert new company with all available fields
        company_id = self.db.insert_company(
            company_number=f"announcement_{hash(ann.native_company_number) & 0xFFFFFFFF:08x}",
            name=ann.company_name,
            source="announcement",
            native_company_number=ann.native_company_number,
            current_status="currently registered",
            registry_type=registry_type,
            city=ann.city,
            state=ann.state,
            purpose=ann.purpose,
            capital_amount=ann.capital_new,
            postal_code=ann.postal_code,
            street=ann.street,
            registration_date=ann.announcement_date,
            first_registered_date=(
                ann.announcement_date if ann.announcement_type == "neueintragung" else None
            ),
            representation_rules=getattr(ann, "representation_rules", None),
            ai_robotics_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            matched_keywords=filter_result.matched_keywords,
            tech_categories=filter_result.tech_categories,
            startup_score=startup_result.total_score,
            startup_classification=classification,
            brand_name_score=brand_result.total_score,
        )

        # Add to enrichment queue for detailed lookup
        self.db.add_to_enrichment_queue(company_id, priority=1, reason="new_from_announcement")

        # Optional inline website lookup for high-signal companies. The full
        # scrape stays on WebsiteScrapeJob; we only try cheap domain guesses
        # here so the announcement run stays fast.
        if (
            self.inline_website_lookup
            and filter_result.relevance_score >= self.inline_website_min_score
        ):
            self._try_inline_website_lookup(company_id, ann)

        stats["new_companies"] += 1
        logger.info(
            "New AI company from announcement: %s (score: %d, class: %s)",
            ann.company_name,
            filter_result.relevance_score,
            classification,
        )

        return company_id

    def _process_capital_announcement(
        self,
        ann: Announcement,
        stats: Dict[str, Any],
    ) -> bool:
        """
        Process an announcement that might indicate a capital change.

        Returns:
            True if capital event was detected, False otherwise
        """
        # Find existing company
        company = self._find_existing_company(ann.native_company_number)

        if not company:
            return False

        company_id = company["id"]

        # Check if this looks like a capital change
        text_lower = ann.text.lower()
        is_capital_change = any(
            kw in text_lower
            for kw in [
                "kapitalerhöhung",
                "kapitalherabsetzung",
                "stammkapital",
                "grundkapital",
                "erhöhung",
                "herabsetzung",
                "kapital",
            ]
        )

        if not is_capital_change:
            return False

        # Try to extract capital amounts from text
        capital_old, capital_new = self.source._extract_capital_amounts(ann.text)

        # Determine event type
        if ann.announcement_type == "kapitalerhoehung" or (capital_new and capital_old and capital_new > capital_old):
            event_type = "capital_increase"
        elif ann.announcement_type == "kapitalherabsetzung" or (
            capital_new and capital_old and capital_new < capital_old
        ):
            event_type = "capital_decrease"
        else:
            event_type = "capital_change"

        # Calculate change amount
        change_amount = None
        if capital_old and capital_new:
            change_amount = capital_new - capital_old

        # Insert capital event
        self.db.insert_capital_event(
            company_id=company_id,
            event_type=event_type,
            event_date=ann.announcement_date,
            previous_amount=capital_old,
            new_amount=capital_new,
            change_amount=change_amount,
            publication_text=ann.text[:500],  # Truncate for storage
            confidence_score=0.8 if capital_new else 0.5,
        )

        # Update company's current capital if we have a new amount
        if capital_new:
            self.db.update_company(company_id, capital_amount=capital_new)

        stats["capital_events"] += 1
        logger.info(
            "Capital event detected: %s - %s (%.0f -> %.0f)",
            company["name"],
            event_type,
            capital_old or 0,
            capital_new or 0,
        )

        return True

    def _try_inline_website_lookup(self, company_id: int, ann: Announcement) -> None:
        """
        Best-effort website lookup for a freshly inserted company.

        Uses domain-guessing only (enable_search=False) to keep latency low:
        scraping and DDG fallback remain on the batch job.
        """
        try:
            if self._website_finder is None:
                from sources.website_finder import WebsiteFinder

                self._website_finder = WebsiteFinder(
                    min_confidence=0.6,
                    enable_search=False,
                )
            result = self._website_finder.find(
                ann.company_name,
                native_company_number=ann.native_company_number,
            )
            if result and result.url:
                self.db.update_company(
                    company_id,
                    website=result.url,
                    website_confidence=result.confidence,
                    website_lookup_at=datetime.now().isoformat(),
                )
                logger.info(
                    "Inline website lookup hit: %s -> %s (conf=%.2f)",
                    ann.company_name,
                    result.url,
                    result.confidence,
                )
        except Exception as e:
            logger.debug("Inline website lookup failed for %s: %s", ann.company_name, e)

    OFFICER_TYPES = {"geschaeftsfuehrer", "neueintragung", "prokura"}

    def _store_announcement(
        self,
        ann: Announcement,
        company_id: Optional[int] = None,
    ) -> int:
        """Store announcement in database and extract officers if applicable."""
        ann_id = self.db.insert_announcement(
            company_name=ann.company_name,
            native_company_number=ann.native_company_number,
            announcement_type=ann.announcement_type,
            announcement_date=ann.announcement_date,
            text=ann.text,
            capital_old=ann.capital_old,
            capital_new=ann.capital_new,
            company_id=company_id,
        )

        # Extract officers for relevant announcement types
        if company_id and ann.announcement_type in self.OFFICER_TYPES and ann.text:
            try:
                from processing.officer_extractor import extract_officers_from_text, persist_officers

                officers = extract_officers_from_text(ann.text)
                if officers:
                    persist_officers(self.db, company_id, officers, ann.announcement_date)
                self.db.mark_announcement_processed(ann_id)
            except Exception as e:
                logger.debug("Officer extraction failed for announcement %d: %s", ann_id, e)

        return ann_id

    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute the announcement monitoring job.

        Args:
            dry_run: If True, don't save to database

        Returns:
            Statistics dict
        """
        stats = {
            "announcements_fetched": 0,
            "announcements_stored": 0,
            "new_companies": 0,
            "already_tracked": 0,
            "capital_events": 0,
            "linked_to_existing": 0,
            "requests_used": 0,
            "errors": 0,
        }

        # Initialize source
        self.source = BundesAPISource()

        # Get date range
        date_from, date_to = self._get_date_range()
        logger.info("Fetching announcements from %s to %s", date_from, date_to)

        try:
            # Fetch announcements
            for ann in self.source.search_announcements(
                date_from=date_from,
                date_to=date_to,
                max_results=1000,  # Get all available
            ):
                stats["announcements_fetched"] += 1
                company_id = None

                # Process based on announcement type
                if ann.announcement_type in self.NEW_COMPANY_TYPES:
                    # Check for new AI startup
                    company_id = self._process_new_company_announcement(ann, stats)

                if ann.announcement_type in self.CAPITAL_TYPES:
                    # Check for capital changes in tracked companies
                    self._process_capital_announcement(ann, stats)

                # Try to link to existing company if not already linked
                if company_id is None:
                    existing = self._find_existing_company(ann.native_company_number)
                    if existing:
                        company_id = existing["id"]
                        stats["linked_to_existing"] += 1

                # Store announcement
                if not dry_run:
                    self._store_announcement(ann, company_id)
                    stats["announcements_stored"] += 1

            stats["requests_used"] = self.source.rate_limiter.requests_made

        except Exception as e:
            logger.exception("Error in announcement monitoring job: %s", e)
            stats["errors"] += 1

        return stats


def run_announcement_job(
    db_path: str,
    lookback_days: int = 7,
    max_requests: int = 10,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function to run announcement monitoring job.

    Args:
        db_path: Path to SQLite database
        lookback_days: Number of days to look back
        max_requests: Maximum requests per run
        dry_run: Don't save to database

    Returns:
        Statistics dict
    """
    db = Database(db_path)

    try:
        job = AnnouncementMonitoringJob(
            db=db,
            max_requests=max_requests,
            lookback_days=lookback_days,
        )
        return job.run(dry_run=dry_run)
    finally:
        db.close()
