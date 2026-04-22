"""
Playwright-based DuckDuckGo search scraper.

Why this exists:
  The curl_cffi / requests-based DDG scrapers in google_search.py all hit
  html.duckduckgo.com or lite.duckduckgo.com. As of 2026-04 those endpoints
  return 202/403 for `site:linkedin.com/in` queries from every IP we've tried
  (home, DE VPN, AT VPN). The main JS-rendered endpoint
  duckduckgo.com/?q=... still serves real results — but only to browsers that
  pass fingerprint checks. Bundled headless Chromium without patches is
  redirected to /static-pages/418.html ("Unexpected error").

  Playwright + playwright-stealth bypasses that. Confirmed HTTP 200 with
  parseable results on 5/5 test queries; extracted 11 unique LinkedIn profile
  URLs across stealth / ex-google / serial entrepreneur variants.

Trade-offs vs the rest of sources/google_search.py:
  - Much heavier: spins up a real Chromium process, ~200MB RAM / ~2s per query.
  - Detectability: low — playwright-stealth patches webdriver, plugins, WebGL,
    navigator.languages. DDG serves normal results. No CAPTCHA seen in testing.
  - ToS risk: scraping DDG results violates their ToS. Same risk level as the
    existing curl_cffi scraper — just more reliable. No LinkedIn scraping here
    (URLs only, profile data comes from search snippets, matching the existing
    pattern in slow_stealth_scraper._store_from_search_result).
  - Deploy: works locally (user's IP). On Railway/Docker, headless Chromium
    works but the IP may be flagged — test before deploying.

Usage:
    from sources.playwright_ddg_scraper import PlaywrightDdgScraper
    with PlaywrightDdgScraper(delay_range=(2, 5)) as scraper:
        results = scraper.search_query('site:linkedin.com/in "stealth" "berlin"')

Or one-shot via the plain class (it opens/closes the browser per batch):
    scraper = PlaywrightDdgScraper()
    scraper.start()
    results = scraper.search_query(query)
    scraper.stop()
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Set
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Mirror of sources.google_search.SearchResult so callers can swap engines."""

    url: str
    title: str
    snippet: str
    query: str
    found_at: datetime = field(default_factory=datetime.now)


