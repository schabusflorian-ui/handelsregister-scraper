"""
Website Scrape Job - Enrich company data by scraping their websites.

Runs after website finder job. For companies that have a website URL but
are missing key data (purpose, description), fetches and parses the website
to extract structured information.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sources.website_scraper import WebsiteScraper, ScrapedWebsiteData

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

    def run(self) -> Dict[str, Any]:
        """Execute the website scraping job."""
        stats = {
            'companies_checked': 0,
            'companies_enriched': 0,
            'descriptions_added': 0,
            'tech_keywords_added': 0,
            'investors_detected': 0,
            'linkedin_found': 0,
            'errors': 0,
        }

        # Ensure we have the website_scraped_at column
        self._ensure_schema()

        # Get companies to scrape
        companies = self._get_companies_to_scrape()
        logger.info("Website scrape job: %d companies to process", len(companies))

        for company in companies:
            stats['companies_checked'] += 1

            try:
                result = self._scrape_company(company)

                if result:
                    stats['companies_enriched'] += 1
                    if result.get('description_added'):
                        stats['descriptions_added'] += 1
                    if result.get('tech_keywords_added'):
                        stats['tech_keywords_added'] += 1
                    if result.get('investors_detected'):
                        stats['investors_detected'] += result['investors_detected']
                    if result.get('linkedin_found'):
                        stats['linkedin_found'] += 1

            except Exception as e:
                logger.error("Error scraping %s: %s", company['name'], e)
                stats['errors'] += 1

        logger.info(
            "Website scrape complete: %d checked, %d enriched, %d descriptions, %d investors",
            stats['companies_checked'],
            stats['companies_enriched'],
            stats['descriptions_added'],
            stats['investors_detected'],
        )

        return stats

    def _ensure_schema(self):
        """Ensure website scraping columns exist."""
        cursor = self.db.conn.cursor()
        try:
            cursor.execute("PRAGMA table_info(companies)")
            columns = [row[1] for row in cursor.fetchall()]

            new_columns = [
                ('website_scraped_at', 'TEXT'),
                ('linkedin_url', 'TEXT'),
                ('twitter_url', 'TEXT'),
                ('website_scrape_quality', 'REAL'),
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
        rows = self.db.conn.execute('''
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
        ''', (cutoff, self.batch_size)).fetchall()

        return [dict(r) for r in rows]

    def _scrape_company(self, company: Dict) -> Optional[Dict]:
        """Scrape a single company website and update the database."""
        url = company['website']
        company_id = company['id']

        logger.debug("Scraping website for %s: %s", company['name'], url)

        # Scrape the website
        data = self.scraper.scrape(url)
        now = datetime.utcnow().isoformat()

        result = {
            'description_added': False,
            'tech_keywords_added': False,
            'investors_detected': 0,
            'linkedin_found': False,
        }

        # Build update dict
        update = {
            'website_scraped_at': now,
            'website_scrape_quality': data.scrape_quality,
        }

        # Add description/purpose if found and not already set
        if data.description and not company.get('purpose'):
            update['purpose'] = data.description[:1000]  # Limit length
            result['description_added'] = True

        # Add LinkedIn URL if found
        if data.linkedin_url:
            update['linkedin_url'] = data.linkedin_url
            result['linkedin_found'] = True

        # Add Twitter URL if found
        if data.twitter_url:
            update['twitter_url'] = data.twitter_url

        # Merge tech keywords with existing
        if data.tech_keywords:
            existing = company.get('matched_keywords')
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
                update['matched_keywords'] = json.dumps(merged[:50])  # Cap at 50
                result['tech_keywords_added'] = True

        # Store investors mentioned (as JSON in a separate field or process separately)
        if data.investors_mentioned:
            result['investors_detected'] = len(data.investors_mentioned)
            # TODO: Create investment records for detected investors
            logger.info(
                "Investors mentioned for %s: %s",
                company['name'], data.investors_mentioned
            )

        # Store funding mentions for review
        if data.funding_mentions:
            logger.info(
                "Funding mentions for %s: %s",
                company['name'], data.funding_mentions
            )

        # Update the database
        self.db.update_company(company_id, **update)

        if result['description_added'] or result['linkedin_found'] or result['investors_detected']:
            logger.info(
                "Enriched %s: desc=%s, linkedin=%s, investors=%d",
                company['name'],
                'yes' if result['description_added'] else 'no',
                'yes' if result['linkedin_found'] else 'no',
                result['investors_detected'],
            )

        return result
