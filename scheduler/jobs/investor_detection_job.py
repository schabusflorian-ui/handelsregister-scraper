"""
Investor Detection Job - Scan capital events and officers for VC investments.

Detects investments by matching:
1. Shareholder names in capital events against known VCs
2. Officer names against known VC partners
3. Company names in announcements

Creates investment records linking companies to investors.
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from processing.investor_matcher import InvestorMatcher, InvestorMatch

logger = logging.getLogger(__name__)


class InvestorDetectionJob:
    """
    Scan for investor/VC involvement in companies.

    Processes:
    - Capital events (publication_text may mention investors)
    - Officers (board members may be VC partners)
    - Announcements (shareholder changes)
    """

    def __init__(
        self,
        db,
        batch_size: int = 100,
        min_confidence: float = 0.8,
    ):
        """
        Initialize detection job.

        Args:
            db: Database instance
            batch_size: Number of records to process per batch
            min_confidence: Minimum confidence for matches
        """
        self.db = db
        self.batch_size = batch_size
        self.min_confidence = min_confidence

        # Initialize matcher (will load from YAML)
        self.matcher = InvestorMatcher()

        # Seed investors to database if not present
        self._ensure_investors_seeded()

    def _ensure_investors_seeded(self):
        """Ensure investor data is in the database."""
        conn = self.db.conn
        count = conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]

        if count == 0:
            logger.info("Seeding investors to database...")
            self.matcher.seed_to_database(self.db)

            # Reload matcher from database
            self.matcher = InvestorMatcher(db=self.db)

    def run(self) -> Dict[str, Any]:
        """
        Run investor detection.

        Returns:
            Statistics about the detection run
        """
        started_at = datetime.utcnow()

        stats = {
            'capital_events_scanned': 0,
            'officers_scanned': 0,
            'announcements_scanned': 0,
            'investments_found': 0,
            'investments_new': 0,
            'errors': 0,
        }

        try:
            # Scan capital events
            capital_stats = self._scan_capital_events()
            stats['capital_events_scanned'] = capital_stats['scanned']
            stats['investments_found'] += capital_stats['found']
            stats['investments_new'] += capital_stats['new']

            # Scan officers
            officer_stats = self._scan_officers()
            stats['officers_scanned'] = officer_stats['scanned']
            stats['investments_found'] += officer_stats['found']
            stats['investments_new'] += officer_stats['new']

            # Scan announcements
            announcement_stats = self._scan_announcements()
            stats['announcements_scanned'] = announcement_stats['scanned']
            stats['investments_found'] += announcement_stats['found']
            stats['investments_new'] += announcement_stats['new']

        except Exception as e:
            logger.exception("Investor detection failed: %s", e)
            stats['errors'] += 1

        stats['duration_seconds'] = (datetime.utcnow() - started_at).total_seconds()

        logger.info(
            "Investor detection complete: %d investments found, %d new",
            stats['investments_found'],
            stats['investments_new']
        )

        return stats

    def _scan_capital_events(self) -> Dict[str, int]:
        """Scan capital events for investor mentions."""
        stats = {'scanned': 0, 'found': 0, 'new': 0}

        conn = self.db.conn

        # Get capital events with publication text
        rows = conn.execute("""
            SELECT ce.id, ce.company_id, ce.publication_text, ce.event_date,
                   ce.new_amount, c.name as company_name
            FROM capital_events ce
            JOIN companies c ON ce.company_id = c.id
            WHERE ce.publication_text IS NOT NULL
              AND ce.publication_text != ''
        """).fetchall()

        for row in rows:
            stats['scanned'] += 1

            # Search for investors in publication text
            matches = self.matcher.search_in_text(
                row['publication_text'],
                min_confidence=self.min_confidence
            )

            for match in matches:
                stats['found'] += 1
                new = self._record_investment(
                    company_id=row['company_id'],
                    investor_id=match.investor_id,
                    round_type=self._infer_round_type(row['new_amount']),
                    amount=row['new_amount'],
                    investment_date=row['event_date'],
                    source='capital_event',
                    confidence=match.confidence,
                    notes=f"Matched '{match.matched_text}' in capital event"
                )
                if new:
                    stats['new'] += 1

        return stats

    def _scan_officers(self) -> Dict[str, int]:
        """Scan officers for VC partner names."""
        stats = {'scanned': 0, 'found': 0, 'new': 0}

        conn = self.db.conn

        # Get officers (board members)
        rows = conn.execute("""
            SELECT o.id, o.company_id, o.name, o.role, o.start_date,
                   c.name as company_name
            FROM officers o
            JOIN companies c ON o.company_id = c.id
            WHERE o.is_current = 1
        """).fetchall()

        for row in rows:
            stats['scanned'] += 1

            # Try to match officer name against VC partners
            matches = self.matcher.match(row['name'], min_confidence=self.min_confidence)

            # Only consider partner matches
            partner_matches = [m for m in matches if m.match_type == 'partner']

            for match in partner_matches:
                stats['found'] += 1
                new = self._record_investment(
                    company_id=row['company_id'],
                    investor_id=match.investor_id,
                    round_type=None,
                    amount=None,
                    investment_date=row['start_date'],
                    source='officer',
                    confidence=match.confidence,
                    notes=f"Officer '{row['name']}' matches VC partner"
                )
                if new:
                    stats['new'] += 1

        return stats

    def _scan_announcements(self) -> Dict[str, int]:
        """Scan announcements for investor mentions."""
        stats = {'scanned': 0, 'found': 0, 'new': 0}

        conn = self.db.conn

        # Get announcements with text
        rows = conn.execute("""
            SELECT a.id, a.company_id, a.text, a.announcement_date,
                   a.capital_new, c.name as company_name
            FROM announcements a
            JOIN companies c ON a.company_id = c.id
            WHERE a.text IS NOT NULL
              AND a.text != ''
              AND a.company_id IS NOT NULL
        """).fetchall()

        for row in rows:
            stats['scanned'] += 1

            # Search for investors in announcement text
            matches = self.matcher.search_in_text(
                row['text'],
                min_confidence=self.min_confidence
            )

            for match in matches:
                stats['found'] += 1
                new = self._record_investment(
                    company_id=row['company_id'],
                    investor_id=match.investor_id,
                    round_type=self._infer_round_type(row['capital_new']),
                    amount=row['capital_new'],
                    investment_date=row['announcement_date'],
                    source='announcement',
                    confidence=match.confidence,
                    notes=f"Matched '{match.matched_text}' in announcement"
                )
                if new:
                    stats['new'] += 1

        return stats

    def _record_investment(
        self,
        company_id: int,
        investor_id: int,
        round_type: Optional[str],
        amount: Optional[float],
        investment_date: Optional[str],
        source: str,
        confidence: float,
        notes: Optional[str] = None
    ) -> bool:
        """
        Record an investment in the database.

        Returns:
            True if new record created, False if already exists
        """
        conn = self.db.conn

        try:
            # Check if already exists
            existing = conn.execute("""
                SELECT id FROM investments
                WHERE company_id = ? AND investor_id = ?
                  AND (investment_date = ? OR (investment_date IS NULL AND ? IS NULL))
            """, (company_id, investor_id, investment_date, investment_date)).fetchone()

            if existing:
                # Update confidence if higher
                conn.execute("""
                    UPDATE investments
                    SET confidence = MAX(confidence, ?),
                        notes = COALESCE(notes, '') || '; ' || ?
                    WHERE id = ?
                """, (confidence, notes or '', existing['id']))
                conn.commit()
                return False

            # Insert new record
            conn.execute("""
                INSERT INTO investments
                (company_id, investor_id, round_type, amount, currency,
                 investment_date, detection_source, confidence, notes)
                VALUES (?, ?, ?, ?, 'EUR', ?, ?, ?, ?)
            """, (
                company_id,
                investor_id,
                round_type,
                amount,
                investment_date,
                source,
                confidence,
                notes
            ))
            conn.commit()
            return True

        except Exception as e:
            logger.error("Error recording investment: %s", e)
            return False

    def _infer_round_type(self, amount: Optional[float]) -> Optional[str]:
        """
        Infer funding round type from capital amount.

        This is a rough heuristic based on typical German funding amounts.
        """
        if amount is None:
            return 'unknown'

        if amount < 100_000:
            return 'pre_seed'
        elif amount < 500_000:
            return 'seed'
        elif amount < 2_000_000:
            return 'seed'
        elif amount < 10_000_000:
            return 'series_a'
        elif amount < 30_000_000:
            return 'series_b'
        elif amount < 100_000_000:
            return 'series_c'
        else:
            return 'growth'


def run_investor_detection(db_path: str) -> Dict[str, Any]:
    """Convenience function to run investor detection."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = InvestorDetectionJob(db=db)
        return job.run()
    finally:
        db.close()
