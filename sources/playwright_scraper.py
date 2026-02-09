"""
Playwright-based scraper with stealth mode for LinkedIn profile discovery.

Uses headless Chrome with anti-detection measures to bypass bot blocking.
"""

import re
import time
import random
import logging
import json
from typing import List, Optional, Dict, Any, Set
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qs, unquote

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """A search result with LinkedIn URL."""
    url: str
    title: str
    snippet: str
    query: str
    found_at: datetime = field(default_factory=datetime.now)


@dataclass
class LinkedInProfile:
    """Extracted LinkedIn profile data."""
    url: str
    name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None

    stealth_signals: List[str] = field(default_factory=list)
    high_value_background: List[str] = field(default_factory=list)
    founder_signals: List[str] = field(default_factory=list)
    confidence_score: float = 0.0

    scraped_at: datetime = field(default_factory=datetime.now)


# Stealth detection keywords
STEALTH_KEYWORDS = [
    'stealth', 'stealth mode', 'building something',
    'something new', 'exciting news soon', 'coming soon',
    'new venture', 'working on', 'exploring opportunities',
    'next chapter', 'what\'s next', 'in transition',
]

HIGH_VALUE_COMPANIES = [
    'google', 'meta', 'facebook', 'amazon', 'apple', 'microsoft',
    'stripe', 'airbnb', 'uber', 'spotify', 'netflix',
    'klarna', 'n26', 'revolut', 'wise',
    'delivery hero', 'zalando', 'celonis', 'personio',
]

FOUNDER_KEYWORDS = [
    'founder', 'co-founder', 'cofounder', 'gründer',
    'ceo', 'chief executive', 'entrepreneur',
]


