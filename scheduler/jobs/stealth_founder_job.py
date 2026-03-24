"""
Stealth Founder Discovery Job.

Combines Google search and LinkedIn scraping to find potential
stealth founders in Germany and store them in the database.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

from sources.google_search import DuckDuckGoSearchScraper, SearchResult
from sources.linkedin_scraper import (
    LinkedInProfile,
    LinkedInProfileScraper,
    StealthFounderDetector,
)

logger = logging.getLogger(__name__)


class StealthFounderJob:
    """
    Job to discover stealth founders via Google search and LinkedIn scraping.
    """

    def __init__(
        self,
        db,
        max_queries: int = 5,
        max_profiles_to_scrape: int = 20,
        min_confidence: float = 0.3,
        google_delay: tuple = (15, 45),
        linkedin_delay: tuple = (5, 15),
    ):
        """
        Args:
            db: Database instance
            max_queries: Maximum Google queries to run per job
            max_profiles_to_scrape: Maximum LinkedIn profiles to scrape per job
            min_confidence: Minimum confidence to store a founder
            google_delay: Delay range for Google requests
            linkedin_delay: Delay range for LinkedIn requests
        """
        self.db = db
        self.max_queries = max_queries
        self.max_profiles_to_scrape = max_profiles_to_scrape
        self.min_confidence = min_confidence
        self.google_delay = google_delay
        self.linkedin_delay = linkedin_delay

        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure the stealth_founders table exists."""
        cursor = self.db.conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stealth_founders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                linkedin_url TEXT UNIQUE,
                name TEXT,
                headline TEXT,
                location TEXT,
                summary TEXT,
                current_company TEXT,
                previous_companies TEXT,
                detection_source TEXT,
                search_query TEXT,
                stealth_signals TEXT,
                confidence_score REAL DEFAULT 0.0,
                first_seen_at TEXT,
                last_checked_at TEXT,
                profile_changed INTEGER DEFAULT 0,
                company_id INTEGER REFERENCES companies(id),
                emerged_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_stealth_founders_confidence ON stealth_founders(confidence_score DESC)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stealth_founders_location ON stealth_founders(location)")

        self.db.conn.commit()

    def _get_existing_urls(self) -> set:
        """Get URLs already in database."""
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT linkedin_url FROM stealth_founders")
        return {row[0] for row in cursor.fetchall()}

    def _store_founder(self, profile: LinkedInProfile, search_query: str):
        """Store or update a stealth founder in the database."""
        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        # Check if exists
        cursor.execute("SELECT id, confidence_score FROM stealth_founders WHERE linkedin_url = ?", (profile.url,))
        existing = cursor.fetchone()

        if existing:
            # Update existing record
            cursor.execute(
                """
                UPDATE stealth_founders SET
                    name = COALESCE(?, name),
                    headline = COALESCE(?, headline),
                    location = COALESCE(?, location),
                    summary = COALESCE(?, summary),
                    stealth_signals = ?,
                    confidence_score = ?,
                    last_checked_at = ?,
                    profile_changed = CASE
                        WHEN headline != ? OR confidence_score != ? THEN 1
                        ELSE profile_changed
                    END
                WHERE linkedin_url = ?
            """,
                (
                    profile.name,
                    profile.headline,
                    profile.location,
                    profile.summary,
                    json.dumps(profile.stealth_signals) if profile.stealth_signals else None,
                    profile.confidence_score,
                    now,
                    profile.headline,
                    profile.confidence_score,
                    profile.url,
                ),
            )
            logger.debug(f"Updated: {profile.name}")
        else:
            # Insert new record
            cursor.execute(
                """
                INSERT INTO stealth_founders (
                    linkedin_url, name, headline, location, summary,
                    previous_companies, detection_source, search_query,
                    stealth_signals, confidence_score,
                    first_seen_at, last_checked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    profile.url,
                    profile.name,
                    profile.headline,
                    profile.location,
                    profile.summary,
                    json.dumps(profile.previous_companies) if profile.previous_companies else None,
                    "google_linkedin_scrape",
                    search_query,
                    json.dumps(profile.stealth_signals) if profile.stealth_signals else None,
                    profile.confidence_score,
                    now,
                    now,
                    now,
                ),
            )
            logger.info(f"New founder: {profile.name} (conf={profile.confidence_score:.2f})")

        self.db.conn.commit()

    def run(self) -> Dict[str, Any]:
        """
        Run the stealth founder discovery job.

        Returns:
            Statistics about the job run
        """
        stats = {
            "queries_run": 0,
            "urls_found": 0,
            "new_urls": 0,
            "profiles_scraped": 0,
            "founders_stored": 0,
            "high_confidence": 0,
            "errors": 0,
        }

        existing_urls = self._get_existing_urls()
        logger.info(f"Starting stealth founder job. {len(existing_urls)} existing URLs in DB.")

        # Phase 1: DuckDuckGo Search for LinkedIn URLs
        logger.info("Phase 1: DuckDuckGo search for LinkedIn profiles...")

        ddg_scraper = DuckDuckGoSearchScraper(delay_range=self.google_delay)

        # DuckDuckGo-friendly queries (no site: operator)
        ddg_queries = [
            "linkedin.com/in stealth founder germany",
            "linkedin.com/in stealth founder berlin",
            "linkedin.com/in stealth mode founder berlin",
            'linkedin.com/in "building something new" founder germany',
            'linkedin.com/in "ex-google" founder germany',
            'linkedin.com/in "ex-stripe" founder berlin',
            "linkedin.com/in stealth co-founder munich",
            'linkedin.com/in "serial entrepreneur" berlin',
        ]
        queries = ddg_queries[: self.max_queries]

        all_results: List[SearchResult] = []
        for i, query in enumerate(queries):
            logger.info(f"Query {i + 1}/{len(queries)}")
            try:
                results = ddg_scraper.search_query(query)
                all_results.extend(results)
                stats["queries_run"] += 1
            except Exception as e:
                logger.error(f"DuckDuckGo search failed: {e}")
                stats["errors"] += 1

            if i < len(queries) - 1:
                ddg_scraper._delay()

        stats["urls_found"] = len(ddg_scraper.found_urls)

        # Filter to new URLs only
        new_urls = [r.url for r in all_results if r.url not in existing_urls]
        new_urls = list(dict.fromkeys(new_urls))  # Dedupe preserving order
        stats["new_urls"] = len(new_urls)

        logger.info(f"Found {stats['urls_found']} URLs, {stats['new_urls']} are new")

        if not new_urls:
            logger.info("No new URLs to scrape")
            return stats

        # Phase 2: Scrape LinkedIn profiles
        logger.info("Phase 2: Scraping LinkedIn profiles...")

        urls_to_scrape = new_urls[: self.max_profiles_to_scrape]
        linkedin_scraper = LinkedInProfileScraper(delay_range=self.linkedin_delay)
        detector = StealthFounderDetector(min_confidence=self.min_confidence)

        # Map URLs to their search queries
        url_to_query = {r.url: r.query for r in all_results}

        for i, url in enumerate(urls_to_scrape):
            logger.info(f"Profile {i + 1}/{len(urls_to_scrape)}")

            try:
                profile = linkedin_scraper.scrape_profile(url)
                stats["profiles_scraped"] += 1

                if profile:
                    if detector.is_stealth_founder(profile):
                        search_query = url_to_query.get(url, "")
                        self._store_founder(profile, search_query)
                        stats["founders_stored"] += 1

                        if profile.confidence_score >= 0.6:
                            stats["high_confidence"] += 1

            except Exception as e:
                logger.error(f"Failed to scrape {url}: {e}")
                stats["errors"] += 1

            if i < len(urls_to_scrape) - 1:
                linkedin_scraper._delay()

        logger.info(
            f"Job complete: {stats['founders_stored']} founders stored, {stats['high_confidence']} high confidence"
        )

        return stats

    def get_top_founders(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get top stealth founders by confidence score."""
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            SELECT
                linkedin_url, name, headline, location,
                stealth_signals, confidence_score, first_seen_at
            FROM stealth_founders
            WHERE confidence_score >= ?
            ORDER BY confidence_score DESC
            LIMIT ?
        """,
            (self.min_confidence, limit),
        )

        results = []
        for row in cursor.fetchall():
            results.append(
                {
                    "url": row[0],
                    "name": row[1],
                    "headline": row[2],
                    "location": row[3],
                    "signals": json.loads(row[4]) if row[4] else [],
                    "confidence": row[5],
                    "first_seen": row[6],
                }
            )

        return results


def run_stealth_discovery(
    db_path: str = "handelsregister.db",
    max_queries: int = 3,
    max_profiles: int = 10,
) -> Dict[str, Any]:
    """
    Convenience function to run stealth founder discovery.

    Args:
        db_path: Path to database
        max_queries: Number of Google queries
        max_profiles: Number of profiles to scrape

    Returns:
        Job statistics
    """
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = StealthFounderJob(
            db=db,
            max_queries=max_queries,
            max_profiles_to_scrape=max_profiles,
        )
        return job.run()
    finally:
        db.close()


def import_and_scrape_urls(
    urls: List[str],
    db_path: str = "handelsregister.db",
    min_confidence: float = 0.1,
) -> Dict[str, Any]:
    """
    Import LinkedIn URLs from external sources and scrape them.

    Use this when search engines are rate-limited. You can:
    1. Manually find URLs via browser
    2. Export from LinkedIn Sales Navigator
    3. Use other data sources

    Args:
        urls: List of LinkedIn profile URLs
        db_path: Path to database
        min_confidence: Minimum confidence to store

    Returns:
        Statistics about the import
    """
    from persistence.database import Database

    stats = {
        "urls_provided": len(urls),
        "profiles_scraped": 0,
        "founders_stored": 0,
        "high_confidence": 0,
        "errors": 0,
    }

    db = Database(db_path)
    try:
        job = StealthFounderJob(
            db=db,
            min_confidence=min_confidence,
            linkedin_delay=(3, 8),
        )

        # Get existing URLs to skip
        existing = job._get_existing_urls()
        new_urls = [u for u in urls if u not in existing]

        logger.info(f"Importing {len(new_urls)} new URLs ({len(urls) - len(new_urls)} already exist)")

        linkedin_scraper = LinkedInProfileScraper(delay_range=(3, 8))
        detector = StealthFounderDetector(min_confidence=min_confidence)

        for i, url in enumerate(new_urls):
            logger.info(f"Scraping {i + 1}/{len(new_urls)}: {url}")

            try:
                profile = linkedin_scraper.scrape_profile(url)
                stats["profiles_scraped"] += 1

                if profile and profile.name:
                    if detector.is_stealth_founder(profile):
                        job._store_founder(profile, "manual_import")
                        stats["founders_stored"] += 1

                        if profile.confidence_score >= 0.6:
                            stats["high_confidence"] += 1

                        logger.info(f"  -> Stored: {profile.name} (conf={profile.confidence_score:.2f})")
                    else:
                        logger.info(f"  -> Below threshold: {profile.name} (conf={profile.confidence_score:.2f})")
                else:
                    logger.warning("  -> Could not extract profile data")

            except Exception as e:
                logger.error(f"  -> Error: {e}")
                stats["errors"] += 1

            if i < len(new_urls) - 1:
                linkedin_scraper._delay()

        return stats

    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Test run with limited scope
    stats = run_stealth_discovery(
        max_queries=2,
        max_profiles=5,
    )

    print("\n" + "=" * 50)
    print("STEALTH FOUNDER DISCOVERY RESULTS")
    print("=" * 50)
    for k, v in stats.items():
        print(f"  {k}: {v}")
