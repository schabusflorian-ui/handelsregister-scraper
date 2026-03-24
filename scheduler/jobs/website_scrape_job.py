"""
Website Scrape Job - Enrich company data by scraping their websites.

Runs after website finder job. For companies that have a website URL but
are missing key data (purpose, description), fetches and parses the website
to extract structured information.

Investor mentions found on websites are matched against known VCs/investors
and persisted to the investments table.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from processing.investor_matcher import InvestorMatcher
from sources.website_scraper import WebsiteScraper

logger = logging.getLogger(__name__)


class WebsiteScrapeJob:
    """
    Batch job to scrape company websites and enrich database records.

    Prioritizes:
    1. High-value companies (startups, high AI score) without purpose
    2. Companies with websites but no description
    3. Companies not scraped recently
    """

    def __init__(
        self,
        db,
        batch_size: int = 30,
        min_scrape_interval_days: int = 30,
        fetch_subpages: bool = True,
    ):
        """
        Args:
            db: Database instance
            batch_size: Number of companies to scrape per run
            min_scrape_interval_days: Don't re-scrape within this many days
            fetch_subpages: Whether to fetch about/team/careers pages
        """
        self.db = db
        self.batch_size = batch_size
        self.min_scrape_interval_days = min_scrape_interval_days
        self.scraper = WebsiteScraper(fetch_subpages=fetch_subpages)

        # Initialize investor matcher for resolving investor mentions
        self.investor_matcher = InvestorMatcher(db=db)
        self._ensure_investors_seeded()

    def run(self) -> Dict[str, Any]:
        """Execute the website scraping job."""
        stats = {
            "companies_checked": 0,
            "companies_enriched": 0,
            "descriptions_added": 0,
            "tech_keywords_added": 0,
            "investors_detected": 0,
            "investments_created": 0,
            "linkedin_found": 0,
            "errors": 0,
        }

        # Ensure we have the website_scraped_at column
        self._ensure_schema()

        # Get companies to scrape
        companies = self._get_companies_to_scrape()
        logger.info("Website scrape job: %d companies to process", len(companies))

        for company in companies:
            stats["companies_checked"] += 1

            try:
                result = self._scrape_company(company)

                if result:
                    stats["companies_enriched"] += 1
                    if result.get("description_added"):
                        stats["descriptions_added"] += 1
                    if result.get("tech_keywords_added"):
                        stats["tech_keywords_added"] += 1
                    if result.get("investors_detected"):
                        stats["investors_detected"] += result["investors_detected"]
                    stats["investments_created"] += result.get("investments_created", 0)
                    if result.get("linkedin_found"):
                        stats["linkedin_found"] += 1

            except Exception as e:
                logger.error("Error scraping %s: %s", company["name"], e)
                stats["errors"] += 1

        logger.info(
            "Website scrape complete: %d checked, %d enriched, %d descriptions, "
            "%d investors detected, %d investment records created",
            stats["companies_checked"],
            stats["companies_enriched"],
            stats["descriptions_added"],
            stats["investors_detected"],
            stats["investments_created"],
        )

        return stats

    def _ensure_schema(self):
        """Ensure website scraping columns exist."""
        cursor = self.db.conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(companies)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = [
                ("website_scraped_at", "TEXT"),
                ("linkedin_url", "TEXT"),
                ("twitter_url", "TEXT"),
                ("website_scrape_quality", "REAL"),
                ("funding_mentions", "TEXT"),
            ]

            for col, col_type in new_columns:
                if col not in columns:
                    cursor.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
                    logger.info("Added column: %s", col)

            self.db.conn.commit()
        except Exception as e:
            logger.error("Schema migration failed: %s", e)

    def _get_companies_to_scrape(self) -> List[Dict]:
        """Get companies that need website scraping."""
        cutoff = (datetime.utcnow() - timedelta(days=self.min_scrape_interval_days)).isoformat()

        # Companies with websites but missing purpose, not recently scraped
        rows = self.db.conn.execute(
            """
            SELECT id, name, website, purpose, matched_keywords,
                   startup_classification, startup_score, ai_robotics_score
            FROM companies
            WHERE website IS NOT NULL
              AND website != ''
              AND (purpose IS NULL OR purpose = '')
              AND (website_scraped_at IS NULL OR website_scraped_at < ?)
            ORDER BY
                startup_score DESC,
                ai_robotics_score DESC,
                id DESC
            LIMIT ?
        """,
            (cutoff, self.batch_size),
        ).fetchall()

        return [dict(r) for r in rows]

    def _scrape_company(self, company: Dict) -> Optional[Dict]:
        """Scrape a single company website and update the database."""
        url = company["website"]
        company_id = company["id"]

        logger.debug("Scraping website for %s: %s", company["name"], url)

        # Scrape the website
        data = self.scraper.scrape(url)
        now = datetime.utcnow().isoformat()

        result = {
            "description_added": False,
            "tech_keywords_added": False,
            "investors_detected": 0,
            "linkedin_found": False,
        }

        # Build update dict
        update = {
            "website_scraped_at": now,
            "website_scrape_quality": data.scrape_quality,
        }

        # Add description/purpose if found and not already set
        if data.description and not company.get("purpose"):
            update["purpose"] = data.description[:1000]  # Limit length
            result["description_added"] = True

        # Add LinkedIn URL if found
        if data.linkedin_url:
            update["linkedin_url"] = data.linkedin_url
            result["linkedin_found"] = True

        # Add Twitter URL if found
        if data.twitter_url:
            update["twitter_url"] = data.twitter_url

        # Merge tech keywords with existing
        if data.tech_keywords:
            existing = company.get("matched_keywords")
            if existing:
                try:
                    existing_list = json.loads(existing)
                except (json.JSONDecodeError, TypeError):
                    existing_list = []
            else:
                existing_list = []

            # Add new keywords
            new_keywords = set(data.tech_keywords) - set(existing_list)
            if new_keywords:
                merged = existing_list + list(new_keywords)
                update["matched_keywords"] = json.dumps(merged[:50])  # Cap at 50
                result["tech_keywords_added"] = True

        # Match investor mentions against known VCs and persist investment records
        if data.investors_mentioned:
            result["investors_detected"] = len(data.investors_mentioned)
            investments_created = 0
            for investor_name in data.investors_mentioned:
                matches = self.investor_matcher.match(investor_name, min_confidence=0.8)
                if matches:
                    best = matches[0]  # Highest confidence first
                    created = self._record_investment(
                        company_id=company_id,
                        investor_id=best.investor_id,
                        source="website",
                        # Slightly discount website mentions vs direct capital events
                        confidence=best.confidence * 0.9,
                        notes=f"Mentioned on website: '{best.matched_text}'",
                    )
                    if created:
                        investments_created += 1
                        logger.info(
                            "Investment recorded: %s → %s (confidence=%.2f)",
                            company["name"],
                            best.investor_name,
                            best.confidence,
                        )
                else:
                    logger.debug("No investor match for '%s' mentioned on %s", investor_name, company["name"])
            result["investments_created"] = investments_created

        # Store funding mentions as JSON for later analysis
        if data.funding_mentions:
            update["funding_mentions"] = json.dumps(data.funding_mentions[:5])
            logger.info("Funding mentions for %s: %s", company["name"], data.funding_mentions)

        # Store team size (from team members or indicator)
        if data.team_members:
            update["team_size"] = len(data.team_members)
        elif data.team_size_indicator:
            # Parse indicator like "10-50" → take midpoint
            try:
                parts = data.team_size_indicator.split("-")
                if len(parts) == 2:
                    update["team_size"] = (int(parts[0]) + int(parts[1])) // 2
            except (ValueError, IndexError):
                pass

        # Store tech stack as JSON
        if data.tech_stack:
            update["tech_stack"] = json.dumps(data.tech_stack[:20])

        # Store GitHub URL
        if data.github_url:
            update["github_url"] = data.github_url

        # Store job count (strong hiring signal)
        if data.job_count > 0:
            update["job_count"] = data.job_count

        # Update the database
        self.db.update_company(company_id, **update)

        if result["description_added"] or result["linkedin_found"] or result["investors_detected"]:
            logger.info(
                "Enriched %s: desc=%s, linkedin=%s, investors=%d, investments=%d",
                company["name"],
                "yes" if result["description_added"] else "no",
                "yes" if result["linkedin_found"] else "no",
                result["investors_detected"],
                result.get("investments_created", 0),
            )

        # Re-score with purpose text if we just added a description
        # This can significantly improve classification for companies with generic names
        if result["description_added"]:
            self._rescore_with_purpose(company, update.get("purpose", ""))

        return result

    def _rescore_with_purpose(self, company: Dict, purpose: str):
        """Re-run scoring pipeline now that we have purpose text."""
        from processing.filters import AIRoboticsFilter
        from processing.startup_scorer import StartupScorer

        filt = AIRoboticsFilter()
        scorer = StartupScorer()

        filter_result = filt.filter_company(
            name=company["name"],
            purpose=purpose,
        )

        # Only update if scores improved (don't downgrade)
        new_ai = max(filter_result.relevance_score, company.get("ai_robotics_score", 0))
        new_climate = max(filter_result.climate_score, company.get("climate_score", 0))

        rescore_update = {}
        if new_ai > company.get("ai_robotics_score", 0):
            rescore_update["ai_robotics_score"] = new_ai
        if new_climate > company.get("climate_score", 0):
            rescore_update["climate_score"] = new_climate
        if filter_result.tech_categories:
            # Merge with existing categories
            existing = company.get("tech_categories")
            if existing:
                try:
                    existing_cats = json.loads(existing)
                except (json.JSONDecodeError, TypeError):
                    existing_cats = []
            else:
                existing_cats = []
            merged = list(set(existing_cats + filter_result.tech_categories))
            if len(merged) > len(existing_cats):
                rescore_update["tech_categories"] = json.dumps(merged)

        if rescore_update:
            # Re-classify with updated scores
            startup_result = scorer.score_company(
                name=company["name"],
                city=company.get("city"),
                purpose=purpose,
                capital_amount=company.get("capital_amount"),
                ai_relevance_score=new_ai,
                climate_score=new_climate,
                tech_categories=filter_result.tech_categories,
            )
            new_classification = scorer.classify(
                startup_result,
                ai_relevance_score=new_ai,
                climate_score=new_climate,
                tech_categories=filter_result.tech_categories,
            )
            rescore_update["startup_score"] = startup_result.total_score
            rescore_update["startup_classification"] = new_classification
            rescore_update["matched_keywords"] = json.dumps(filter_result.matched_keywords)

            self.db.update_company(company["id"], **rescore_update)
            logger.info(
                "Re-scored %s with purpose: AI=%d→%d, climate=%d→%d, class=%s",
                company["name"],
                company.get("ai_robotics_score", 0), new_ai,
                company.get("climate_score", 0), new_climate,
                new_classification,
            )

    def _ensure_investors_seeded(self):
        """Ensure investor data is in the database (needed for matching)."""
        try:
            count = self.db.conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
            if count == 0:
                logger.info("Seeding investor data from YAML...")
                self.investor_matcher.seed_to_database(self.db)
        except Exception as e:
            logger.warning("Could not check/seed investors: %s", e)

    def _record_investment(
        self,
        company_id: int,
        investor_id: int,
        source: str,
        confidence: float,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Record an investment in the database, skipping if duplicate exists.

        Returns:
            True if a new record was created, False if already exists.
        """
        conn = self.db.conn
        try:
            # Check if already exists (same company, investor, source)
            existing = conn.execute(
                """
                SELECT id FROM investments
                WHERE company_id = ? AND investor_id = ?
                  AND detection_source = ?
            """,
                (company_id, investor_id, source),
            ).fetchone()

            if existing:
                # Update confidence if this match is better
                conn.execute(
                    """
                    UPDATE investments
                    SET confidence = MAX(confidence, ?)
                    WHERE id = ?
                """,
                    (confidence, existing["id"]),
                )
                conn.commit()
                return False

            # Insert new investment record
            # round_type, amount, investment_date are NULL for website mentions
            # — they can be enriched later from capital events or announcements
            conn.execute(
                """
                INSERT INTO investments
                (company_id, investor_id, round_type, amount, currency,
                 investment_date, detection_source, confidence, notes)
                VALUES (?, ?, NULL, NULL, 'EUR', NULL, ?, ?, ?)
            """,
                (company_id, investor_id, source, confidence, notes),
            )
            conn.commit()
            return True

        except Exception as e:
            logger.error("Error recording investment for company %d, investor %d: %s", company_id, investor_id, e)
            return False
