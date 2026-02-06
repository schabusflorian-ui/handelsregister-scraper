"""
Search scraper for finding LinkedIn profiles.

Uses Google or DuckDuckGo to find LinkedIn profiles matching specific patterns.
Handles rate limiting and anti-bot detection.
"""

import re
import time
import random
import logging
import requests
import cloudscraper
from typing import List, Set, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qs
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# Rotate user agents to avoid detection
USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


# Stealth-related search queries for Germany
STEALTH_QUERIES = [
    # Direct stealth signals
    'site:linkedin.com/in "stealth" "founder" "germany"',
    'site:linkedin.com/in "stealth" "gründer" "deutschland"',
    'site:linkedin.com/in "stealth mode" "berlin"',
    'site:linkedin.com/in "stealth mode" "munich"',
    'site:linkedin.com/in "stealth" "co-founder" "germany"',

    # Building something new
    'site:linkedin.com/in "building something new" "founder" "germany"',
    'site:linkedin.com/in "building something new" "berlin"',
    'site:linkedin.com/in "working on something exciting" "germany"',
    'site:linkedin.com/in "working on something new" "berlin"',

    # Transition signals
    'site:linkedin.com/in "ex-" "founder" "berlin" "stealth"',
    'site:linkedin.com/in "former" "now building" "germany"',
    'site:linkedin.com/in "left" "to start" "berlin"',
    'site:linkedin.com/in "previously at" "founder" "berlin"',

    # Role-based signals
    'site:linkedin.com/in "founder & ceo" "stealth" "germany"',
    'site:linkedin.com/in "co-founder" "coming soon" "germany"',
    'site:linkedin.com/in "founder" "()" "berlin"',  # Empty company name

    # VC/Angel signals
    'site:linkedin.com/in "serial entrepreneur" "new venture" "germany"',
    'site:linkedin.com/in "angel investor" "building" "berlin"',
    'site:linkedin.com/in "entrepreneur in residence" "germany"',

    # Tech background + founder
    'site:linkedin.com/in "ex-google" "founder" "germany"',
    'site:linkedin.com/in "ex-meta" "founder" "berlin"',
    'site:linkedin.com/in "ex-amazon" "founder" "germany"',
    'site:linkedin.com/in "ex-stripe" "founder" "europe"',
    'site:linkedin.com/in "ex-n26" "founder"',
    'site:linkedin.com/in "ex-zalando" "founder"',
    'site:linkedin.com/in "ex-delivery hero" "founder"',
]


@dataclass
class SearchResult:
    """A single search result."""
    url: str
    title: str
    snippet: str
    query: str
    found_at: datetime = field(default_factory=datetime.now)


