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


# Search queries to cycle through (DACH region: Germany, Austria, Switzerland)
STEALTH_QUERIES = [
    # === GERMANY ===
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

    # === AUSTRIA ===
    'linkedin.com/in stealth founder austria',
    'linkedin.com/in stealth founder vienna',
    'linkedin.com/in stealth founder wien',
    'linkedin.com/in "building something new" founder austria',
    'linkedin.com/in "ex-google" founder vienna',
    'linkedin.com/in stealth co-founder graz',
    'linkedin.com/in "serial entrepreneur" vienna',
    'linkedin.com/in "serial entrepreneur" austria',
    'linkedin.com/in stealth startup founder salzburg',

    # === SWITZERLAND ===
    'linkedin.com/in stealth founder switzerland',
    'linkedin.com/in stealth founder zurich',
    'linkedin.com/in stealth founder zürich',
    'linkedin.com/in "building something new" founder switzerland',
    'linkedin.com/in "ex-google" founder zurich',
    'linkedin.com/in stealth co-founder geneva',
    'linkedin.com/in "serial entrepreneur" zurich',
    'linkedin.com/in "serial entrepreneur" switzerland',
    'linkedin.com/in stealth startup founder basel',
    'linkedin.com/in "crypto" founder zug',
]


class SlowStealthScraper:
    """
    Slow, continuous scraper that runs at ~1 request per minute.

    Designed to avoid rate limiting by:
    - Long delays between requests (60+ seconds)
    - Random jitter on delays
    - Rotating through queries
    - Persisting state for resume
    - Exponential backoff on rate limits
    """

    # Backoff settings
    MAX_CONSECUTIVE_FAILURES = 5
    BACKOFF_MULTIPLIER = 2
    MAX_BACKOFF_MINUTES = 60

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
        self.consecutive_failures = 0

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

    def _calculate_backoff(self) -> int:
        """Calculate backoff delay based on consecutive failures."""
        if self.consecutive_failures == 0:
            return 0

        # Exponential backoff: 2^failures minutes, capped at MAX_BACKOFF_MINUTES
        backoff_minutes = min(
            self.BACKOFF_MULTIPLIER ** self.consecutive_failures,
            self.MAX_BACKOFF_MINUTES
        )
        return backoff_minutes * 60  # Return seconds

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
            'skipped_non_german': 0,
            'skipped_low_confidence': 0,
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

    def _get_founders_to_recheck(self, days_old: int = 7, limit: int = 1) -> List[Dict]:
        """Get founders that haven't been checked in X days."""
        cursor = self.db.conn.cursor()
        cursor.execute('''
            SELECT id, linkedin_url, name, headline
            FROM stealth_founders
            WHERE last_checked_at IS NULL
               OR datetime(last_checked_at) < datetime('now', ?)
            ORDER BY last_checked_at ASC NULLS FIRST
            LIMIT ?
        ''', (f'-{days_old} days', limit))

        return [
            {'id': row[0], 'url': row[1], 'name': row[2], 'headline': row[3]}
            for row in cursor.fetchall()
        ]

    def _update_founder_check(self, founder_id: int, new_headline: str = None, changed: bool = False):
        """Update last_checked_at and detect changes."""
        cursor = self.db.conn.cursor()
        cursor.execute('''
            UPDATE stealth_founders
            SET last_checked_at = ?,
                profile_changed = CASE WHEN ? THEN 1 ELSE profile_changed END,
                headline = CASE WHEN ? IS NOT NULL THEN ? ELSE headline END
            WHERE id = ?
        ''', (datetime.now().isoformat(), changed, new_headline, new_headline, founder_id))
        self.db.conn.commit()

    def _get_existing_urls(self) -> set:
        """Get URLs already in database."""
        cursor = self.db.conn.cursor()
        cursor.execute('SELECT linkedin_url FROM stealth_founders')
        return {row[0] for row in cursor.fetchall()}

    def _parse_search_title(self, title: str) -> tuple:
        """
        Parse LinkedIn search result title to extract name and headline.

        Examples:
            "Kristina Lunz - Co-Founder/CEO STEALTH | LinkedIn" -> ("Kristina Lunz", "Co-Founder/CEO STEALTH")
            "John Doe - Building something new | LinkedIn" -> ("John Doe", "Building something new")
        """
        # Remove common suffixes
        title = title.replace(' | LinkedIn', '').replace(' - LinkedIn', '').strip()

        # Split on first " - " to get name and headline
        if ' - ' in title:
            parts = title.split(' - ', 1)
            name = parts[0].strip()
            headline = parts[1].strip() if len(parts) > 1 else None
        else:
            name = title
            headline = None

        return name, headline

    def _calculate_snippet_confidence(self, title: str, snippet: str) -> tuple:
        """
        Calculate confidence score from search snippet without scraping LinkedIn.

        Returns: (confidence_score, detected_signals)
        """
        text = f"{title} {snippet}".lower()
        signals = []
        score = 0.0

        # Stealth keywords (strong signal)
        stealth_words = ['stealth', 'building something', 'something new', 'pre-launch', 'unannounced']
        for word in stealth_words:
            if word in text:
                signals.append(word)
                score += 0.15

        # Founder keywords
        founder_words = ['founder', 'co-founder', 'cofounder', 'gründer', 'ceo', 'entrepreneur']
        for word in founder_words:
            if word in text:
                signals.append(word)
                score += 0.1

        # High-value background
        companies = ['google', 'meta', 'facebook', 'amazon', 'stripe', 'microsoft', 'apple', 'n26', 'zalando']
        for company in companies:
            if f"ex-{company}" in text or f"ex {company}" in text or f"former {company}" in text:
                signals.append(f"ex-{company}")
                score += 0.1

        # DACH location indicators
        dach_indicators = ['germany', 'berlin', 'munich', 'hamburg', 'austria', 'vienna', 'switzerland', 'zurich']
        for loc in dach_indicators:
            if loc in text:
                signals.append(f"loc:{loc}")
                score += 0.05
                break  # Only count location once

        return min(score, 1.0), signals

    def _store_from_search_result(self, result, search_query: str):
        """
        Store a founder directly from search snippet - NO LinkedIn scrape needed!

        This avoids 999 blocks by extracting info from DuckDuckGo results.
        """
        from sources.linkedin_scraper import is_dach_location

        name, headline = self._parse_search_title(result.title)
        confidence, signals = self._calculate_snippet_confidence(result.title, result.snippet)

        # Check if likely DACH based on search result
        combined_text = f"{result.title} {result.snippet}"
        is_dach = is_dach_location(combined_text, headline, result.snippet)

        # Skip if not DACH or too low confidence
        if not is_dach:
            logger.debug(f"  Skipping non-DACH: {name}")
            self.state['skipped_non_german'] = self.state.get('skipped_non_german', 0) + 1
            return

        if confidence < 0.1:
            logger.debug(f"  Skipping low confidence: {name} ({confidence:.2f})")
            self.state['skipped_low_confidence'] = self.state.get('skipped_low_confidence', 0) + 1
            return

        # Store in database
        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute('''
            INSERT OR IGNORE INTO stealth_founders (
                linkedin_url, name, headline, summary,
                detection_source, search_query, stealth_signals,
                confidence_score, first_seen_at, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            result.url,
            name,
            headline,
            result.snippet,  # Use snippet as summary
            'search_snippet',
            search_query,
            json.dumps(signals) if signals else None,
            confidence,
            now,
            now,
            now,
        ))

        if cursor.rowcount > 0:
            self.db.conn.commit()
            self.state['total_founders_found'] = self.state.get('total_founders_found', 0) + 1
            logger.info(f"  Stored from snippet: {name} (conf={confidence:.2f}, signals={signals})")

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

    def _do_search(self, max_pages: int = 2, use_multi_engine: bool = True) -> List[str]:
        """
        Perform one search query and return new URLs found.

        Args:
            max_pages: Number of result pages to fetch (default 2 = ~60 results)
            use_multi_engine: Rotate between DuckDuckGo and Brave
        """
        from sources.google_search import DuckDuckGoSearchScraper, MultiSearchScraper

        # Get current query
        query = STEALTH_QUERIES[self.state['query_index']]

        logger.info(f"Searching: {query}")

        if use_multi_engine:
            scraper = MultiSearchScraper(delay_range=(2, 5))
        else:
            scraper = DuckDuckGoSearchScraper(delay_range=(2, 5), use_cloudscraper=True)

        try:
            # Fetch results (multi-engine handles pagination internally)
            if use_multi_engine:
                results = scraper.search_query(query, max_pages=max_pages)
            else:
                results = scraper.search_query(query, max_pages=max_pages)

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

                    # Store founder directly from search snippet (no LinkedIn scrape needed!)
                    self._store_from_search_result(r, query)

            # Update state
            self.state['query_index'] = (self.state['query_index'] + 1) % len(STEALTH_QUERIES)
            self.state['last_search_at'] = datetime.now().isoformat()
            self.state['total_searches'] += 1
            # Don't add to pending_urls since we already stored from snippet
            # self.state['pending_urls'].extend(new_urls)
            self._save_state()

            logger.info(f"  Stored {len(new_urls)} founders from search snippets")

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
        # Require German location for filtering
        detector = StealthFounderDetector(min_confidence=0.1, require_german_location=True)

        try:
            profile = scraper.scrape_profile(url)

            result = {
                'url': url,
                'success': False,
                'name': None,
                'confidence': 0,
                'stored': False,
                'location': None,
                'is_german': False,
            }

            if profile and profile.name:
                result['success'] = True
                result['name'] = profile.name
                result['confidence'] = profile.confidence_score
                result['location'] = profile.location
                result['is_german'] = detector.is_german(profile)

                if detector.is_stealth_founder(profile):
                    # Get the query that found this URL (approximate)
                    query_idx = max(0, self.state['query_index'] - 1)
                    search_query = STEALTH_QUERIES[query_idx]

                    self._store_founder(profile, search_query)
                    result['stored'] = True
                    self.state['total_founders_found'] += 1

                    logger.info(f"  Stored: {profile.name} (conf={profile.confidence_score:.2f}, loc={profile.location})")
                elif not result['is_german']:
                    logger.info(f"  Not in Germany: {profile.name} (loc={profile.location})")
                    self.state['skipped_non_german'] = self.state.get('skipped_non_german', 0) + 1
                else:
                    logger.info(f"  Below threshold: {profile.name} (conf={profile.confidence_score:.2f})")
                    self.state['skipped_low_confidence'] = self.state.get('skipped_low_confidence', 0) + 1
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

    def _do_recheck(self) -> Optional[Dict[str, Any]]:
        """Re-check an existing founder for profile changes."""
        from sources.linkedin_scraper import LinkedInProfileScraper

        founders = self._get_founders_to_recheck(days_old=7, limit=1)
        if not founders:
            return None

        founder = founders[0]
        url = founder['url']
        old_headline = founder['headline']

        logger.info(f"Re-checking: {founder['name']} ({url})")

        scraper = LinkedInProfileScraper(delay_range=(1, 2), use_cloudscraper=True)

        try:
            profile = scraper.scrape_profile(url)

            result = {
                'url': url,
                'name': founder['name'],
                'success': False,
                'changed': False,
            }

            if profile and profile.name:
                result['success'] = True
                new_headline = profile.headline

                # Detect if profile changed (headline different = may have emerged)
                if old_headline and new_headline and old_headline != new_headline:
                    result['changed'] = True
                    logger.info(f"  CHANGED: '{old_headline[:40]}...' -> '{new_headline[:40]}...'")

                    # Check if they emerged from stealth
                    if 'stealth' in old_headline.lower() and 'stealth' not in new_headline.lower():
                        logger.info(f"  EMERGED FROM STEALTH!")
                        cursor = self.db.conn.cursor()
                        cursor.execute('''
                            UPDATE stealth_founders SET emerged_at = ? WHERE id = ?
                        ''', (datetime.now().isoformat(), founder['id']))
                        self.db.conn.commit()

                self._update_founder_check(founder['id'], new_headline, result['changed'])
            else:
                logger.warning(f"  Could not fetch profile")
                # Still update last_checked_at
                self._update_founder_check(founder['id'])

            return result

        except Exception as e:
            logger.error(f"Re-check failed: {e}")
            return {'url': url, 'success': False, 'error': str(e)}

    def run_once(self, iteration: int = 0) -> Dict[str, Any]:
        """
        Run one cycle: search, scrape, or re-check.

        Alternates between:
        - Scraping pending URLs (priority)
        - Searching for new URLs
        - Re-checking existing founders (every 10 iterations)

        Returns stats about what was done.
        """
        stats = {
            'action': None,
            'success': False,
            'details': {},
        }

        # Every 10 iterations, re-check an existing founder
        if iteration > 0 and iteration % 10 == 0:
            result = self._do_recheck()
            if result:
                stats['action'] = 'recheck'
                stats['success'] = result.get('success', False)
                stats['details'] = result
                return stats

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
        Run continuously with proper delays and exponential backoff on failures.

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

                # Run one cycle (pass iteration for periodic re-checks)
                stats = self.run_once(iteration=iteration)

                logger.info(f"[{iteration}] {stats['action']}: {stats['details']}")

                # Track consecutive failures for backoff
                if stats['success']:
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        backoff = self._calculate_backoff()
                        logger.warning(f"  {self.consecutive_failures} consecutive failures - backing off for {backoff/60:.0f} minutes")
                        time.sleep(backoff)
                        continue

                # Choose delay based on action
                if stats['action'] == 'search':
                    delay = self._get_delay(self.search_delay)
                else:
                    delay = self._get_delay(self.scrape_delay)

                # Add extra delay if we had a failure (but not yet at max)
                if not stats['success'] and self.consecutive_failures > 0:
                    extra_delay = self.consecutive_failures * 30  # 30s extra per failure
                    delay += extra_delay
                    logger.info(f"  Adding {extra_delay}s extra delay due to {self.consecutive_failures} failure(s)")

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

        cursor.execute('SELECT COUNT(*) FROM stealth_founders WHERE emerged_at IS NOT NULL')
        emerged_count = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM stealth_founders WHERE profile_changed = 1')
        changed_count = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*) FROM stealth_founders
            WHERE last_checked_at IS NULL
               OR datetime(last_checked_at) < datetime('now', '-7 days')
        ''')
        needs_recheck = cursor.fetchone()[0]

        return {
            'total_in_db': total_in_db,
            'high_confidence': high_confidence,
            'emerged': emerged_count,
            'profile_changed': changed_count,
            'needs_recheck': needs_recheck,
            'pending_urls': len(self.state['pending_urls']),
            'total_searches': self.state['total_searches'],
            'total_scrapes': self.state['total_scrapes'],
            'total_founders_found': self.state['total_founders_found'],
            'skipped_non_german': self.state.get('skipped_non_german', 0),
            'skipped_low_confidence': self.state.get('skipped_low_confidence', 0),
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