class PlaywrightStealthScraper:
    """
    Scraper using Playwright with stealth mode to avoid bot detection.
    """

    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 100,
        delay_range: tuple = (3, 8),
    ):
        self.headless = headless
        self.slow_mo = slow_mo
        self.delay_range = delay_range
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.found_urls: Set[str] = set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def start(self):
        """Start the browser."""
        self.playwright = sync_playwright().start()

        # Launch with realistic settings
        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )

        # Create context with realistic viewport and user agent
        self.context = self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='Europe/Berlin',
        )

        self.page = self.context.new_page()

        # Apply stealth mode
        stealth = Stealth(
            navigator_webdriver=True,
            navigator_plugins=True,
            navigator_languages=True,
            webgl_vendor=True,
        )
        stealth.apply_stealth_sync(self.page)

        logger.info("Playwright browser started with stealth mode")

    def stop(self):
        """Stop the browser."""
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Playwright browser stopped")

    def _delay(self):
        """Random delay between actions."""
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def _random_mouse_move(self):
        """Simulate random mouse movement."""
        if self.page:
            x = random.randint(100, 800)
            y = random.randint(100, 600)
            self.page.mouse.move(x, y)

    def search_duckduckgo(self, query: str) -> List[SearchResult]:
        """
        Search DuckDuckGo for LinkedIn profiles.
        """
        results = []
        url = f"https://duckduckgo.com/?q={quote_plus(query)}"

        logger.info(f"DDG Search: {query[:50]}...")

        try:
            self.page.goto(url, wait_until='networkidle', timeout=30000)
            self._random_mouse_move()
            time.sleep(2)  # Wait for JS to load results

            # Wait for results to appear
            self.page.wait_for_selector('[data-testid="result"]', timeout=10000)

            # Extract results
            result_elements = self.page.query_selector_all('[data-testid="result"]')

            for elem in result_elements:
                try:
                    # Get the link
                    link_elem = elem.query_selector('a[data-testid="result-title-a"]')
                    if not link_elem:
                        continue

                    href = link_elem.get_attribute('href')
                    if not href or 'linkedin.com/in/' not in href.lower():
                        continue

                    # Clean the URL
                    clean_url = self._clean_linkedin_url(href)
                    if not clean_url or clean_url in self.found_urls:
                        continue

                    self.found_urls.add(clean_url)

                    title = link_elem.inner_text()

                    # Get snippet
                    snippet = ''
                    snippet_elem = elem.query_selector('[data-testid="result-snippet"]')
                    if snippet_elem:
                        snippet = snippet_elem.inner_text()

                    results.append(SearchResult(
                        url=clean_url,
                        title=title,
                        snippet=snippet,
                        query=query,
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing result: {e}")
                    continue

            logger.info(f"  Found {len(results)} LinkedIn profiles")

        except Exception as e:
            logger.error(f"DuckDuckGo search failed: {e}")

        return results

    def search_google(self, query: str) -> List[SearchResult]:
        """
        Search Google for LinkedIn profiles.
        """
        results = []
        url = f"https://www.google.com/search?q={quote_plus(query)}"

        logger.info(f"Google Search: {query[:50]}...")

        try:
            self.page.goto(url, wait_until='networkidle', timeout=30000)
            self._random_mouse_move()
            time.sleep(2)

            # Handle cookie consent if present
            try:
                accept_btn = self.page.query_selector('button:has-text("Accept all")')
                if accept_btn:
                    accept_btn.click()
                    time.sleep(1)
            except:
                pass

            # Wait for results
            self.page.wait_for_selector('div.g', timeout=10000)

            # Extract results
            result_elements = self.page.query_selector_all('div.g')

            for elem in result_elements:
                try:
                    link_elem = elem.query_selector('a')
                    if not link_elem:
                        continue

                    href = link_elem.get_attribute('href')
                    if not href or 'linkedin.com/in/' not in href.lower():
                        continue

                    clean_url = self._clean_linkedin_url(href)
                    if not clean_url or clean_url in self.found_urls:
                        continue

                    self.found_urls.add(clean_url)

                    title_elem = elem.query_selector('h3')
                    title = title_elem.inner_text() if title_elem else ''

                    snippet = ''
                    snippet_elem = elem.query_selector('div[data-sncf]')
                    if snippet_elem:
                        snippet = snippet_elem.inner_text()

                    results.append(SearchResult(
                        url=clean_url,
                        title=title,
                        snippet=snippet,
                        query=query,
                    ))

                except Exception as e:
                    logger.debug(f"Error parsing Google result: {e}")
                    continue

            logger.info(f"  Found {len(results)} LinkedIn profiles")

        except Exception as e:
            logger.error(f"Google search failed: {e}")

        return results

    def scrape_linkedin_profile(self, url: str) -> Optional[LinkedInProfile]:
        """
        Scrape a LinkedIn profile page.
        """
        logger.info(f"Scraping LinkedIn: {url}")

        profile = LinkedInProfile(url=url)

        try:
            self.page.goto(url, wait_until='networkidle', timeout=30000)
            self._random_mouse_move()
            time.sleep(2)

            # Check for auth wall
            if 'authwall' in self.page.url or 'login' in self.page.url:
                logger.debug("Hit auth wall, extracting available data...")

            # Try to extract name
            name_selectors = [
                'h1.top-card-layout__title',
                'h1.text-heading-xlarge',
                '.top-card__title',
                'h1',
            ]
            for selector in name_selectors:
                try:
                    elem = self.page.query_selector(selector)
                    if elem:
                        text = elem.inner_text().strip()
                        if text and len(text) < 100 and 'linkedin' not in text.lower():
                            profile.name = text
                            break
                except:
                    continue

            # Try to extract headline
            headline_selectors = [
                '.top-card-layout__headline',
                'h2.top-card-layout__headline',
                '.text-body-medium',
            ]
            for selector in headline_selectors:
                try:
                    elem = self.page.query_selector(selector)
                    if elem:
                        text = elem.inner_text().strip()
                        if text and len(text) > 5:
                            profile.headline = text
                            break
                except:
                    continue

            # Try to extract location
            location_selectors = [
                '.top-card-layout__first-subline',
                '.top-card__subline-item',
            ]
            for selector in location_selectors:
                try:
                    elem = self.page.query_selector(selector)
                    if elem:
                        text = elem.inner_text().strip()
                        if any(x in text.lower() for x in ['germany', 'berlin', 'munich', 'frankfurt']):
                            profile.location = text
                            break
                except:
                    continue

            # Try meta tags as fallback
            try:
                og_title = self.page.query_selector('meta[property="og:title"]')
                if og_title:
                    content = og_title.get_attribute('content')
                    if content and ' - ' in content:
                        parts = content.split(' - ', 1)
                        profile.name = profile.name or parts[0].strip()
                        if len(parts) > 1:
                            profile.headline = profile.headline or parts[1].strip()

                og_desc = self.page.query_selector('meta[property="og:description"]')
                if og_desc:
                    profile.summary = og_desc.get_attribute('content')
            except:
                pass

            # Detect signals
            profile = self._detect_signals(profile)

            logger.info(f"  -> {profile.name or 'Unknown'} (conf={profile.confidence_score:.2f})")

            return profile

        except Exception as e:
            logger.error(f"Failed to scrape {url}: {e}")
            return None

    def _clean_linkedin_url(self, url: str) -> Optional[str]:
        """Clean and normalize LinkedIn URL."""
        # Handle various URL formats
        if 'uddg=' in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            if 'uddg' in params:
                url = unquote(params['uddg'][0])

        match = re.search(r'(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#]+)', url)
        if match:
            cleaned = match.group(1)
            # Normalize to www.linkedin.com
            cleaned = re.sub(r'https?://[a-z]{2}\.linkedin\.com', 'https://www.linkedin.com', cleaned)
            return cleaned

        return None

    def _detect_signals(self, profile: LinkedInProfile) -> LinkedInProfile:
        """Detect stealth signals in profile."""
        text = ' '.join(filter(None, [
            profile.headline,
            profile.summary,
            profile.name,
        ])).lower()

        for keyword in STEALTH_KEYWORDS:
            if keyword in text:
                profile.stealth_signals.append(keyword)

        for company in HIGH_VALUE_COMPANIES:
            if company in text:
                profile.high_value_background.append(company)

        for keyword in FOUNDER_KEYWORDS:
            if keyword in text:
                profile.founder_signals.append(keyword)

        # Calculate confidence
        score = 0.0
        if profile.stealth_signals:
            score += min(0.4, len(profile.stealth_signals) * 0.15)
        if profile.founder_signals:
            score += min(0.3, len(profile.founder_signals) * 0.1)
        if profile.high_value_background:
            score += min(0.2, len(profile.high_value_background) * 0.1)
        if profile.location and any(x in profile.location.lower() for x in ['germany', 'berlin', 'munich']):
            score += 0.1

        profile.confidence_score = min(1.0, score)

        return profile

    def find_stealth_founders(
        self,
        queries: Optional[List[str]] = None,
        use_google: bool = False,
        max_queries: int = 5,
    ) -> List[SearchResult]:
        """
        Run multiple search queries to find stealth founder profiles.
        """
        if queries is None:
            queries = [
                'site:linkedin.com/in "stealth" "founder" "germany"',
                'site:linkedin.com/in "stealth mode" "berlin"',
                'site:linkedin.com/in "building something new" "founder" "germany"',
                'site:linkedin.com/in "ex-google" "founder" "berlin"',
                'site:linkedin.com/in "ex-stripe" "founder" "germany"',
                'site:linkedin.com/in "serial entrepreneur" "berlin"',
                'site:linkedin.com/in "stealth" "co-founder" "munich"',
            ]

        queries = queries[:max_queries]
        all_results = []

        search_fn = self.search_google if use_google else self.search_duckduckgo

        for i, query in enumerate(queries):
            results = search_fn(query)
            all_results.extend(results)

            if i < len(queries) - 1:
                self._delay()

        logger.info(f"Total unique profiles found: {len(self.found_urls)}")

        return all_results


def run_stealth_discovery(
    max_queries: int = 5,
    max_profiles: int = 20,
    use_google: bool = True,
    headless: bool = True,
) -> Dict[str, Any]:
    """
    Run stealth founder discovery using Playwright.

    Args:
        max_queries: Number of search queries to run
        max_profiles: Maximum profiles to scrape
        use_google: Use Google (True) or DuckDuckGo (False)
        headless: Run browser in headless mode

    Returns:
        Dictionary with discovery statistics and found profiles
    """
    stats = {
        'queries_run': 0,
        'urls_found': 0,
        'profiles_scraped': 0,
        'founders_found': 0,
        'high_confidence': 0,
    }

    founders = []

    with PlaywrightStealthScraper(headless=headless, delay_range=(5, 10)) as scraper:
        # Phase 1: Search for LinkedIn URLs
        logger.info("Phase 1: Searching for stealth founder profiles...")

        results = scraper.find_stealth_founders(
            use_google=use_google,
            max_queries=max_queries,
        )

        stats['queries_run'] = max_queries
        stats['urls_found'] = len(scraper.found_urls)

        # Phase 2: Scrape profiles
        logger.info(f"\nPhase 2: Scraping {min(len(results), max_profiles)} profiles...")

        for i, result in enumerate(results[:max_profiles]):
            scraper._delay()

            profile = scraper.scrape_linkedin_profile(result.url)
            stats['profiles_scraped'] += 1

            if profile and profile.confidence_score > 0:
                founders.append({
                    'url': profile.url,
                    'name': profile.name,
                    'headline': profile.headline,
                    'location': profile.location,
                    'signals': profile.stealth_signals,
                    'background': profile.high_value_background,
                    'confidence': profile.confidence_score,
                })
                stats['founders_found'] += 1

                if profile.confidence_score >= 0.5:
                    stats['high_confidence'] += 1

    return {
        'stats': stats,
        'founders': sorted(founders, key=lambda x: x['confidence'], reverse=True),
    }


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S',
    )

    print("=" * 60)
    print("STEALTH FOUNDER DISCOVERY (Playwright)")
    print("=" * 60)

    result = run_stealth_discovery(
        max_queries=2,
        max_profiles=5,
        use_google=True,
        headless=True,
    )

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)

    for k, v in result['stats'].items():
        print(f"  {k}: {v}")

    print("\nTOP FOUNDERS:")
    for f in result['founders'][:10]:
        print(f"\n  {f['name']} (conf={f['confidence']:.2f})")
        print(f"    {f['headline']}")
        print(f"    {f['url']}")
        print(f"    Signals: {f['signals']}")
