"""
Backfill Job - Historical data collection from Handelsregister.

This job systematically searches through keyword × state combinations
to discover companies registered in the past ~2 years that weren't
captured in the OffeneRegister bulk data (which is from 2018-2019).

Strategy:
1. High-signal keywords × all states
2. Medium-signal keywords × major startup hubs
3. Climate tech keywords × all states

The job maintains persistent state to:
- Track which combinations have been searched
- Resume after interruptions
- Avoid re-searching completed combinations
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from persistence.database import Database
from processing.filters import AIRoboticsFilter
from processing.startup_scorer import StartupScorer
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import BundesAPISource, SearchResult

logger = logging.getLogger(__name__)


# German state codes
GERMAN_STATES = ["bw", "by", "be", "bb", "hb", "hh", "he", "mv", "ni", "nw", "rp", "sl", "sn", "st", "sh", "th"]

# Major startup hub states (for medium-priority searches)
STARTUP_HUB_STATES = ["be", "by", "hh", "nw", "he", "bw"]


@dataclass
class BackfillCombination:
    """A single keyword × state combination to search."""

    keyword: str
    state_code: Optional[str]  # None = nationwide search
    registry_type: str = "HRB"  # Focus on GmbH/UG (commercial register)
    status: str = "pending"  # pending, completed, failed
    results_count: int = 0
    last_attempted_at: Optional[str] = None


class BackfillJob:
    """
    Job to backfill historical company data.

    Systematically searches keyword × state combinations to discover
    companies from the past ~2 years.
    """

    # High-signal AI/robotics keywords (search nationwide + all states)
    HIGH_SIGNAL_KEYWORDS = [
        "künstliche intelligenz",
        "artificial intelligence",
        "machine learning",
        "deep learning",
        "robotik",
        "robotics",
        "neural network",
        "computer vision",
        "NLP",
        "autonomous",
        "AI platform",
        "KI plattform",
        "chatbot",
        "generative",
    ]

    # Medium-signal keywords (search nationwide only)
    MEDIUM_SIGNAL_KEYWORDS = [
        "data science",
        "big data",
        "analytics",
        "predictive",
        "automation",
        "IoT",
        "industrie 4.0",
        "smart factory",
        "sensor tech",
        "embedded systems",
    ]

    # Climate tech keywords
    CLIMATE_TECH_KEYWORDS = [
        "erneuerbare energie",
        "renewable energy",
        "solarenergie",
        "solar energy",
        "windenergie",
        "wind energy",
        "photovoltaik",
        "energiespeicher",
        "battery technology",
        "smart grid",
        "wasserstoff",
        "hydrogen",
        "klimaneutral",
        "carbon neutral",
        "dekarbonisierung",
        "cleantech",
        "gruene technologie",
        "green technology",
        "umwelttechnologie",
        "elektromobilitaet",
        "e-mobility",
        "ladeinfrastruktur",
        "brennstoffzelle",
        "kreislaufwirtschaft",
        "circular economy",
        "recycling technologie",
    ]

    def __init__(
        self,
        db: Database,
        rate_limiter: PersistentRateLimiter,
        max_requests: int = 30,
    ):
        """
        Initialize backfill job.

        Args:
            db: Database instance
            rate_limiter: Persistent rate limiter
            max_requests: Maximum requests per job run
        """
        self.db = db
        self.rate_limiter = rate_limiter
        self.max_requests = max_requests

        self.filter = AIRoboticsFilter()
        self.startup_scorer = StartupScorer()
        self.source = None

    def _ensure_tables(self):
        """Create backfill state table if needed."""
        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS backfill_state (
                id INTEGER PRIMARY KEY,
                keyword TEXT NOT NULL,
                state_code TEXT,
                registry_type TEXT DEFAULT 'HRB',
                status TEXT DEFAULT 'pending',
                results_count INTEGER DEFAULT 0,
                last_attempted_at TEXT,
                UNIQUE(keyword, state_code, registry_type)
            )
        """)

        # Index for finding pending combinations
        self.db.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_backfill_status
            ON backfill_state(status)
        """)

        self.db.conn.commit()

    def _initialize_combinations(self):
        """Populate backfill_state table with all combinations to search."""
        # Check if already initialized
        count = self.db.conn.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0]

        if count > 0:
            logger.info("Backfill state already initialized with %d combinations", count)
            return

        combinations = []

        # Phase 1: High-signal keywords nationwide
        for keyword in self.HIGH_SIGNAL_KEYWORDS:
            combinations.append((keyword, None, "HRB"))

        # Phase 2: High-signal keywords × all states
        for keyword in self.HIGH_SIGNAL_KEYWORDS:
            for state in GERMAN_STATES:
                combinations.append((keyword, state, "HRB"))

        # Phase 3: Medium-signal keywords nationwide
        for keyword in self.MEDIUM_SIGNAL_KEYWORDS:
            combinations.append((keyword, None, "HRB"))

        # Phase 4: Climate tech keywords nationwide
        for keyword in self.CLIMATE_TECH_KEYWORDS:
            combinations.append((keyword, None, "HRB"))

        # Phase 5: Climate tech × startup hub states
        for keyword in self.CLIMATE_TECH_KEYWORDS:
            for state in STARTUP_HUB_STATES:
                combinations.append((keyword, state, "HRB"))

        # Insert all combinations
        self.db.conn.executemany(
            """
            INSERT OR IGNORE INTO backfill_state (keyword, state_code, registry_type)
            VALUES (?, ?, ?)
        """,
            combinations,
        )
        self.db.conn.commit()

        logger.info("Initialized %d backfill combinations", len(combinations))

    def _get_next_pending(self, limit: int = 1) -> List[BackfillCombination]:
        """Get next pending combinations to process."""
        rows = self.db.conn.execute(
            """
            SELECT keyword, state_code, registry_type, status, results_count
            FROM backfill_state
            WHERE status = 'pending'
            ORDER BY id
            LIMIT ?
        """,
            (limit,),
        ).fetchall()

        return [
            BackfillCombination(
                keyword=row["keyword"],
                state_code=row["state_code"],
                registry_type=row["registry_type"],
                status=row["status"],
                results_count=row["results_count"],
            )
            for row in rows
        ]

    def _update_combination_status(
        self,
        keyword: str,
        state_code: Optional[str],
        registry_type: str,
        status: str,
        results_count: int = 0,
    ):
        """Update status of a combination."""
        self.db.conn.execute(
            """
            UPDATE backfill_state
            SET status = ?,
                results_count = ?,
                last_attempted_at = ?
            WHERE keyword = ?
              AND (state_code = ? OR (state_code IS NULL AND ? IS NULL))
              AND registry_type = ?
        """,
            (status, results_count, datetime.utcnow().isoformat(), keyword, state_code, state_code, registry_type),
        )
        self.db.conn.commit()

    def _process_result(self, result: SearchResult) -> bool:
        """Process a single search result. Returns True if newly inserted."""
        # Check if already in database
        existing = self.db.get_company_by_native_number(result.native_company_number)
        if existing:
            return False

        # Apply AI/robotics filter
        filter_result = self.filter.filter_company(
            name=result.name,
            status=result.status,
        )

        if not filter_result.passes:
            return False

        # Calculate startup score
        startup_result = self.startup_scorer.score_company(
            name=result.name,
            city=result.state,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            purpose=getattr(result, "purpose", None),
            capital_amount=getattr(result, "capital_amount", None),
            tech_categories=filter_result.tech_categories,
        )
        classification = self.startup_scorer.classify(
            startup_result,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            tech_categories=filter_result.tech_categories,
        )

        # Extract legal form from company name
        from processing.filters import extract_legal_form

        legal_form = extract_legal_form(result.name)

        # Insert new company
        company_id = self.db.insert_company(
            company_number=f"bundesapi_{hash(result.native_company_number) & 0xFFFFFFFF:08x}",
            name=result.name,
            source="bundesapi",
            native_company_number=result.native_company_number,
            current_status=result.status,
            registry_court=result.registry_court,
            registry_type=result.registry_type,
            state=result.state,
            city=result.city,
            legal_form=legal_form,
            ai_robotics_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            matched_keywords=filter_result.matched_keywords,
            tech_categories=filter_result.tech_categories,
            startup_score=startup_result.total_score,
            startup_classification=classification,
        )

        # Add to enrichment queue
        self.db.add_to_enrichment_queue(company_id, priority=2, reason="backfill")

        # Capture Neueintragung VÖ (1 extra request) to store
        # first_registered_date + officers + purpose at discovery time.
        try:
            from processing.vo_capture import capture_neueintragung

            capture_neueintragung(
                self.db, self.source, company_id, result, self.rate_limiter
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("VÖ capture error for %s: %s", result.name, e)

        logger.info(
            "New company (backfill): %s (AI: %d, startup: %s)",
            result.name,
            filter_result.relevance_score,
            classification,
        )
        return True

    def get_progress(self) -> Dict[str, Any]:
        """Get current backfill progress."""
        total = self.db.conn.execute("SELECT COUNT(*) FROM backfill_state").fetchone()[0]

        completed = self.db.conn.execute("SELECT COUNT(*) FROM backfill_state WHERE status = 'completed'").fetchone()[0]

        failed = self.db.conn.execute("SELECT COUNT(*) FROM backfill_state WHERE status = 'failed'").fetchone()[0]

        pending = self.db.conn.execute("SELECT COUNT(*) FROM backfill_state WHERE status = 'pending'").fetchone()[0]

        return {
            "total_combinations": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "progress_percent": (completed / total * 100) if total > 0 else 0,
        }

    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute the backfill job.

        Args:
            dry_run: If True, don't save to database

        Returns:
            Statistics dict
        """
        self._ensure_tables()
        self._initialize_combinations()

        stats = {
            "combinations_processed": 0,
            "companies_found": 0,
            "companies_new": 0,
            "requests_used": 0,
            "errors": 0,
        }

        # Initialize source
        self.source = BundesAPISource()

        while stats["requests_used"] < self.max_requests:
            # Check rate limit
            rate_state = self.rate_limiter.get_state()
            if rate_state.tokens_available < 2:
                logger.info("Insufficient rate limit tokens, pausing")
                break

            # Get next pending combination
            pending = self._get_next_pending(limit=1)
            if not pending:
                logger.info("No more pending combinations - backfill complete!")
                break

            combo = pending[0]
            state_label = combo.state_code or "nationwide"
            logger.info("Backfill: '%s' in %s", combo.keyword, state_label)

            try:
                # Acquire rate limit tokens
                if not self.rate_limiter.acquire(count=2, block=False):
                    logger.warning("Could not acquire rate limit tokens")
                    break

                stats["requests_used"] += 2

                # Perform search
                states = [combo.state_code] if combo.state_code else None
                results_count = 0
                new_count = 0

                for result in self.source.search(
                    keywords=[combo.keyword],
                    keyword_mode="all",
                    states=states,
                    registry_types=[combo.registry_type] if combo.registry_type else None,
                    max_results=100,
                ):
                    results_count += 1
                    stats["companies_found"] += 1

                    if not dry_run:
                        if self._process_result(result):
                            new_count += 1
                            stats["companies_new"] += 1

                # Mark combination as completed
                self._update_combination_status(
                    combo.keyword,
                    combo.state_code,
                    combo.registry_type,
                    status="completed",
                    results_count=results_count,
                )

                stats["combinations_processed"] += 1
                logger.info("Completed: %d results, %d new", results_count, new_count)

            except Exception as e:
                logger.error("Error processing '%s' in %s: %s", combo.keyword, state_label, e)
                stats["errors"] += 1

                # Mark as failed after error
                self._update_combination_status(combo.keyword, combo.state_code, combo.registry_type, status="failed")

        # Add progress to stats
        stats.update(self.get_progress())

        return stats


def run_backfill_job(
    db_path: str,
    max_requests: int = 30,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function to run backfill job.

    Args:
        db_path: Path to SQLite database
        max_requests: Maximum requests per run
        dry_run: Don't save to database

    Returns:
        Statistics dict
    """
    db = Database(db_path)
    rate_limiter = PersistentRateLimiter(db_path)

    try:
        job = BackfillJob(
            db=db,
            rate_limiter=rate_limiter,
            max_requests=max_requests,
        )
        return job.run(dry_run=dry_run)
    finally:
        db.close()
