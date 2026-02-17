"""
Officer LinkedIn Enrichment Job.

Slowly enriches officers with LinkedIn data by searching for their profiles
via DuckDuckGo/Brave and extracting info from search snippets.

Uses the snippet-first approach: career data is extracted from search engine
result titles and snippets, never hitting linkedin.com directly (which blocks
cloud IPs aggressively).

Rate limiting: ~1 search per 2.5 minutes by default.
State persists to disk for crash recovery.
"""

import os
import json
import time
import random
import logging
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class OfficerLinkedInEnrichmentJob:
    """
    Enriches officers with LinkedIn profile data from search snippets.

    Designed for conservative rate limiting on Railway/cloud environments.
    """

    def __init__(
        self,
        db,
        state_file: str = 'data/officer_linkedin_state.json',
        search_delay: int = 150,       # 2.5 minutes between searches
        jitter: float = 0.3,           # +/- 30% random variance
        search_engine: str = 'curl',   # 'curl' or 'brave' or 'rotate'
        min_confidence: float = 0.40,
    ):
        self.db = db
        self.state_file = state_file
        self.search_delay = search_delay
        self.jitter = jitter
        self.search_engine_type = search_engine
        self.min_confidence = min_confidence
        self._search_engine = None
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        """Load persistent state from file."""
        default = {
            'total_searches': 0,
            'total_enriched': 0,
            'total_no_match': 0,
            'last_search_at': None,
            'failed_officer_ids': [],
        }
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    # Merge with defaults for forward compatibility
                    for k, v in default.items():
                        if k not in state:
                            state[k] = v
                    return state
        except Exception as e:
            logger.warning(f"Could not load state from {self.state_file}: {e}")
        return default

    def _save_state(self):
        """Save persistent state to file."""
        try:
            os.makedirs(os.path.dirname(self.state_file) or '.', exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    def _get_search_engine(self):
        """Lazily initialize search engine."""
        if self._search_engine is None:
            try:
                if self.search_engine_type == 'rotate':
                    from sources.google_search import MultiSearchScraper
                    self._search_engine = MultiSearchScraper(
                        ddg_delay=(3, 8),
                        brave_delay=(3, 8),
                    )
                elif self.search_engine_type == 'brave':
                    from sources.google_search import BraveSearchScraper
                    self._search_engine = BraveSearchScraper(delay_range=(3, 8))
                else:
                    from sources.google_search import CurlCffiSearchScraper
                    self._search_engine = CurlCffiSearchScraper(delay_range=(2, 5))
            except ImportError as e:
                logger.error(f"Search engine import failed: {e}")
                raise
        return self._search_engine

    def _get_delay(self) -> float:
        """Get delay with jitter."""
        jitter_amount = self.search_delay * self.jitter
        return self.search_delay + random.uniform(-jitter_amount, jitter_amount)

    def run_once(self) -> Dict[str, Any]:
        """
        Enrich a single officer. Returns stats dict.

        Returns:
            Dict with keys: action ('search' | 'idle' | 'rate_limited'), success, details
        """
        from processing.officer_linkedin_search import search_officer_linkedin, RateLimitedError

        stats = {'action': None, 'success': False, 'details': {}}

        # Get next officer to enrich
        officers = self.db.get_officers_for_linkedin_enrichment(limit=20)

        # Skip officers that previously failed
        failed_ids = set(self.state.get('failed_officer_ids', []))
        officers = [o for o in officers if o['id'] not in failed_ids]

        if not officers:
            stats['action'] = 'idle'
            stats['details'] = {'reason': 'no_officers_to_enrich'}
            return stats

        officer = officers[0]
        stats['action'] = 'search'

        logger.info(
            f"Enriching officer: {officer['name']} "
            f"(company: {officer.get('company_name', 'unknown')}, "
            f"AI score: {officer.get('ai_robotics_score', '?')})"
        )

        try:
            match = search_officer_linkedin(
                officer_name=officer['name'],
                company_name=officer.get('company_name', ''),
                company_city=officer.get('company_city'),
                min_confidence=self.min_confidence,
            )

            if match:
                # Store enrichment data
                self.db.update_officer_linkedin(
                    officer['id'],
                    linkedin_url=match.linkedin_url,
                    linkedin_headline=match.headline,
                    linkedin_location=match.location,
                    linkedin_previous_companies=(
                        json.dumps(match.previous_companies)
                        if match.previous_companies else None
                    ),
                    linkedin_snippet=match.snippet[:500] if match.snippet else None,
                    linkedin_match_confidence=match.match_confidence,
                    linkedin_enrichment_source=match.source,
                )
                self.state['total_enriched'] += 1
                stats['success'] = True
                stats['details'] = {
                    'officer': officer['name'],
                    'company': officer.get('company_name'),
                    'linkedin_url': match.linkedin_url,
                    'headline': match.headline,
                    'confidence': round(match.match_confidence, 2),
                    'previous_companies': match.previous_companies,
                }
                logger.info(
                    f"  -> Matched: {match.headline or 'no headline'} "
                    f"(confidence={match.match_confidence:.2f})"
                )
            else:
                # Mark as attempted (no match found)
                self.db.update_officer_linkedin(
                    officer['id'],
                    linkedin_match_confidence=0.0,
                    linkedin_enrichment_source='no_match',
                )
                self.state['total_no_match'] += 1
                stats['details'] = {
                    'officer': officer['name'],
                    'company': officer.get('company_name'),
                    'reason': 'no_confident_match',
                }
                logger.info(f"  -> No confident match found")

            self.state['total_searches'] += 1
            self.state['last_search_at'] = datetime.now().isoformat()
            self._save_state()

        except RateLimitedError as e:
            logger.warning(f"Rate limited: {e}")
            stats['action'] = 'rate_limited'
            stats['details'] = {'officer': officer['name'], 'error': 'rate_limited'}
            # Don't mark officer as failed — we want to retry when rate limit lifts
            self._save_state()

        except Exception as e:
            logger.error(f"Search failed for {officer['name']}: {e}")
            stats['details'] = {'officer': officer['name'], 'error': str(e)}
            # Record failure to skip this officer next time
            self.state.setdefault('failed_officer_ids', []).append(officer['id'])
            # Keep bounded
            if len(self.state['failed_officer_ids']) > 500:
                self.state['failed_officer_ids'] = self.state['failed_officer_ids'][-250:]
            self._save_state()

        return stats

    def run_batch(self, batch_size: int = 5) -> Dict[str, Any]:
        """
        Run a batch of enrichments with delays between each.

        Called by the scheduler. With default 2.5 min delay and batch_size=5,
        takes ~12.5 minutes total.
        """
        batch_stats = {
            'officers_processed': 0,
            'officers_enriched': 0,
            'officers_no_match': 0,
            'errors': 0,
        }

        consecutive_rate_limits = 0

        for i in range(batch_size):
            stats = self.run_once()

            if stats['action'] == 'idle':
                logger.info("No more officers to enrich")
                break

            if stats['action'] == 'rate_limited':
                consecutive_rate_limits += 1
                batch_stats['errors'] += 1
                if consecutive_rate_limits >= 2:
                    logger.warning(
                        "Rate limited %d times in a row — stopping batch. "
                        "Search engines are blocking requests.",
                        consecutive_rate_limits,
                    )
                    break
                # Exponential backoff: wait longer before retrying
                backoff = self.search_delay * (2 ** consecutive_rate_limits)
                logger.info(f"  Rate limited, backing off {backoff:.0f}s...")
                time.sleep(backoff)
                continue

            consecutive_rate_limits = 0  # Reset on successful search
            batch_stats['officers_processed'] += 1
            if stats['success']:
                batch_stats['officers_enriched'] += 1
            elif 'error' in stats.get('details', {}):
                batch_stats['errors'] += 1
            else:
                batch_stats['officers_no_match'] += 1

            # Delay between searches (not after the last one)
            if i < batch_size - 1:
                delay = self._get_delay()
                logger.info(f"  Sleeping {delay:.0f}s before next search...")
                time.sleep(delay)

        return batch_stats

    def run_continuous(self, max_iterations: Optional[int] = None):
        """
        Run continuously (for local execution).

        Args:
            max_iterations: Stop after N iterations (None = run forever)
        """
        iteration = 0
        logger.info("Starting officer LinkedIn enrichment (continuous mode)...")
        logger.info(f"  Search delay: {self.search_delay}s (+/- {self.jitter*100:.0f}% jitter)")
        logger.info(f"  Min confidence: {self.min_confidence}")

        consecutive_rate_limits = 0

        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1

                stats = self.run_once()

                if stats['action'] == 'idle':
                    logger.info("No more officers to enrich. Waiting 10 minutes...")
                    time.sleep(600)
                    consecutive_rate_limits = 0
                    continue

                if stats['action'] == 'rate_limited':
                    consecutive_rate_limits += 1
                    backoff = min(self.search_delay * (2 ** consecutive_rate_limits), 3600)
                    logger.warning(
                        f"Rate limited ({consecutive_rate_limits}x). "
                        f"Backing off {backoff:.0f}s..."
                    )
                    time.sleep(backoff)
                    continue

                consecutive_rate_limits = 0

                if iteration % 10 == 0:
                    s = self.get_stats()
                    logger.info(
                        f"[Progress] Searched: {s['total_searches']}, "
                        f"Enriched: {s['total_enriched']}, "
                        f"Remaining: {s.get('remaining', '?')}"
                    )

                delay = self._get_delay()
                logger.info(f"  Sleeping {delay:.0f}s...")
                time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Stopped by user")
            self._save_state()

    def get_stats(self) -> Dict[str, Any]:
        """Get enrichment statistics."""
        try:
            cursor = self.db.conn.cursor()

            cursor.execute(
                'SELECT COUNT(*) FROM officers WHERE linkedin_enriched_at IS NOT NULL'
            )
            total_attempted = cursor.fetchone()[0]

            cursor.execute(
                'SELECT COUNT(*) FROM officers WHERE linkedin_url IS NOT NULL'
            )
            with_linkedin = cursor.fetchone()[0]

            cursor.execute('''
                SELECT COUNT(*) FROM officers o
                JOIN companies c ON o.company_id = c.id
                WHERE o.linkedin_enriched_at IS NULL
                  AND o.is_current = 1
                  AND c.ai_robotics_score >= 1
            ''')
            remaining = cursor.fetchone()[0]
        except Exception:
            total_attempted = 0
            with_linkedin = 0
            remaining = 0

        return {
            'total_attempted': total_attempted,
            'with_linkedin_url': with_linkedin,
            'remaining': remaining,
            **{k: v for k, v in self.state.items() if k != 'failed_officer_ids'},
            'failed_count': len(self.state.get('failed_officer_ids', [])),
        }
