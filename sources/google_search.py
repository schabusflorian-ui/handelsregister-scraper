"""
Search scraper for finding LinkedIn profiles.

Uses Google or DuckDuckGo to find LinkedIn profiles matching specific patterns.
Handles rate limiting and anti-bot detection.
"""

import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qs, quote_plus, urlparse

import cloudscraper
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# Rotate user agents to avoid detection
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
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

        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

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
            if "captcha" in response.text.lower() or "unusual traffic" in response.text.lower():
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
        soup = BeautifulSoup(html, "html.parser")

        # Google's result divs (may change, need to adapt)
        for div in soup.find_all("div", class_="g"):
            try:
                # Find the link
                link = div.find("a", href=True)
                if not link:
                    continue

                url = link["href"]

                # Only interested in LinkedIn profile URLs
                if "linkedin.com/in/" not in url:
                    continue

                # Clean up the URL
                url = self._clean_linkedin_url(url)
                if not url:
                    continue

                # Get title
                title_elem = div.find("h3")
                title = title_elem.get_text() if title_elem else ""

                # Get snippet
                snippet_elem = div.find("div", {"data-sncf": True}) or div.find("span", class_="aCOpRe")
                snippet = snippet_elem.get_text() if snippet_elem else ""

                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        query=query,
                    )
                )

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
        if url.startswith("/url?"):
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "q" in params:
                url = params["q"][0]

        # Extract the base LinkedIn profile URL
        match = re.search(r"(https?://(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
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
            logger.info(f"Query {i + 1}/{len(STEALTH_QUERIES)}")

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
            logger.info(f"Query {i + 1}/{len(queries)}")

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
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
        else:
            self.session = requests.Session()
        self.found_urls: Set[str] = set()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
        }

    def _delay(self):
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def _search_ddg(self, query: str, retries: int = 2, page_data: dict = None) -> tuple:
        """
        Execute DuckDuckGo search.

        Args:
            query: Search query
            retries: Number of retries on failure
            page_data: Form data for pagination (None for first page)

        Returns:
            Tuple of (html_content, next_page_data) where next_page_data is None if no more pages
        """
        if page_data:
            # Pagination request - POST with form data
            url = "https://html.duckduckgo.com/html/"
            method = "POST"
            request_kwargs = {"data": page_data}
        else:
            # First page - GET request
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            method = "GET"
            request_kwargs = {}

        for attempt in range(retries + 1):
            try:
                if method == "POST":
                    response = self.session.post(url, headers=self._get_headers(), timeout=30, **request_kwargs)
                else:
                    response = self.session.get(
                        url,
                        headers=self._get_headers(),
                        timeout=30,
                    )

                if response.status_code == 200:
                    # Extract next page form data if available
                    next_data = self._extract_next_page_data(response.text)
                    return response.text, next_data

                if response.status_code == 202:
                    # Rate limited - wait longer with exponential backoff
                    wait_time = 30 * (2**attempt)  # 30s, 60s, 120s, 240s
                    logger.warning(f"DDG rate limit (202), waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                logger.warning(f"DuckDuckGo returned {response.status_code}")
                return None, None

            except requests.RequestException as e:
                logger.error(f"DuckDuckGo request failed: {e}")
                if attempt < retries:
                    time.sleep(5)
                    continue
                return None, None

        return None, None

    def _extract_next_page_data(self, html: str) -> Optional[dict]:
        """Extract form data needed to fetch the next page of results."""
        soup = BeautifulSoup(html, "html.parser")

        # Find the "Next" button form
        next_form = soup.find("input", {"value": "Next"})
        if not next_form:
            return None

        # Get the parent form
        form = next_form.find_parent("form")
        if not form:
            return None

        # Extract all hidden inputs
        form_data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            value = inp.get("value", "")
            if name:
                form_data[name] = value

        return form_data if form_data else None

    def _parse_results(self, html: str, query: str) -> List[SearchResult]:
        """Parse DuckDuckGo search results."""
        from urllib.parse import unquote

        results = []
        soup = BeautifulSoup(html, "html.parser")

        # DuckDuckGo result links
        for result in soup.find_all("a", class_="result__a"):
            try:
                href = result.get("href", "")

                # Decode URL-encoded href to check for linkedin
                decoded_href = unquote(href)

                # Only LinkedIn profile URLs (including country subdomains like de.linkedin.com)
                if "linkedin.com/in/" not in decoded_href:
                    continue

                # Extract clean URL
                url = self._clean_linkedin_url(href)
                if not url:
                    continue

                title = result.get_text(strip=True)

                # Get snippet from sibling
                snippet = ""
                snippet_elem = result.find_next("a", class_="result__snippet")
                if snippet_elem:
                    snippet = snippet_elem.get_text(strip=True)

                results.append(
                    SearchResult(
                        url=url,
                        title=title,
                        snippet=snippet,
                        query=query,
                    )
                )

            except Exception as e:
                logger.debug(f"Error parsing DDG result: {e}")
                continue

        return results

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL from DuckDuckGo redirect."""
        from urllib.parse import unquote

        # DDG uses uddg parameter for actual URL
        if "uddg=" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                url = unquote(params["uddg"][0])

        # Match LinkedIn URLs including country subdomains (de.linkedin.com, uk.linkedin.com, etc.)
        match = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
        if match:
            # Normalize to www.linkedin.com
            cleaned = match.group(1)
            cleaned = re.sub(r"https?://[a-z]{2}\.linkedin\.com", "https://www.linkedin.com", cleaned)
            return cleaned

        return None

    def search_query(self, query: str, max_pages: int = 3) -> List[SearchResult]:
        """
        Execute a search query with pagination.

        Args:
            query: Search query string
            max_pages: Maximum pages to fetch (default 3, ~30 results per page)

        Returns:
            List of unique SearchResult objects
        """
        logger.info(f"DDG Search: {query[:60]}...")

        all_results = []
        page = 1
        next_page_data = None

        while page <= max_pages:
            html, next_page_data = self._search_ddg(query, page_data=next_page_data)

            if not html:
                break

            results = self._parse_results(html, query)

            # Deduplicate
            new_results = []
            for r in results:
                if r.url not in self.found_urls:
                    self.found_urls.add(r.url)
                    new_results.append(r)

            all_results.extend(new_results)

            if page == 1:
                logger.info(f"  Page {page}: {len(new_results)} new URLs")
            else:
                logger.info(f"  Page {page}: +{len(new_results)} new URLs (total: {len(all_results)})")

            # Stop if no more pages or no new results
            if not next_page_data or len(new_results) == 0:
                break

            page += 1

            # Delay between pages
            if page <= max_pages:
                self._delay()

        logger.info(f"  Found {len(all_results)} total LinkedIn URLs")
        return all_results

    def search_stealth_queries(self, queries: Optional[List[str]] = None) -> List[SearchResult]:
        """Run stealth founder queries."""
        if queries is None:
            # Convert site: queries to regular queries for DDG
            queries = [
                "linkedin.com/in stealth founder germany",
                "linkedin.com/in stealth mode berlin",
                'linkedin.com/in "building something new" founder germany',
                'linkedin.com/in "ex-google" founder berlin',
                'linkedin.com/in "ex-meta" founder germany',
                'linkedin.com/in "serial entrepreneur" berlin',
                "linkedin.com/in stealth co-founder munich",
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
            "linkedin.com/in stealth founder germany",
            "linkedin.com/in stealth mode berlin",
            'linkedin.com/in "building something new" founder germany',
            'linkedin.com/in "ex-google" founder berlin',
            'linkedin.com/in "ex-stripe" founder germany',
            'linkedin.com/in "serial entrepreneur" berlin',
            "linkedin.com/in stealth co-founder munich",
        ]
        queries = ddg_queries[:max_queries] if max_queries else ddg_queries
        return scraper.search_stealth_queries(queries)

    # Fallback to Google (likely to be blocked)
    scraper = GoogleSearchScraper(delay_range=delay_range)
    queries = STEALTH_QUERIES[:max_queries] if max_queries else STEALTH_QUERIES

    all_results = []
    for i, query in enumerate(queries):
        logger.info(f"Query {i + 1}/{len(queries)}: {query[:50]}...")

        results = scraper.search_query(query)
        all_results.extend(results)

        if i < len(queries) - 1:
            scraper._delay()

    logger.info(f"Found {len(scraper.found_urls)} unique LinkedIn profiles")

    return all_results


class CurlCffiSearchScraper:
    """
    DuckDuckGo search using curl_cffi for TLS fingerprint impersonation.

    Much more reliable than cloudscraper or Playwright because DDG primarily
    detects bots via TLS fingerprint, not JavaScript. curl_cffi impersonates
    Chrome's TLS handshake at the libcurl level.

    Lightweight (~5MB) - no browser download needed. Cloud-friendly.
    """

    def __init__(self, delay_range: tuple = (3, 8), impersonate: str = "chrome"):
        self.delay_range = delay_range
        self.impersonate = impersonate
        self.found_urls: Set[str] = set()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
            "Referer": "https://duckduckgo.com/",
            "DNT": "1",
        }

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    def _search_ddg(self, query: str, retries: int = 1, page_data: dict = None) -> tuple:
        """Execute DuckDuckGo search via curl_cffi.

        On 202 rate limit: return immediately instead of retrying.
        Retrying burns time and makes rate limiting worse. Better to let
        the outer loop handle backoff and move to the next query.
        """
        from curl_cffi import requests as curl_requests

        headers = self._get_headers()

        for attempt in range(retries + 1):
            try:
                if page_data:
                    response = curl_requests.post(
                        "https://html.duckduckgo.com/html/",
                        data=page_data,
                        headers=headers,
                        impersonate=self.impersonate,
                        timeout=30,
                    )
                else:
                    response = curl_requests.get(
                        f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                        headers=headers,
                        impersonate=self.impersonate,
                        timeout=30,
                    )

                if response.status_code == 200:
                    next_data = self._extract_next_page_data(response.text)
                    return response.text, next_data

                if response.status_code == 202:
                    # Don't retry — return immediately and let the outer loop
                    # handle backoff. Retrying just digs the rate limit deeper.
                    logger.warning("DDG rate limit (202) — skipping to next query")
                    return None, None

                logger.warning(f"DuckDuckGo returned {response.status_code}")
                return None, None

            except Exception as e:
                logger.error(f"curl_cffi request failed: {e}")
                if attempt < retries:
                    time.sleep(5)
                    continue
                return None, None

        return None, None

    def _extract_next_page_data(self, html: str) -> Optional[dict]:
        """Extract form data needed to fetch the next page of results."""
        soup = BeautifulSoup(html, "html.parser")
        next_form = soup.find("input", {"value": "Next"})
        if not next_form:
            return None
        form = next_form.find_parent("form")
        if not form:
            return None
        form_data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            value = inp.get("value", "")
            if name:
                form_data[name] = value
        return form_data if form_data else None

    def _parse_results(self, html: str, query: str) -> List[SearchResult]:
        """Parse DuckDuckGo search results."""
        from urllib.parse import unquote

        results = []
        soup = BeautifulSoup(html, "html.parser")

        for result in soup.find_all("a", class_="result__a"):
            try:
                href = result.get("href", "")
                decoded_href = unquote(href)

                if "linkedin.com/in/" not in decoded_href:
                    continue

                url = self._clean_linkedin_url(href)
                if not url:
                    continue

                title = result.get_text(strip=True)

                snippet = ""
                snippet_elem = result.find_next("a", class_="result__snippet")
                if snippet_elem:
                    snippet = snippet_elem.get_text(strip=True)

                results.append(SearchResult(url=url, title=title, snippet=snippet, query=query))
            except Exception as e:
                logger.debug(f"Error parsing DDG result: {e}")
                continue

        return results

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL from DuckDuckGo redirect."""
        from urllib.parse import unquote

        if "uddg=" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if "uddg" in params:
                url = unquote(params["uddg"][0])

        match = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
        if match:
            cleaned = match.group(1)
            cleaned = re.sub(r"https?://[a-z]{2}\.linkedin\.com", "https://www.linkedin.com", cleaned)
            return cleaned
        return None

    def search_query(self, query: str, max_pages: int = 3) -> List[SearchResult]:
        """Execute a search query with pagination."""
        logger.info(f"DDG (curl_cffi) Search: {query[:60]}...")

        all_results = []
        page = 1
        next_page_data = None

        while page <= max_pages:
            html, next_page_data = self._search_ddg(query, page_data=next_page_data)
            if not html:
                break

            results = self._parse_results(html, query)

            new_results = []
            for r in results:
                if r.url not in self.found_urls:
                    self.found_urls.add(r.url)
                    new_results.append(r)

            all_results.extend(new_results)

            if page == 1:
                logger.info(f"  Page {page}: {len(new_results)} new URLs")
            else:
                logger.info(f"  Page {page}: +{len(new_results)} new URLs (total: {len(all_results)})")

            if not next_page_data or len(new_results) == 0:
                break

            page += 1
            if page <= max_pages:
                self._delay()

        logger.info(f"  Found {len(all_results)} total LinkedIn URLs")
        return all_results


class BraveSearchScraper:
    """
    Brave Search scraper - good alternative with less aggressive blocking.

    Uses HTML scraping (no API key needed for basic use).
    """

    def __init__(self, delay_range: tuple = (3, 8), use_cloudscraper: bool = True):
        self.delay_range = delay_range
        if use_cloudscraper:
            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "darwin", "mobile": False}
            )
        else:
            self.session = requests.Session()
        self.found_urls: Set[str] = set()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    def _search_brave(self, query: str, retries: int = 2) -> Optional[str]:
        """Execute Brave search."""
        url = f"https://search.brave.com/search?q={quote_plus(query)}&source=web"

        for attempt in range(retries + 1):
            try:
                response = self.session.get(url, headers=self._get_headers(), timeout=30)

                if response.status_code == 200:
                    return response.text

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 20
                    logger.warning(f"Brave rate limit (429), waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue

                logger.warning(f"Brave returned {response.status_code}")
                return None

            except requests.RequestException as e:
                logger.error(f"Brave request failed: {e}")
                if attempt < retries:
                    time.sleep(5)
                    continue
                return None

        return None

    def _parse_results(self, html: str, query: str) -> List[SearchResult]:
        """Parse Brave search results."""
        results = []
        soup = BeautifulSoup(html, "html.parser")
        seen_urls = set()

        # Find all links containing linkedin.com/in
        for link in soup.find_all("a", href=lambda h: h and "linkedin.com/in/" in h):
            try:
                href = link.get("href", "")

                # Clean URL
                url = self._clean_linkedin_url(href)
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                # Get title from link text or parent
                title = link.get_text(strip=True)

                # Clean up title - remove path cruft like "› in  › username"
                if title.startswith("›") or "linkedin.com" in title.lower():
                    # Extract username from URL and try to find better title
                    username = url.split("/in/")[-1].rstrip("/").replace("-", " ").replace("%20", " ")
                    # Try to find a better title in parent elements
                    parent = link.find_parent(["div", "article"])
                    if parent:
                        # Look for heading or title element with actual name
                        for elem in parent.find_all(["h2", "h3", "h4", "a", "span"]):
                            text = elem.get_text(strip=True)
                            # Skip if it's just path or LinkedIn text
                            if (
                                text
                                and len(text) > 5
                                and not text.startswith("›")
                                and "linkedin.com" not in text.lower()
                            ):
                                title = text
                                break
                    # Fallback: use cleaned username
                    if title.startswith("›"):
                        title = username.title()

                # Get snippet from nearby elements
                snippet = ""
                parent = link.find_parent(["div", "article"])
                if parent:
                    # Look for description/snippet text
                    for desc in parent.find_all(["p", "span", "div"]):
                        text = desc.get_text(strip=True)
                        if len(text) > 50 and "linkedin.com" not in text.lower():
                            snippet = text[:500]
                            break

                # Only add if we have a reasonable title
                if title and len(title) > 3:
                    results.append(SearchResult(url=url, title=title, snippet=snippet, query=query))

            except Exception as e:
                logger.debug(f"Error parsing Brave result: {e}")
                continue

        return results

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL."""
        match = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
        if match:
            cleaned = match.group(1)
            cleaned = re.sub(r"https?://[a-z]{2}\.linkedin\.com", "https://www.linkedin.com", cleaned)
            return cleaned
        return None

    def search_query(self, query: str) -> List[SearchResult]:
        """Execute a search query."""
        logger.info(f"Brave Search: {query[:60]}...")

        html = self._search_brave(query)
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


class DdgsLibraryScraper:
    """
    Search using the ddgs library (duckduckgo-search package).

    As of v8.x, the 'auto' backend routes through Bing HTML search,
    which is far more resilient to rate limiting than DDG's html endpoint.
    Supports site: operator, pagination, and has built-in error handling.

    Lightweight, free, no API key needed.
    """

    def __init__(
        self,
        delay_range: tuple = (1, 3),
        max_results_per_query: int = 30,
        proxy: Optional[str] = None,
    ):
        self.delay_range = delay_range
        self.max_results_per_query = max_results_per_query
        self.proxy = proxy
        self.found_urls: Set[str] = set()

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL - normalize country subdomains."""
        match = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
        if match:
            cleaned = match.group(1)
            cleaned = re.sub(
                r"https?://[a-z]{2}\.linkedin\.com",
                "https://www.linkedin.com",
                cleaned,
            )
            return cleaned
        return None

    def search_query(self, query: str, max_pages: int = 3, timelimit: Optional[str] = None) -> List[SearchResult]:
        """
        Execute a search query using the ddgs library.

        Args:
            query: Search query (supports site: operator)
            max_pages: Ignored — ddgs handles pagination via max_results
            timelimit: Time filter — 'd' (day), 'w' (week), 'm' (month), or None (all time)

        Returns:
            List of SearchResult with LinkedIn profile URLs
        """
        try:
            from ddgs import DDGS
            from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

            DuckDuckGoSearchException = DDGSException
        except ImportError:
            from duckduckgo_search import DDGS
            from duckduckgo_search.exceptions import (
                DuckDuckGoSearchException,
                RatelimitException,
                TimeoutException,
            )

        time_label = f" [timelimit={timelimit}]" if timelimit else ""
        logger.info(f"DDGs Library Search: {query[:60]}...{time_label}")

        raw_results = None
        for attempt in range(3):
            try:
                ddgs = DDGS(proxy=self.proxy, timeout=30)
                search_kwargs = dict(
                    query=query,
                    max_results=self.max_results_per_query,
                )
                if timelimit:
                    search_kwargs["timelimit"] = timelimit
                raw_results = ddgs.text(**search_kwargs)
                break
            except RatelimitException:
                logger.warning("ddgs rate limited — skipping to next query")
                return []
            except TimeoutException:
                logger.warning("ddgs timeout")
                return []
            except DuckDuckGoSearchException as e:
                logger.error(f"ddgs search error: {e}")
                return []
            except Exception as e:
                if attempt < 2:
                    logger.debug(f"ddgs attempt {attempt + 1} failed: {e}, retrying...")
                    time.sleep(2)
                    continue
                logger.error(f"ddgs failed after 3 attempts: {e}")
                return []

        if not raw_results:
            logger.info("  No results returned")
            return []

        results = []
        for item in raw_results:
            href = item.get("href", "")
            if "linkedin.com/in/" not in href:
                continue

            url = self._clean_linkedin_url(href)
            if not url or url in self.found_urls:
                continue

            self.found_urls.add(url)
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("body", ""),
                    query=query,
                )
            )

        logger.info(f"  Found {len(results)} new LinkedIn URLs")
        return results


