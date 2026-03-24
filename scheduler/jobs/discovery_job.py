"""
Discovery Job - Find new AI/robotics companies in Handelsregister.

This job runs periodically to discover newly registered companies
matching our AI/robotics/climate-tech keywords.

Features:
- Keyword rotation to spread searches across the full keyword set
- Persistent state for resume after restarts
- Integration with rate limiter
- Progress tracking and reporting
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from persistence.database import Database
from processing.brand_name_scorer import BrandNameScorer
from processing.filters import AIRoboticsFilter
from processing.startup_scorer import StartupScorer
from scheduler.rate_limiter import PersistentRateLimiter
from sources.bundesapi import BundesAPISource, SearchResult

logger = logging.getLogger(__name__)


@dataclass
class DiscoveryJobState:
    """Persistent state for discovery job."""

    job_id: int
    job_type: str = "discovery"
    started_at: str = ""
    completed_at: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed

    # Progress tracking
    keywords_total: int = 0
    keywords_completed: int = 0
    current_keyword_index: int = 0

    # Results
    companies_found: int = 0
    companies_new: int = 0
    requests_used: int = 0

    # Checkpoint data for resume
    checkpoint_data: Dict = field(default_factory=dict)

    # Error tracking
    last_error: Optional[str] = None
    error_count: int = 0


class DiscoveryJob:
    """
    Job to discover new companies from Handelsregister.

    Searches through configured keywords and saves any new
    AI/robotics companies to the database.
    """

    # Priority keywords to search first (high signal)
    PRIORITY_KEYWORDS = [
        "künstliche intelligenz",
        "artificial intelligence",
        "machine learning",
        "deep learning",
        "robotik",
        "robotics",
        "KI GmbH",
        "AI GmbH",
        ".ai GmbH",
        "neural",
        "autonomous",
        "automation",
        "computer vision",
        "natural language",
    ]

    # Secondary keywords (medium signal)
    SECONDARY_KEYWORDS = [
        "data science",
        "big data",
        "predictive",
        "analytics platform",
        "cloud platform",
        "IoT",
        "internet of things",
        "sensor",
        "smart factory",
        "industrie 4.0",
        "digitalisierung",
        "software platform",
        "SaaS",
    ]

    def __init__(
        self,
        db: Database,
        rate_limiter: PersistentRateLimiter,
        keywords: Optional[List[str]] = None,
        max_requests: int = 20,
    ):
        """
        Initialize discovery job.

        Args:
            db: Database instance
            rate_limiter: Persistent rate limiter
            keywords: Optional custom keyword list (uses defaults if not provided)
            max_requests: Maximum requests to use per job run
        """
        self.db = db
        self.rate_limiter = rate_limiter
        self.keywords = keywords or (self.PRIORITY_KEYWORDS + self.SECONDARY_KEYWORDS)
        self.max_requests = max_requests

        self.filter = AIRoboticsFilter()
        self.startup_scorer = StartupScorer()
        self.brand_scorer = BrandNameScorer()
        self.source = None  # Lazily initialized

        self._state: Optional[DiscoveryJobState] = None

    def _ensure_tables(self):
        """Ensure job tracking tables exist."""
        self.db.conn.execute("""
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY,
                job_type TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'pending',
                keywords_total INTEGER DEFAULT 0,
                keywords_completed INTEGER DEFAULT 0,
                current_keyword_index INTEGER DEFAULT 0,
                companies_found INTEGER DEFAULT 0,
                companies_new INTEGER DEFAULT 0,
                requests_used INTEGER DEFAULT 0,
                checkpoint_data TEXT,
                last_error TEXT,
                error_count INTEGER DEFAULT 0
            )
        """)
        self.db.conn.commit()

    def _create_job_run(self) -> int:
        """Create a new job run record."""
        cursor = self.db.conn.execute(
            """
            INSERT INTO job_runs (job_type, started_at, status, keywords_total)
            VALUES (?, ?, 'running', ?)
        """,
            ("discovery", datetime.utcnow().isoformat(), len(self.keywords)),
        )
        self.db.conn.commit()
        return cursor.lastrowid

    def _update_job_state(self):
        """Persist current job state to database."""
        if not self._state:
            return

        self.db.conn.execute(
            """
            UPDATE job_runs SET
                status = ?,
                completed_at = ?,
                keywords_completed = ?,
                current_keyword_index = ?,
                companies_found = ?,
                companies_new = ?,
                requests_used = ?,
                checkpoint_data = ?,
                last_error = ?,
                error_count = ?
            WHERE id = ?
        """,
            (
                self._state.status,
                self._state.completed_at,
                self._state.keywords_completed,
                self._state.current_keyword_index,
                self._state.companies_found,
                self._state.companies_new,
                self._state.requests_used,
                json.dumps(self._state.checkpoint_data),
                self._state.last_error,
                self._state.error_count,
                self._state.job_id,
            ),
        )
        self.db.conn.commit()

    def _get_last_incomplete_job(self) -> Optional[DiscoveryJobState]:
        """Get the last incomplete job for resume."""
        row = self.db.conn.execute("""
            SELECT * FROM job_runs
            WHERE job_type = 'discovery' AND status = 'running'
            ORDER BY id DESC LIMIT 1
        """).fetchone()

        if row:
            return DiscoveryJobState(
                job_id=row["id"],
                job_type=row["job_type"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                status=row["status"],
                keywords_total=row["keywords_total"],
                keywords_completed=row["keywords_completed"],
                current_keyword_index=row["current_keyword_index"],
                companies_found=row["companies_found"],
                companies_new=row["companies_new"],
                requests_used=row["requests_used"],
                checkpoint_data=json.loads(row["checkpoint_data"] or "{}"),
                last_error=row["last_error"],
                error_count=row["error_count"],
            )
        return None

    def _process_result(self, result: SearchResult) -> bool:
        """
        Process a single search result.

        Returns True if company was newly inserted.
        """
        self._state.companies_found += 1

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
            city=result.state,  # Use state as city approximation
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
        )
        classification = self.startup_scorer.classify(
            startup_result,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
        )

        # Extract legal form from company name
        from processing.filters import extract_legal_form

        legal_form = extract_legal_form(result.name)

        # Calculate brand name score
        brand_result = self.brand_scorer.score(result.name, city=result.city)

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
            brand_name_score=brand_result.total_score,
        )

        # Add to enrichment queue — boost priority for brand-name startups
        priority = 0 if brand_result.is_likely_tech_startup else 1
        self.db.add_to_enrichment_queue(company_id, priority=priority, reason="new_from_bundesapi")

        self._state.companies_new += 1
        logger.info(
            "New company: %s (AI score: %d, startup: %s)", result.name, filter_result.relevance_score, classification
        )

        return True

    def run(self, resume: bool = True, dry_run: bool = False) -> Dict[str, Any]:
        """
        Execute the discovery job.

        Args:
            resume: If True, try to resume from last incomplete job
            dry_run: If True, don't save to database (for testing)

        Returns:
            Statistics dict
        """
        self._ensure_tables()

        # Try to resume if requested
        if resume:
            existing_job = self._get_last_incomplete_job()
            if existing_job:
                logger.info(
                    "Resuming job %d from keyword index %d", existing_job.job_id, existing_job.current_keyword_index
                )
                self._state = existing_job

        # Create new job if not resuming
        if not self._state:
            job_id = self._create_job_run()
            self._state = DiscoveryJobState(
                job_id=job_id,
                started_at=datetime.utcnow().isoformat(),
                status="running",
                keywords_total=len(self.keywords),
            )

        # Initialize source
        self.source = BundesAPISource()

        # Process keywords starting from checkpoint
        start_index = self._state.current_keyword_index

        try:
            for i, keyword in enumerate(self.keywords[start_index:], start=start_index):
                # Check rate limit budget
                rate_state = self.rate_limiter.get_state()
                if rate_state.tokens_available < 2:  # Need at least 2 tokens (init + search)
                    logger.info("Insufficient rate limit tokens, pausing job")
                    self._state.checkpoint_data["paused_reason"] = "rate_limit"
                    self._update_job_state()
                    break

                # Check max requests for this run
                if self._state.requests_used >= self.max_requests:
                    logger.info("Reached max requests for this run (%d)", self.max_requests)
                    self._state.checkpoint_data["paused_reason"] = "max_requests"
                    self._update_job_state()
                    break

                # Update checkpoint
                self._state.current_keyword_index = i

                logger.info("Searching keyword %d/%d: %s", i + 1, len(self.keywords), keyword)

                try:
                    # Acquire rate limit token
                    if not self.rate_limiter.acquire(count=2, block=False):
                        logger.warning("Could not acquire rate limit token")
                        break

                    self._state.requests_used += 2  # Init + search

                    # Search with retries
                    results_count = 0
                    for result in self.source.search(
                        keywords=[keyword],
                        keyword_mode="all",
                        max_results=50,
                    ):
                        results_count += 1
                        if not dry_run:
                            self._process_result(result)

                    logger.info("Found %d results for '%s'", results_count, keyword)
                    self._state.keywords_completed += 1

                except Exception as e:
                    logger.error("Error searching '%s': %s", keyword, e)
                    self._state.last_error = str(e)
                    self._state.error_count += 1

                    # Continue to next keyword unless too many errors
                    if self._state.error_count >= 5:
                        logger.error("Too many errors, stopping job")
                        self._state.status = "failed"
                        break

                # Save progress periodically
                self._update_job_state()

            # Mark job as completed if we processed all keywords
            if self._state.current_keyword_index >= len(self.keywords) - 1:
                self._state.status = "completed"
                self._state.completed_at = datetime.utcnow().isoformat()

        except Exception as e:
            logger.exception("Job failed with error: %s", e)
            self._state.status = "failed"
            self._state.last_error = str(e)

        finally:
            self._update_job_state()

        return {
            "job_id": self._state.job_id,
            "status": self._state.status,
            "keywords_completed": self._state.keywords_completed,
            "keywords_total": self._state.keywords_total,
            "companies_found": self._state.companies_found,
            "companies_new": self._state.companies_new,
            "requests_used": self._state.requests_used,
            "errors": self._state.error_count,
        }


def run_discovery_job(
    db_path: str,
    max_requests: int = 20,
    resume: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Convenience function to run discovery job.

    Args:
        db_path: Path to SQLite database
        max_requests: Maximum requests per run
        resume: Whether to resume incomplete jobs
        dry_run: Don't save to database

    Returns:
        Statistics dict
    """
    db = Database(db_path)
    rate_limiter = PersistentRateLimiter(db_path)

    try:
        job = DiscoveryJob(
            db=db,
            rate_limiter=rate_limiter,
            max_requests=max_requests,
        )
        return job.run(resume=resume, dry_run=dry_run)
    finally:
        db.close()
