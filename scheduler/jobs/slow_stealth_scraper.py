"""
Slow continuous stealth founder scraper.

Runs at 1 request per minute to avoid rate limiting.
Designed to run continuously in the background, gradually
building up a database of stealth founders over time.
"""

import logging
import json
import time
import random
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


# Search queries to cycle through
STEALTH_QUERIES = [
    'linkedin.com/in stealth founder germany',
    'linkedin.com/in stealth founder berlin',
    'linkedin.com/in stealth mode founder germany',
    'linkedin.com/in "building something new" founder germany',
    'linkedin.com/in "building something new" founder berlin',
    'linkedin.com/in "ex-google" founder germany',
    'linkedin.com/in "ex-google" founder berlin',
    'linkedin.com/in "ex-meta" founder germany',
    'linkedin.com/in "ex-stripe" founder germany',
    'linkedin.com/in "ex-amazon" founder berlin',
    'linkedin.com/in stealth co-founder munich',
    'linkedin.com/in stealth co-founder berlin',
    'linkedin.com/in "serial entrepreneur" berlin',
    'linkedin.com/in "serial entrepreneur" germany',
    'linkedin.com/in "working on something exciting" germany',
    'linkedin.com/in "next chapter" founder berlin',
    'linkedin.com/in "exploring opportunities" founder germany',
    'linkedin.com/in stealth startup founder hamburg',
    'linkedin.com/in stealth startup founder frankfurt',
    'linkedin.com/in "ex-n26" founder',
    'linkedin.com/in "ex-zalando" founder',
    'linkedin.com/in "ex-delivery hero" founder',
    'linkedin.com/in "ex-celonis" founder',
    'linkedin.com/in "angel investor" building berlin',
    'linkedin.com/in "entrepreneur in residence" germany',
]