class SerperSearchScraper:
    """
    Google search via Serper.dev API.

    2,500 free queries (no credit card needed), then $50/50k queries.
    Returns Google-quality results with structured JSON.
    Requires SERPER_API_KEY environment variable.
    """

    ENDPOINT = "https://google.serper.dev/search"

    def __init__(
        self,
        api_key: Optional[str] = None,
        delay_range: tuple = (0.5, 1.5),
        max_results_per_query: int = 30,
    ):
        self.api_key = api_key or os.environ.get("SERPER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Serper API key required. Set SERPER_API_KEY env var "
                "or pass api_key=. Get one free at https://serper.dev/"
            )
        self.delay_range = delay_range
        self.max_results_per_query = max_results_per_query
        self.found_urls: Set[str] = set()

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean LinkedIn URL."""
        match = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)", url)
        if match:
            cleaned = match.group(1)
            cleaned = re.sub(
                r"https?://[a-z]{2}\.linkedin\.com",
                "https://www.linkedin.com",
                cleaned,
            )
            return cleaned
        return None

    def search_query(self, query: str, max_pages: int = 1, tbs: Optional[str] = None) -> List[SearchResult]:
        """
        Execute search via Serper.dev API.

        Args:
            query: Search query (Google syntax, supports site: operator)
            max_pages: Ignored — Serper returns up to 100 results per request
            tbs: Google time filter — 'qdr:d' (day), 'qdr:w' (week), 'qdr:m' (month), or None (all time)

        Returns:
            List of SearchResult with LinkedIn profile URLs
        """
        time_label = f" [tbs={tbs}]" if tbs else ""
        logger.info(f"Serper Search: {query[:60]}...{time_label}")

        num = min(self.max_results_per_query, 100)

        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }

        payload = {
            "q": query,
            "num": num,
        }

        if tbs:
            payload["tbs"] = tbs

        try:
            response = requests.post(
                self.ENDPOINT,
                json=payload,
                headers=headers,
                timeout=30,
            )

            if response.status_code == 429:
                logger.warning("Serper rate limited (429)")
                return []

            if response.status_code == 403:
                logger.error("Serper API key invalid or quota exhausted")
                return []

            if response.status_code != 200:
                logger.warning(f"Serper returned {response.status_code}")
                return []

            data = response.json()

        except requests.RequestException as e:
            logger.error(f"Serper request failed: {e}")
            return []

        results = []
        for item in data.get("organic", []):
            href = item.get("link", "")
            if "linkedin.com/in/" not in href:
                continue

            url = self._clean_linkedin_url(href)
            if not url or url in self.found_urls:
                continue

            self.found_urls.add(url)
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("snippet", ""),
                    query=query,
                )
            )

        logger.info(f"  Found {len(results)} new LinkedIn URLs")
        return results


class MultiSearchScraper:
    """
    Combines multiple search engines with automatic fallback.

    Primary: ddgs library (Bing-backed, free, no API key)
    Fallback: Serper.dev (Google results, 2500 free queries)
    Last resort: CurlCffi DDG (raw HTTP, rate-limited)
    """

    def __init__(self, delay_range: tuple = (2, 5)):
        self.delay_range = delay_range
        self.found_urls: Set[str] = set()

        # Primary: ddgs library (Bing-backed, free)
        self.ddgs_lib = DdgsLibraryScraper(delay_range=delay_range)

        # Fallback: Serper (only if API key available)
        self.serper = None
        serper_key = os.environ.get("SERPER_API_KEY")
        if serper_key:
            self.serper = SerperSearchScraper(api_key=serper_key, delay_range=(0.5, 1.5))
            logger.info("Serper.dev fallback enabled")

        # Last resort: curl_cffi raw DDG
        self.ddg_curl = CurlCffiSearchScraper(delay_range=delay_range)

    def _deduplicate(self, results: List[SearchResult]) -> List[SearchResult]:
        """Deduplicate across all engines."""
        new_results = []
        for r in results:
            if r.url not in self.found_urls:
                self.found_urls.add(r.url)
                new_results.append(r)
        return new_results

    def search_query(self, query: str, max_pages: int = 2) -> List[SearchResult]:
        """Search with automatic fallback chain: ddgs → serper → curl_cffi."""

        # 1. Try ddgs library first (Bing-backed, most reliable)
        try:
            results = self.ddgs_lib.search_query(query, max_pages=max_pages)
            if results:
                return self._deduplicate(results)
        except Exception as e:
            logger.warning(f"ddgs library failed: {e}")

        # 2. Fallback to Serper if available
        if self.serper:
            try:
                results = self.serper.search_query(query)
                if results:
                    logger.info("  (via Serper fallback)")
                    return self._deduplicate(results)
            except Exception as e:
                logger.warning(f"Serper fallback failed: {e}")

        # 3. Last resort: curl_cffi raw DDG
        try:
            results = self.ddg_curl.search_query(query, max_pages=max_pages)
            if results:
                logger.info("  (via curl_cffi fallback)")
                return self._deduplicate(results)
        except Exception as e:
            logger.warning(f"curl_cffi fallback failed: {e}")

        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test with DuckDuckGo
    print("Testing DuckDuckGo search...")
    results = find_stealth_founders(max_queries=2, delay_range=(3, 6), use_duckduckgo=True)

    print(f"\nFound {len(results)} profiles:")
    for r in results[:10]:
        print(f"\n{r.title}")
        print(f"  URL: {r.url}")
        print(f"  Snippet: {r.snippet[:100]}..." if r.snippet else "  (no snippet)")