class PlaywrightDdgScraper:
    """DDG search via headless Chromium + playwright-stealth.

    Opens one browser context and reuses it across queries — much cheaper
    than launching per-query. Callers should use `with` or call stop().
    """

    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        delay_range: tuple = (3, 7),
        headless: bool = True,
        block_media: bool = True,
        warmup: bool = True,
    ):
        self.delay_range = delay_range
        self.headless = headless
        self.block_media = block_media
        self.warmup = warmup

        self._playwright = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._stealth_cm = None  # stealth context manager (exited on stop)
        self.found_urls: Set[str] = set()

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def start(self):
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth

        # Stealth.use_sync wraps sync_playwright(); we own the lifetime.
        stealth = Stealth()
        self._stealth_cm = stealth.use_sync(sync_playwright())
        self._playwright = self._stealth_cm.__enter__()

        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._ctx = self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=self.USER_AGENT,
            locale="en-US",
            timezone_id="Europe/Berlin",
        )

        if self.block_media:
            self._ctx.route("**/*", self._block_media_route)

        self._page = self._ctx.new_page()

        if self.warmup:
            try:
                self._page.goto("https://duckduckgo.com/", wait_until="load", timeout=30000)
                time.sleep(1.5)
            except Exception as e:
                logger.debug(f"DDG warmup failed: {e}")

        logger.info("PlaywrightDdgScraper started (stealth enabled, headless=%s)", self.headless)

    def stop(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._stealth_cm:
                self._stealth_cm.__exit__(None, None, None)
        except Exception:
            pass
        self._playwright = self._browser = self._ctx = self._page = None
        self._stealth_cm = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _block_media_route(route):
        if route.request.resource_type in {"image", "media", "font"}:
            return route.abort()
        return route.continue_()

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    @staticmethod
    def _clean_linkedin_url(url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        # DDG may wrap via /l/?uddg=<encoded>
        if "uddg=" in url:
            p = urlparse(url)
            q = parse_qs(p.query)
            if "uddg" in q:
                url = unquote(q["uddg"][0])
        # Strip any display-only path fragments (› etc. don't appear in href)
        m = re.search(r"(https?://(?:[a-z]{2}\.)?(?:www\.)?linkedin\.com/in/[^/?&#\s]+)", url)
        if not m:
            return None
        cleaned = m.group(1)
        # Normalize de.linkedin / at.linkedin / uk.linkedin → www
        cleaned = re.sub(r"https?://[a-z]{2}\.linkedin\.com", "https://www.linkedin.com", cleaned)
        return cleaned

    @staticmethod
    def _is_blocked(final_url: str, body_excerpt: str) -> bool:
        if "/static-pages/418" in final_url or "/418" in final_url:
            return True
        lowered = body_excerpt.lower()
        return (
            "unexpected error" in lowered
            or "unusual traffic" in lowered
            or "captcha" in lowered
        )

    # ------------------------------------------------------------------ search
    def search_query(self, query: str, max_pages: int = 1) -> List[SearchResult]:
        """Run one search query and return unique LinkedIn profile results.

        The main DDG JS page lazy-loads results via infinite scroll; max_pages
        controls how many scroll-load rounds we trigger before extracting.
        """
        if not self._page:
            raise RuntimeError("Call start() before search_query() (or use `with`).")

        url = f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"
        logger.info(f"[playwright-ddg] {query[:60]}")

        try:
            resp = self._page.goto(url, wait_until="load", timeout=30000)
        except Exception as e:
            logger.warning(f"  goto failed: {e}")
            return []

        try:
            self._page.wait_for_selector(
                "article[data-testid='result'], a[href*='linkedin.com/in']",
                timeout=8000,
            )
        except Exception:
            pass
        time.sleep(1.2)

        status = resp.status if resp else 0
        final_url = self._page.url
        body = ""
        try:
            body = self._page.locator("body").inner_text()[:300]
        except Exception:
            pass

        if self._is_blocked(final_url, body):
            logger.warning(f"  BLOCKED (final_url={final_url[:80]})")
            return []

        logger.info(f"  HTTP {status}")

        # Optionally scroll to trigger more results
        for _ in range(max(0, max_pages - 1)):
            try:
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)
            except Exception:
                break

        results: List[SearchResult] = []
        seen: Set[str] = set()

        # Each result is an <article data-testid="result"> with:
        #   a[data-testid="result-title-a"] — title link (href is the real URL)
        #   a[data-testid="result-extras-url-link"] — may be present too
        articles = self._page.query_selector_all("article[data-testid='result']")
        for art in articles:
            try:
                title_a = art.query_selector("a[data-testid='result-title-a']") or art.query_selector("a")
                if not title_a:
                    continue
                href = title_a.get_attribute("href") or ""
                cleaned = self._clean_linkedin_url(href)
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                title = (title_a.text_content() or "").strip()

                snippet_el = art.query_selector("[data-result='snippet']") or art.query_selector("span")
                snippet = (snippet_el.text_content() or "").strip() if snippet_el else ""

                results.append(SearchResult(url=cleaned, title=title, snippet=snippet, query=query))
            except Exception as e:
                logger.debug(f"  parse error: {e}")
                continue

        # Fallback: if articles didn't render, grab any linkedin anchor
        if not results:
            anchors = self._page.query_selector_all("a[href*='linkedin.com/in']")
            for a in anchors:
                href = a.get_attribute("href") or ""
                cleaned = self._clean_linkedin_url(href)
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                title = (a.text_content() or "").strip()
                results.append(SearchResult(url=cleaned, title=title, snippet="", query=query))

        # Deduplicate across the scraper's lifetime
        new_results = []
        for r in results:
            if r.url not in self.found_urls:
                self.found_urls.add(r.url)
                new_results.append(r)

        logger.info(f"  Found {len(new_results)} new LinkedIn profile URLs")
        return new_results

    def search_queries(self, queries: List[str], max_pages: int = 1) -> List[SearchResult]:
        """Convenience: run a list of queries with delays between them."""
        out: List[SearchResult] = []
        for i, q in enumerate(queries):
            out.extend(self.search_query(q, max_pages=max_pages))
            if i < len(queries) - 1:
                self._delay()
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sample_queries = [
        'site:linkedin.com/in "stealth" "founder" "berlin"',
        'site:linkedin.com/in "stealth mode" "munich"',
        'site:linkedin.com/in "serial entrepreneur" "vienna"',
    ]

    with PlaywrightDdgScraper(delay_range=(2, 4)) as s:
        all_results = s.search_queries(sample_queries)

    print(f"\nTotal unique profiles: {len(all_results)}")
    for r in all_results:
        print(f"  {r.title[:60]}")
        print(f"    {r.url}")
