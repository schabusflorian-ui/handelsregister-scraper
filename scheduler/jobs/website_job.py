"""
Website Finder Job - Discover and validate company websites.

Runs periodically to find websites for companies that don't have one yet.
Prioritizes high-value companies (high startup/AI scores).
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from sources.website_finder import WebsiteFinder

logger = logging.getLogger(__name__)


class WebsiteFinderJob:
    """
    Find websites for companies missing them.

    Uses domain guessing + DuckDuckGo search, then validates
    with title matching and Impressum detection.
    """

    def __init__(
        self,
        db,
        batch_size: int = 50,
        min_confidence: float = 0.4,
        enable_search: bool = True,
        relookup_days: int = 30,
    ):
        """
        Args:
            db: Database instance
            batch_size: Max companies to process per run
            min_confidence: Minimum confidence to store a website
            enable_search: Whether to use DuckDuckGo fallback
            relookup_days: Re-check companies after this many days
        """
        self.db = db
        self.batch_size = batch_size
        self.relookup_days = relookup_days
        self.finder = WebsiteFinder(
            min_confidence=min_confidence,
            enable_search=enable_search,
        )

    def run(self) -> Dict[str, Any]:
        """
        Run website finder job.

        Returns:
            Statistics about the run
        """
        started_at = datetime.utcnow()

        stats = {
            'companies_checked': 0,
            'websites_found': 0,
            'websites_by_guess': 0,
            'websites_by_search': 0,
            'websites_impressum_verified': 0,
            'already_had_website': 0,
            'errors': 0,
        }

        try:
            companies = self._get_companies_to_check()
            stats['companies_checked'] = len(companies)

            for company in companies:
                try:
                    result = self.finder.find(
                        company['name'],
                        native_company_number=company.get('native_company_number'),
                        registry_court=company.get('registry_court'),
                    )
                    now = datetime.utcnow().isoformat()

                    if result:
                        self.db.update_company(
                            company['id'],
                            website=result.url,
                            website_confidence=result.confidence,
                            website_lookup_at=now,
                        )
                        stats['websites_found'] += 1
                        if result.source == 'domain_guess':
                            stats['websites_by_guess'] += 1
                        else:
                            stats['websites_by_search'] += 1
                        if result.impressum_verified:
                            stats['websites_impressum_verified'] += 1

                        logger.info(
                            "Website found: %s -> %s (conf=%.2f, src=%s, impressum=%s)",
                            company['name'], result.url,
                            result.confidence, result.source,
                            'verified' if result.impressum_verified else 'no',
                        )
                    else:
                        # Mark as checked so we don't retry every run
                        self.db.update_company(
                            company['id'],
                            website_lookup_at=now,
                        )

                except Exception as e:
                    logger.error("Website lookup failed for '%s': %s",
                                 company['name'], e)
                    stats['errors'] += 1

        except Exception as e:
            logger.exception("Website finder job failed: %s", e)
            stats['errors'] += 1

        stats['duration_seconds'] = (datetime.utcnow() - started_at).total_seconds()

        logger.info(
            "Website finder complete: %d checked, %d found (%d guess, %d search, %d impressum-verified)",
            stats['companies_checked'],
            stats['websites_found'],
            stats['websites_by_guess'],
            stats['websites_by_search'],
            stats['websites_impressum_verified'],
        )

        return stats

    def _get_companies_to_check(self):
        """
        Get companies that need a website lookup.

        Prioritizes:
        1. Companies that have never been checked
        2. Companies checked > relookup_days ago (without a result)
        Orders by startup_score + ai_robotics_score descending.
        """
        cutoff = (datetime.utcnow() - timedelta(days=self.relookup_days)).isoformat()

        rows = self.db.conn.execute('''
            SELECT id, name, native_company_number, registry_court,
                   website, website_lookup_at
            FROM companies
            WHERE website IS NULL
              AND (website_lookup_at IS NULL OR website_lookup_at < ?)
            ORDER BY
                startup_score DESC,
                ai_robotics_score DESC,
                id DESC
            LIMIT ?
        ''', (cutoff, self.batch_size)).fetchall()

        return [dict(row) for row in rows]


def run_website_finder(db_path: str, batch_size: int = 50) -> Dict[str, Any]:
    """Convenience function to run website finder."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = WebsiteFinderJob(db=db, batch_size=batch_size)
        return job.run()
    finally:
        db.close()