class GoogleSearchScraper:
    """
    Scrapes Google search results to find LinkedIn profile URLs.

    Uses careful rate limiting and rotating user agents to avoid blocks.
    """

    def __init__(
        self,
        delay_range: tuple = (10, 30),
        max_results_per_query: int = 50,
        proxy: Optional[str] = None,
    ):
        """
        Args:
            delay_range: Min/max seconds between requests (random)
            max_results_per_query: Maximum results to fetch per query
            proxy: Optional proxy URL (e.g., 'http://user:pass@host:port')
        """
        self.delay_range = delay_range
        self.max_results_per_query = max_results_per_query
        self.proxy = proxy
        self.session = requests.Session()
        self.found_urls: Set[str] = set()

    def _get_headers(self) -> Dict[str, str]:
        """Get randomized headers."""
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }

    def _delay(self):
        """Random delay between requests."""
        delay = random.uniform(*self.delay_range)
        logger.debug(f"Waiting {delay:.1f}s before next request")
        time.sleep(delay)

    def _search_google(self, query: str, start: int = 0) -> Optional[str]:
        """
        Execute a Google search and return the HTML.

        Args:
            query: Search query string
            start: Result offset for pagination

        Returns:
            HTML content or None if blocked/error
        """
        url = f"https://www.google.com/search?q={quote_plus(query)}&start={start}&num=10"

        proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None

        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                proxies=proxies,
                timeout=30,
            )

            if response.status_code == 429:
                logger.warning("Rate limited by Google (429). Need to wait longer.")
                return None

            if response.status_code != 200:
                logger.warning(f"Google returned status {response.status_code}")
                return None

            # Check for CAPTCHA
            if 'captcha' in response.text.lower() or 'unusual traffic' in response.text.lower():
                logger.warning("Google CAPTCHA detected. Need to wait or use proxy.")
                return None

            return response.text

        except requests.RequestException as e:
            logger.error(f"Request failed: {e}")
            return None

    def _parse_results(self, html: str, query: str) -> List[SearchResult]:
        """
        Parse Google search results HTML.

        Args:
            html: Raw HTML from Google
            query: Original search query

        Returns:
            List of SearchResult objects
        """
        results = []
        soup = BeautifulSoup(html, 'html.parser')

        # Google's result divs (may change, need to adapt)
        for div in soup.find_all('div', class_='g'):
            try:
                # Find the link
                link = div.find('a', href=True)
                if not link:
                    continue

                url = link['href']

                # Only interested in LinkedIn profile URLs
                if 'linkedin.com/in/' not in url:
                    continue

                # Clean up the URL
                url = self._clean_linkedin_url(url)
                if not url:
                    continue

                # Get title
                title_elem = div.find('h3')
                title = title_elem.get_text() if title_elem else ''

                # Get snippet
                snippet_elem = div.find('div', {'data-sncf': True}) or div.find('span', class_='aCOpRe')
                snippet = snippet_elem.get_text() if snippet_elem else ''

                results.append(SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    query=query,
                ))

            except Exception as e:
                logger.debug(f"Error parsing result: {e}")
                continue

        return results

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """
        Clean and normalize a LinkedIn URL.

        Args:
            url: Raw URL from search results

        Returns:
            Cleaned URL or None if invalid
        """
        # Handle Google redirect URLs
        if url.startswith('/url?'):
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'q' in params:
                url = params['q'][0]

        # Extract the base LinkedIn profile URL
        match = re.search(r'(https?://(?:www\.)?linkedin\.com/in/[^/?&#]+)', url)
        if match:
            return match.group(1)

        return None

    def search_query(self, query: str) -> List[SearchResult]:
        """
        Execute a single search query and return all results.

        Args:
            query: Search query string

        Returns:
            List of SearchResult objects
        """
        all_results = []
        start = 0

        logger.info(f"Searching: {query[:60]}...")

        while start < self.max_results_per_query:
            html = self._search_google(query, start)

            if not html:
                break

            results = self._parse_results(html, query)

            if not results:
                break  # No more results

            # Deduplicate
            new_results = []
            for r in results:
                if r.url not in self.found_urls:
                    self.found_urls.add(r.url)
                    new_results.append(r)

            all_results.extend(new_results)
            logger.info(f"  Found {len(new_results)} new URLs (page {start // 10 + 1})")

            start += 10

            if start < self.max_results_per_query:
                self._delay()

        return all_results

    def search_all_stealth_queries(self) -> List[SearchResult]:
        """
        Run all predefined stealth founder queries.

        Returns:
            Combined list of all unique results
        """
        all_results = []

        for i, query in enumerate(STEALTH_QUERIES):
            logger.info(f"Query {i+1}/{len(STEALTH_QUERIES)}")

            results = self.search_query(query)
            all_results.extend(results)

            logger.info(f"  Total unique URLs so far: {len(self.found_urls)}")

            if i < len(STEALTH_QUERIES) - 1:
                self._delay()

        return all_results

    def search_custom_queries(self, queries: List[str]) -> List[SearchResult]:
        """
        Run custom search queries.

        Args:
            queries: List of search query strings

        Returns:
            Combined list of all unique results
        """
        all_results = []

        for i, query in enumerate(queries):
            logger.info(f"Query {i+1}/{len(queries)}")

            results = self.search_query(query)
            all_results.extend(results)

            if i < len(queries) - 1:
                self._delay()

        return all_results


