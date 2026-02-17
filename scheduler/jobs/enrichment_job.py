"""
Enrichment Job - Process companies from enrichment queue.

This job processes companies from the enrichment queue, running
capital detection and other enrichment tasks on each company.

Since we don't have access to handelsregister.ai API for publications,
we focus on capital diff detection using the capital_amount field
that gets updated during discovery.

Future enhancements:
- Publication mining via handelsregister.ai API (paid)
- News monitoring via RSS feeds (gruenderszene, t3n)
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from persistence.database import Database
from processing.capital_detector import CapitalRaiseDetector, CapitalEvent
from scheduler.rate_limiter import PersistentRateLimiter

logger = logging.getLogger(__name__)


class EnrichmentJob:
    """
    Job to enrich company data from the enrichment queue.

    Currently performs:
    - Capital diff detection (compare capital_amount over time)

    Future (requires API access):
    - Publication mining for capital events
    - News monitoring for funding announcements
    """

    def __init__(
        self,
        db: Database,
        batch_size: int = 50,
    ):
        """
        Initialize enrichment job.

        Args:
            db: Database instance
            batch_size: Number of companies to process per run
        """
        self.db = db
        self.batch_size = batch_size
        self.capital_detector = CapitalRaiseDetector()

    def _store_capital_history(self, company_id: int, capital_amount: Optional[float]):
        """Store current capital amount for future comparison."""
        if capital_amount is None:
            return

        # Store in capital_history table (create if needed)
        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS capital_history (
                id INTEGER PRIMARY KEY,
                company_id INTEGER NOT NULL,
                capital_amount REAL,
                recorded_at TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            )
        """)

        self.db.conn.execute("""
            INSERT INTO capital_history (company_id, capital_amount, recorded_at)
            VALUES (?, ?, ?)
        """, (company_id, capital_amount, datetime.utcnow().isoformat()))
        self.db.conn.commit()

    def _get_previous_capital(self, company_id: int) -> Optional[float]:
        """Get the most recent capital amount for comparison."""
        try:
            row = self.db.conn.execute("""
                SELECT capital_amount FROM capital_history
                WHERE company_id = ?
                ORDER BY recorded_at DESC
                LIMIT 1 OFFSET 1
            """, (company_id,)).fetchone()

            if row:
                return row['capital_amount']
        except Exception:
            pass

        return None

    def _process_company(self, company: Dict) -> Dict[str, Any]:
        """
        Process a single company for enrichment.

        Args:
            company: Company dict from database

        Returns:
            Result dict with events_detected count
        """
        company_id = company['id']
        company_name = company['name']
        current_capital = company.get('capital_amount')

        result = {
            'company_id': company_id,
            'company_name': company_name,
            'events_detected': 0,
            'success': True,
        }

        try:
            # Store current capital for future comparison
            self._store_capital_history(company_id, current_capital)

            # Check for capital changes
            previous_capital = self._get_previous_capital(company_id)

            if previous_capital is not None and current_capital is not None:
                event = self.capital_detector.detect_from_capital_diff(
                    previous_capital=previous_capital,
                    current_capital=current_capital,
                    detection_date=datetime.utcnow().isoformat(),
                )

                if event:
                    # Store the capital event
                    self.db.insert_capital_event(
                        company_id=company_id,
                        event_type=event.event_type,
                        event_date=event.event_date,
                        previous_amount=event.previous_amount,
                        new_amount=event.new_amount,
                        change_amount=event.change_amount,
                        currency=event.currency,
                        publication_text=event.publication_text,
                        confidence_score=event.confidence_score,
                    )

                    result['events_detected'] += 1
                    logger.info(
                        "Capital %s detected for %s: %.0f -> %.0f EUR",
                        event.event_type, company_name,
                        event.previous_amount or 0, event.new_amount or 0
                    )

                    # Log the change
                    self.db.log_change(
                        company_id=company_id,
                        change_type='capital_change',
                        field_name='capital_amount',
                        old_value=str(previous_capital) if previous_capital else None,
                        new_value=str(current_capital) if current_capital else None,
                    )

            # Mark as enriched
            self.db.mark_enriched(company_id, success=True)

        except Exception as e:
            logger.error("Error enriching %s: %s", company_name, e)
            result['success'] = False
            # Don't mark as failed - allow retry
            self.db.conn.execute("""
                UPDATE enrichment_queue
                SET attempts = attempts + 1, last_attempt = ?
                WHERE company_id = ?
            """, (datetime.utcnow().isoformat(), company_id))
            self.db.conn.commit()

        return result

    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute the enrichment job.

        Args:
            dry_run: If True, don't persist changes

        Returns:
            Statistics dict
        """
        stats = {
            'companies_processed': 0,
            'events_detected': 0,
            'errors': 0,
            'queue_remaining': 0,
        }

        # Get companies from enrichment queue
        companies = self.db.get_companies_for_enrichment(limit=self.batch_size)

        if not companies:
            logger.info("No companies in enrichment queue")
            return stats

        logger.info("Processing %d companies from enrichment queue", len(companies))

        for company in companies:
            if dry_run:
                stats['companies_processed'] += 1
                continue

            result = self._process_company(company)

            stats['companies_processed'] += 1
            stats['events_detected'] += result.get('events_detected', 0)

            if not result['success']:
                stats['errors'] += 1

        stats['queue_remaining'] = self.db.get_enrichment_queue_size()

        # Backfill officers from stored announcements
        if not dry_run:
            try:
                from processing.officer_extractor import backfill_officers_from_announcements
                officer_stats = backfill_officers_from_announcements(self.db)
                stats['officers_added'] = officer_stats.get('officers_added', 0)
            except Exception as e:
                logger.debug("Officer backfill error: %s", e)
                stats['officers_added'] = 0

        logger.info(
            "Enrichment complete: %d processed, %d events detected, %d officers added, %d remaining",
            stats['companies_processed'], stats['events_detected'],
            stats.get('officers_added', 0), stats['queue_remaining'],
        )

        return stats


def run_enrichment_job(
    db_path: str,
    batch_size: int = 50,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function to run enrichment job.

    Args:
        db_path: Path to SQLite database
        batch_size: Number of companies per batch
        dry_run: Don't save to database

    Returns:
        Statistics dict
    """
    db = Database(db_path)

    try:
        job = EnrichmentJob(
            db=db,
            batch_size=batch_size,
        )
        return job.run(dry_run=dry_run)
    finally:
        db.close()