class SlowStealthScraper:
    """
    Slow, continuous scraper that runs at ~1 request per minute.

    Designed to avoid rate limiting by:
    - Long delays between requests (60+ seconds)
    - Random jitter on delays
    - Rotating through queries
    - Persisting state for resume
    """

    def __init__(
        self,
        db,
        state_file: str = 'data/stealth_scraper_state.json',
        search_delay: int = 60,  # Seconds between search requests
        scrape_delay: int = 90,  # Seconds between LinkedIn scrapes
        jitter: float = 0.3,     # Random jitter (0.3 = ±30%)
    ):
        self.db = db
        self.state_file = state_file
        self.search_delay = search_delay
        self.scrape_delay = scrape_delay
        self.jitter = jitter

        self.state = self._load_state()
        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure stealth_founders table exists."""
        cursor = self.db.conn.cursor()
        cursor.execute('''
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
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stealth_founders_confidence ON stealth_founders(confidence_score DESC)')
        self.db.conn.commit()

    def _load_state(self) -> Dict[str, Any]:
        """Load scraper state from file."""
        default_state = {
            'query_index': 0,
            'pending_urls': [],
            'processed_urls': [],
            'last_search_at': None,
            'last_scrape_at': None,
            'total_searches': 0,
            'total_scrapes': 0,
            'total_founders_found': 0,
        }

        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    # Merge with defaults for any missing keys
                    for key, value in default_state.items():
                        if key not in state:
                            state[key] = value
                    return state
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

        return default_state

    def _save_state(self):
        """Save scraper state to file."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    def _get_delay(self, base_delay: int) -> float:
        """Get delay with random jitter."""
        jitter_amount = base_delay * self.jitter
        return base_delay + random.uniform(-jitter_amount, jitter_amount)

    def _get_existing_urls(self) -> set:
        """Get URLs already in database."""
        cursor = self.db.conn.cursor()
        cursor.execute('SELECT linkedin_url FROM stealth_founders')
        return {row[0] for row in cursor.fetchall()}

    def _store_founder(self, profile, search_query: str):
        """Store a founder in the database."""
        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute('''
            INSERT OR REPLACE INTO stealth_founders (
                linkedin_url, name, headline, location, summary,
                detection_source, search_query, stealth_signals,
                confidence_score, first_seen_at, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            profile.url,
            profile.name,
            profile.headline,
            profile.location,
            profile.summary,
            'slow_scraper',
            search_query,
            json.dumps(profile.stealth_signals) if profile.stealth_signals else None,
            profile.confidence_score,
            now,
            now,
            now,
        ))
        self.db.conn.commit()
        logger.info(f"Stored founder: {profile.name} (conf={profile.confidence_score:.2f})")

    def _do_search(self) -> List[str]:
        """Perform one search query and return new URLs found."""
        from sources.google_search import DuckDuckGoSearchScraper

        # Get current query
        query = STEALTH_QUERIES[self.state['query_index']]

        logger.info(f"Searching: {query}")

        scraper = DuckDuckGoSearchScraper(delay_range=(1, 2), use_cloudscraper=True)

        try:
            results = scraper.search_query(query)

            # Get existing URLs to filter
            existing = self._get_existing_urls()
            existing.update(self.state['processed_urls'])
            existing.update(self.state['pending_urls'])

            # Extract new URLs
            new_urls = []
            for r in results:
                if r.url not in existing:
                    new_urls.append(r.url)
                    logger.info(f"  Found: {r.title[:50]}")

            # Update state
            self.state['query_index'] = (self.state['query_index'] + 1) % len(STEALTH_QUERIES)
            self.state['last_search_at'] = datetime.now().isoformat()
            self.state['total_searches'] += 1
            self.state['pending_urls'].extend(new_urls)
            self._save_state()

            logger.info(f"  Found {len(new_urls)} new URLs, {len(self.state['pending_urls'])} pending")

            return new_urls

        except Exception as e:
            logger.error(f"Search failed: {e}")
            # Move to next query anyway
            self.state['query_index'] = (self.state['query_index'] + 1) % len(STEALTH_QUERIES)
            self._save_state()
            return []

    def _do_scrape(self) -> Optional[Dict[str, Any]]:
        """Scrape one LinkedIn profile from pending queue."""
        from sources.linkedin_scraper import LinkedInProfileScraper, StealthFounderDetector

        if not self.state['pending_urls']:
            return None

        # Get next URL
        url = self.state['pending_urls'].pop(0)

        logger.info(f"Scraping: {url}")

        scraper = LinkedInProfileScraper(delay_range=(1, 2), use_cloudscraper=True)
        detector = StealthFounderDetector(min_confidence=0.1)

        try:
            profile = scraper.scrape_profile(url)

            result = {
                'url': url,
                'success': False,
                'name': None,
                'confidence': 0,
                'stored': False,
            }

            if profile and profile.name:
                result['success'] = True
                result['name'] = profile.name
                result['confidence'] = profile.confidence_score

                if detector.is_stealth_founder(profile):
                    # Get the query that found this URL (approximate)
                    query_idx = max(0, self.state['query_index'] - 1)
                    search_query = STEALTH_QUERIES[query_idx]

                    self._store_founder(profile, search_query)
                    result['stored'] = True
                    self.state['total_founders_found'] += 1

                    logger.info(f"  Stored: {profile.name} (conf={profile.confidence_score:.2f})")
                else:
                    logger.info(f"  Below threshold: {profile.name} (conf={profile.confidence_score:.2f})")
            else:
                logger.warning(f"  Could not extract profile data")

            # Update state
            self.state['processed_urls'].append(url)
            # Keep processed_urls bounded
            if len(self.state['processed_urls']) > 1000:
                self.state['processed_urls'] = self.state['processed_urls'][-500:]

            self.state['last_scrape_at'] = datetime.now().isoformat()
            self.state['total_scrapes'] += 1
            self._save_state()

            return result

        except Exception as e:
            logger.error(f"Scrape failed: {e}")
            self.state['processed_urls'].append(url)
            self._save_state()
            return {'url': url, 'success': False, 'error': str(e)}

    def run_once(self) -> Dict[str, Any]:
        """
        Run one cycle: either search or scrape.

        Alternates between searching for new URLs and scraping pending URLs.
        Returns stats about what was done.
        """
        stats = {
            'action': None,
            'success': False,
            'details': {},
        }

        # Decide whether to search or scrape
        # If we have pending URLs, scrape them first
        if self.state['pending_urls']:
            stats['action'] = 'scrape'
            result = self._do_scrape()
            if result:
                stats['success'] = result.get('success', False)
                stats['details'] = result
        else:
            stats['action'] = 'search'
            new_urls = self._do_search()
            stats['success'] = len(new_urls) > 0
            stats['details'] = {'new_urls': len(new_urls)}

        return stats

    def run_continuous(self, max_iterations: int = None):
        """
        Run continuously with proper delays.

        Args:
            max_iterations: Maximum iterations (None = run forever)
        """
        iteration = 0

        logger.info("Starting slow continuous scraper...")
        logger.info(f"  Search delay: {self.search_delay}s (±{self.jitter*100:.0f}%)")
        logger.info(f"  Scrape delay: {self.scrape_delay}s (±{self.jitter*100:.0f}%)")
        logger.info(f"  State file: {self.state_file}")
        logger.info(f"  Pending URLs: {len(self.state['pending_urls'])}")

        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1

                # Run one cycle
                stats = self.run_once()

                logger.info(f"[{iteration}] {stats['action']}: {stats['details']}")

                # Choose delay based on action
                if stats['action'] == 'search':
                    delay = self._get_delay(self.search_delay)
                else:
                    delay = self._get_delay(self.scrape_delay)

                logger.info(f"  Sleeping {delay:.0f}s...")
                time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Stopped by user")
            self._save_state()

    def get_stats(self) -> Dict[str, Any]:
        """Get current scraper statistics."""
        cursor = self.db.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM stealth_founders')
        total_in_db = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM stealth_founders WHERE confidence_score >= 0.5')
        high_confidence = cursor.fetchone()[0]

        return {
            'total_in_db': total_in_db,
            'high_confidence': high_confidence,
            'pending_urls': len(self.state['pending_urls']),
            'total_searches': self.state['total_searches'],
            'total_scrapes': self.state['total_scrapes'],
            'total_founders_found': self.state['total_founders_found'],
            'current_query_index': self.state['query_index'],
            'current_query': STEALTH_QUERIES[self.state['query_index']],
            'last_search_at': self.state['last_search_at'],
            'last_scrape_at': self.state['last_scrape_at'],
        }


def run_slow_scraper(
    db_path: str = 'handelsregister.db',
    search_delay: int = 60,
    scrape_delay: int = 90,
    max_iterations: int = None,
):
    """
    Convenience function to run the slow scraper.

    Args:
        db_path: Path to database
        search_delay: Seconds between search requests (default 60)
        scrape_delay: Seconds between LinkedIn scrapes (default 90)
        max_iterations: Max iterations (None = run forever)
    """
    from persistence.database import Database

    db = Database(db_path)
    try:
        scraper = SlowStealthScraper(
            db=db,
            search_delay=search_delay,
            scrape_delay=scrape_delay,
        )
        scraper.run_continuous(max_iterations=max_iterations)
    finally:
        db.close()


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    print("=" * 60)
    print("SLOW STEALTH FOUNDER SCRAPER")
    print("=" * 60)
    print()
    print("This scraper runs at ~1 request per minute to avoid blocks.")
    print("It will gradually build up a database of stealth founders.")
    print()
    print("Press Ctrl+C to stop (state is saved automatically)")
    print()

    # Run with 10 iterations for testing, or None for continuous
    run_slow_scraper(
        search_delay=60,   # 1 minute between searches
        scrape_delay=90,   # 1.5 minutes between LinkedIn scrapes
        max_iterations=10, # Remove this for continuous running
    )