class DuckDuckGoSearchScraper:
    """
    DuckDuckGo search scraper - more permissive than Google.

    Uses DuckDuckGo HTML search which is less likely to block scrapers.
    Now uses cloudscraper for better bot detection bypass.
    """

    def __init__(
        self,
        delay_range: tuple = (3, 8),
        max_results_per_query: int = 30,
        use_cloudscraper: bool = True,
    ):
        self.delay_range = delay_range
        self.max_results_per_query = max_results_per_query
        # Use cloudscraper for better bot bypass
        if use_cloudscraper:
            self.session = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
            )
        else:
            self.session = requests.Session()
        self.found_urls: Set[str] = set()

    def _get_headers(self) -> Dict[str, str]:
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'DNT': '1',
            'Connection': 'keep-alive',
        }

    def _delay(self):
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def _search_ddg(self, query: str, retries: int = 2) -> Optional[str]:
        """Execute DuckDuckGo search."""
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        for attempt in range(retries + 1):
            try:
                response = self.session.get(
                    url,
                    headers=self._get_headers(),
                    timeout=30,
                )

                if response.status_code == 200:
                    return response.text

                if response.status_code == 202:
                    # Rate limited - wait and retry
                    wait_time = (attempt + 1) * 15
                    logger.warning(f"DDG rate limit (202), waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                logger.warning(f"DuckDuckGo returned {response.status_code}")
                return None

            except requests.RequestException as e:
                logger.error(f"DuckDuckGo request failed: {e}")
                if attempt < retries:
                    time.sleep(5)
                    continue
                return None

        return None

    def _parse_results(self, html: str, query: str) -> List[SearchResult]:
        """Parse DuckDuckGo search results."""
        from urllib.parse import unquote

        results = []
        soup = BeautifulSoup(html, 'html.parser')

        # DuckDuckGo result links
        for result in soup.find_all('a', class_='result__a'):
            try:
                href = result.get('href', '')

                # Decode URL-encoded href to check for linkedin
                decoded_href = unquote(href)

                # Only LinkedIn profile URLs (including country subdomains like de.linkedin.com)
                if 'linkedin.com/in/' not in decoded_href:
                    continue

                # Extract clean URL
                url = self._clean_linkedin_url(href)
                if not url:
                    continue

                title = result.get_text(strip=True)

                # Get snippet from sibling
                snippet = ''
                snippet_elem = result.find_next('a', class_='result__snippet')
                if snippet_elem:
                    snippet = snippet_elem.get_text(strip=True)

                results.append(SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    query=query,
                ))

            except Exception as e:
                logger.debug(f"Error parsing DDG result: {e}")
                continue

        return results

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL from DuckDuckGo redirect."""
        from urllib.parse import unquote

        # DDG uses uddg parameter for actual URL
        if 'uddg=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'uddg' in params:
                url = unquote(params['uddg'][0])

        # Match LinkedIn URLs including country subdomains (de.linkedin.com, uk.linkedin.com, etc.)
        match = re.search(r'(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)', url)
        if match:
            # Normalize to www.linkedin.com
            cleaned = match.group(1)
            cleaned = re.sub(r'https?://[a-z]{2}\.linkedin\.com', 'https://www.linkedin.com', cleaned)
            return cleaned

        return None

    def search_query(self, query: str) -> List[SearchResult]:
        """Execute a search query."""
        logger.info(f"DDG Search: {query[:60]}...")

        html = self._search_ddg(query)
        if not html:
            return []

        results = self._parse_results(html, query)

        # Deduplicate
        new_results = []
        for r in results:
            if r.url not in self.found_urls:
                self.found_urls.add(r.url)
                new_results.append(r)

        logger.info(f"  Found {len(new_results)} new LinkedIn URLs")

        return new_results

    def search_stealth_queries(self, queries: Optional[List[str]] = None) -> List[SearchResult]:
        """Run stealth founder queries."""
        if queries is None:
            # Convert site: queries to regular queries for DDG
            queries = [
                'linkedin.com/in stealth founder germany',
                'linkedin.com/in stealth mode berlin',
                'linkedin.com/in "building something new" founder germany',
                'linkedin.com/in "ex-google" founder berlin',
                'linkedin.com/in "ex-meta" founder germany',
                'linkedin.com/in "serial entrepreneur" berlin',
                'linkedin.com/in stealth co-founder munich',
                'linkedin.com/in "working on something exciting" germany',
            ]

        all_results = []
        for i, query in enumerate(queries):
            results = self.search_query(query)
            all_results.extend(results)

            if i < len(queries) - 1:
                self._delay()

        return all_results


def find_stealth_founders(
    max_queries: Optional[int] = None,
    delay_range: tuple = (15, 45),
    use_duckduckgo: bool = True,
) -> List[SearchResult]:
    """
    Convenience function to find stealth founder LinkedIn profiles.

    Args:
        max_queries: Limit number of queries (for testing)
        delay_range: Min/max delay between requests
        use_duckduckgo: Use DuckDuckGo instead of Google (recommended)

    Returns:
        List of SearchResult objects with LinkedIn URLs
    """
    if use_duckduckgo:
        scraper = DuckDuckGoSearchScraper(delay_range=delay_range)
        ddg_queries = [
            'linkedin.com/in stealth founder germany',
            'linkedin.com/in stealth mode berlin',
            'linkedin.com/in "building something new" founder germany',
            'linkedin.com/in "ex-google" founder berlin',
            'linkedin.com/in "ex-stripe" founder germany',
            'linkedin.com/in "serial entrepreneur" berlin',
            'linkedin.com/in stealth co-founder munich',
        ]
        queries = ddg_queries[:max_queries] if max_queries else ddg_queries
        return scraper.search_stealth_queries(queries)

    # Fallback to Google (likely to be blocked)
    scraper = GoogleSearchScraper(delay_range=delay_range)
    queries = STEALTH_QUERIES[:max_queries] if max_queries else STEALTH_QUERIES

    all_results = []
    for i, query in enumerate(queries):
        logger.info(f"Query {i+1}/{len(queries)}: {query[:50]}...")

        results = scraper.search_query(query)
        all_results.extend(results)

        if i < len(queries) - 1:
            scraper._delay()

    logger.info(f"Found {len(scraper.found_urls)} unique LinkedIn profiles")

    return all_results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # Test with DuckDuckGo
    print("Testing DuckDuckGo search...")
    results = find_stealth_founders(max_queries=2, delay_range=(3, 6), use_duckduckgo=True)

    print(f"\nFound {len(results)} profiles:")
    for r in results[:10]:
        print(f"\n{r.title}")
        print(f"  URL: {r.url}")
        print(f"  Snippet: {r.snippet[:100]}..." if r.snippet else "  (no snippet)")
