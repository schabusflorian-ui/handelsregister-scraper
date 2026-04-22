"""
BundesAPI Handelsregister scraper.

Queries the official German Handelsregister portal (handelsregister.de)
with strict rate limiting to comply with legal requirements.

IMPORTANT: The official portal has a 60 requests/hour limit.
Exceeding this limit can violate German criminal law (§303a, b StGB).

Based on the approach from: https://github.com/bundesAPI/handelsregister

Form field names (verified 2026-02-02, extended 2026-02-17):
- Keywords: form:schlagwoerter (textarea) — NOT required if other params set
- Keyword mode: form:schlagwortOptionen (radio: 1=all, 2=at least one, 3=exact)
- Register type: form:registerArt_input (select: "", HRA, HRB, GnR, PR, VR, GsR)
- State checkboxes: form:{StateName}_input (e.g., form:Bayern_input)
- City: form:ort (text, max 30 chars, supports wildcards * and ?)
- Legal form: form:rechtsform (select, numeric codes — see LEGAL_FORM_CODES)
- Postal code: form:postleitzahl (text, max 5 chars)
- Shareholder: form:beteiligter (text)
- Results per page: form:ergebnisseProSeite (select: 10, 25, 50, 100)
- Submit: form:btnSuche
"""

import random
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Rotate across a small pool of realistic browser UAs to avoid fingerprinting
# on a single string. All are recent desktop browsers on macOS.
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# Cycle the HTTP session after this many requests to drop stale cookies and
# rotate the UA without tripping JSF ViewState (the scraper re-initialises on
# the next call).
_SESSION_MAX_REQUESTS = 50


@dataclass
class BundesAPIConfig:
    """Configuration for bundesAPI scraper."""

    base_url: str = "https://www.handelsregister.de"
    requests_per_hour: int = 60
    timeout: int = 45  # Increased timeout for slow portal
    max_retries: int = 5  # More retries for connection drops
    retry_delay: float = 3.0  # Longer delay between retries
    # Use a realistic browser user agent
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for API calls.

    Ensures we never exceed the legal 60 requests/hour limit.
    """

    def __init__(self, rate: int = 60, per_seconds: int = 3600):
        self.rate = rate
        self.per_seconds = per_seconds
        self.tokens = float(rate)
        self.last_update = time.time()
        self._requests_made = 0

    def acquire(self, timeout: float = 300) -> bool:
        """Acquire a token, blocking if necessary."""
        start_time = time.time()

        while True:
            now = time.time()
            elapsed = now - self.last_update

            self.tokens = min(float(self.rate), self.tokens + elapsed * (self.rate / self.per_seconds))
            self.last_update = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                self._requests_made += 1
                return True

            if now - start_time >= timeout:
                return False

            wait_time = (1.0 - self.tokens) * (self.per_seconds / self.rate)
            wait_time = min(wait_time, timeout - (now - start_time))

            if wait_time > 0:
                print(f"Rate limit: waiting {wait_time:.1f}s for next request...")
                time.sleep(wait_time)

    @property
    def requests_made(self) -> int:
        return self._requests_made

    @property
    def tokens_available(self) -> float:
        now = time.time()
        elapsed = now - self.last_update
        return min(float(self.rate), self.tokens + elapsed * (self.rate / self.per_seconds))


@dataclass
class SearchResult:
    """A single search result from Handelsregister."""

    name: str
    native_company_number: str
    registry_court: str
    registry_type: str
    status: Optional[str]
    state: Optional[str]
    city: Optional[str] = None
    row_index: Optional[int] = None  # Index in search results for VO fetching


@dataclass
class Announcement:
    """A single announcement (Veröffentlichung/Bekanntmachung) from the register."""

    company_name: str
    native_company_number: str
    announcement_date: Optional[str]
    announcement_type: Optional[str]  # e.g., Neueintragung, Kapitalerhöhung
    text: str
    capital_old: Optional[float] = None
    capital_new: Optional[float] = None
    city: Optional[str] = None
    state: Optional[str] = None
    registry_type: Optional[str] = None
    purpose: Optional[str] = None  # Business purpose (Gegenstand)
    postal_code: Optional[str] = None
    street: Optional[str] = None
    representation_rules: Optional[str] = None  # Vertretungsregelung


class BundesAPISource:
    """
    Scraper for the official Handelsregister portal.

    Uses direct navigation to advanced search page, then submits the search form.
    The portal uses JSF (JavaServer Faces) with ViewState for form handling.

    Respects the strict 60 requests/hour limit.
    """

    # German state names as used in form field names (form:{name}_input)
    # The portal uses full German state names, not codes
    STATES = {
        "bw": "Baden-Württemberg",
        "by": "Bayern",
        "be": "Berlin",
        "bb": "Brandenburg",
        "hb": "Bremen",
        "hh": "Hamburg",
        "he": "Hessen",
        "mv": "Mecklenburg-Vorpommern",
        "ni": "Niedersachsen",
        "nw": "Nordrhein-Westfalen",
        "rp": "Rheinland-Pfalz",
        "sl": "Saarland",
        "sn": "Sachsen",
        "st": "Sachsen-Anhalt",
        "sh": "Schleswig-Holstein",
        "th": "Thüringen",
    }

    # Valid register types
    REGISTER_TYPES = ["HRA", "HRB", "GnR", "PR", "VR", "GsR"]

    # Registry court codes for form:registergericht_input
    # Use these to search by specific court (HRB numbers are sequential per court)
    REGISTRY_COURTS = {
        "Berlin": "F1103",  # Amtsgericht Charlottenburg
        "München": "D2601",  # Amtsgericht München
        "Hamburg": "R2101",  # Amtsgericht Hamburg
        "Frankfurt": "R3201",  # Amtsgericht Frankfurt am Main
        "Köln": "R2707",  # Amtsgericht Köln
        "Düsseldorf": "R2701",  # Amtsgericht Düsseldorf
    }

    # Legal form codes for form:rechtsform dropdown
    # (verified from bundesAPI/handelsregister GitHub)
    LEGAL_FORM_CODES = {
        "AG": "1",  # Aktiengesellschaft
        "eG": "2",  # Eingetragene Genossenschaft
        "eV": "3",  # Eingetragener Verein
        "SE": "6",  # Europäische Aktiengesellschaft
        "GmbH": "8",  # Gesellschaft mit beschränkter Haftung (includes UG)
        "KG": "10",  # Kommanditgesellschaft
        "OHG": "12",  # Offene Handelsgesellschaft
        "Partnerschaft": "13",  # Partnerschaft
    }

    def __init__(self, config: Optional[BundesAPIConfig] = None):
        self.config = config or BundesAPIConfig()
        self.rate_limiter = TokenBucketRateLimiter(rate=self.config.requests_per_hour, per_seconds=3600)
        self._session = None
        self._initialized = False
        self._session_request_count = 0

    @property
    def session(self) -> requests.Session:
        """Lazy-initialize requests session with browser-like headers."""
        if self._session is None:
            self._session = requests.Session()
            # Rotate UA per session lifetime to reduce fingerprinting pressure
            ua = random.choice(_USER_AGENTS)
            self._session.headers.update(
                {
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                }
            )
            self._session_request_count = 0
        return self._session

    def reset_session(self):
        """Reset the session (useful after errors)."""
        if self._session:
            self._session.close()
        self._session = None
        self._initialized = False
        self._last_response = None
        self._session_request_count = 0

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """
        Make a request with retry logic.

        - Honours Retry-After on 429/503
        - Exponential backoff with jitter on 5xx and transport errors
        - Does not retry most 4xx (they're unlikely to change with a retry)
        - Cycles the session every _SESSION_MAX_REQUESTS to drop stale cookies
        """
        kwargs.setdefault("timeout", self.config.timeout)

        # Cycle the session periodically so we don't ride the same cookies
        # and UA forever. Skip during session-initialization (when the state
        # hasn't been set up yet) to avoid tearing down mid-handshake.
        if self._initialized and self._session_request_count >= _SESSION_MAX_REQUESTS:
            self.reset_session()

        last_error: Optional[Exception] = None
        for attempt in range(self.config.max_retries):
            try:
                if method.lower() == "get":
                    response = self.session.get(url, **kwargs)
                else:
                    response = self.session.post(url, **kwargs)
                self._session_request_count += 1

                status = response.status_code
                if status == 429 or status >= 500:
                    # Server is telling us to slow down or is flaking.
                    wait = self._compute_backoff(attempt, response)
                    if attempt < self.config.max_retries - 1:
                        print(
                            f"Request returned {status} (attempt {attempt + 1}), "
                            f"backing off {wait:.1f}s..."
                        )
                        time.sleep(wait)
                        continue
                # All other statuses — let raise_for_status surface 4xx errors
                response.raise_for_status()
                return response
            except (requests.RequestException, ConnectionError) as e:
                last_error = e
                if attempt < self.config.max_retries - 1:
                    wait = self._compute_backoff(attempt, None)
                    print(
                        f"Request failed (attempt {attempt + 1}): {type(e).__name__}, "
                        f"backing off {wait:.1f}s..."
                    )
                    time.sleep(wait)
                else:
                    raise

        # Exhausted retries with no exception (e.g., repeated 5xx/429)
        if last_error is not None:
            raise last_error
        # Surface the final 5xx/429 response as an HTTPError
        response.raise_for_status()
        return response

    def _compute_backoff(self, attempt: int, response: Optional[requests.Response]) -> float:
        """
        Compute backoff delay: honour Retry-After header if present, else
        exponential with full-jitter (cap 60s).
        """
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.0, min(60.0, float(retry_after)))
                except ValueError:
                    pass  # RFC date format — fall through to exponential
        base = self.config.retry_delay * (2 ** attempt)
        return min(60.0, base + random.uniform(0, base * 0.5))

    def _initialize_session(self) -> bool:
        """
        Initialize session by navigating directly to the advanced search page.

        The portal allows direct navigation to the advanced search URL.
        Returns True if successful, False otherwise.
        """
        if self._initialized:
            return True

        try:
            # Navigate directly to advanced search page
            # This is more reliable than clicking through the welcome page
            advanced_search_url = f"{self.config.base_url}/rp_web/erweitertesuche/welcome.xhtml"

            print(f"Navigating to advanced search: {advanced_search_url}")
            response = self._make_request("get", advanced_search_url)

            # Verify we're on the advanced search page by checking for the keywords field
            if "schlagwoerter" in response.text.lower() or "schlagwort" in response.text.lower():
                self._initialized = True
                self._last_response = response
                print("Successfully reached advanced search page")
                return True
            else:
                print(f"Did not reach advanced search page. URL: {response.url}")
                # Try to diagnose the issue
                if "session" in response.text.lower() and "abgelaufen" in response.text.lower():
                    print("Session expired message detected")
                return False

        except requests.RequestException as e:
            print(f"Error initializing session: {e}")
            self.reset_session()
            return False

    def search(
        self,
        keywords: Optional[List[str]] = None,
        keyword_mode: str = "all",  # Changed default to 'all' - more reliable
        states: Optional[List[str]] = None,
        registry_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        max_results: int = 100,
        shareholder_name: Optional[str] = None,
        city: Optional[str] = None,
        legal_form_code: Optional[str] = None,
        postal_code: Optional[str] = None,
        results_per_page: int = 100,
        register_number: Optional[str] = None,
        register_court: Optional[str] = None,
    ) -> Iterator[SearchResult]:
        """
        Search for companies matching criteria.

        Supports keyword-free search: the portal requires at least one search
        parameter (keywords, city, legal_form_code, state, etc.) but keywords
        are NOT mandatory.

        Args:
            keywords: Search terms for company name (optional if other params set)
            keyword_mode: 'all' (contain all keywords), 'min' (at least one), or 'exact'
            states: List of state codes (e.g., ['by', 'be']) - maps to full state names
            registry_types: List of registry types (HRA, HRB, GnR, PR, VR, GsR)
            include_deleted: Include deleted/dissolved companies
            max_results: Maximum results to return (with pagination, can exceed 100)
            shareholder_name: Search by shareholder/participant name
            city: Filter by city name (max 30 chars, supports wildcards * and ?)
            legal_form_code: Legal form code (e.g., '8' for GmbH — see LEGAL_FORM_CODES)
            postal_code: Filter by postal code (max 5 chars)
            results_per_page: Results per page (10, 25, 50, or 100)
            register_number: Registration number (supports wildcards * and ?, e.g., '283*')
            register_court: Registry court code (e.g., 'F1103' for Berlin Charlottenburg)

        Yields:
            SearchResult objects
        """
        # Acquire rate limit token for initialization
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        # Initialize session if needed (this counts as 1 request)
        if not self._initialize_session():
            print("Failed to initialize session with handelsregister.de")
            # Try resetting and retrying once
            self.reset_session()
            if not self.rate_limiter.acquire():
                print("Rate limit timeout on retry")
                return
            if not self._initialize_session():
                print("Failed to initialize session after reset")
                return

        # Map keyword mode to numeric value (verified from browser inspection)
        # 1 = contain all keywords
        # 2 = contain at least one keyword
        # 3 = contain the exact name of the company
        mode_map = {"all": "1", "min": "2", "exact": "3"}
        keyword_mode_value = mode_map.get(keyword_mode, "1")

        # Get the search form from the last response
        soup = BeautifulSoup(self._last_response.text, "lxml")

        # Find the search form
        search_form = soup.find("form", {"name": "form"}) or soup.find("form", id="form")
        if not search_form:
            print("Could not find search form on page")
            # Debug: print what forms are available
            forms = soup.find_all("form")
            print(f"Available forms: {[f.get('name', f.get('id', 'unnamed')) for f in forms]}")
            return

        # Get form action
        form_action = search_form.get("action", "")
        form_url = urljoin(self.config.base_url, form_action) if form_action else self._last_response.url

        # Get ViewState (required for JSF forms)
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
        viewstate_value = viewstate.get("value", "") if viewstate else ""

        if not viewstate_value:
            print("Warning: No ViewState found - form submission may fail")

        # Build search form data with verified field names
        form_data = {
            "form": "form",
            "suchTyp": "e",  # Extended search type
            "form:schlagwoerter": " ".join(keywords) if keywords else "",
            "form:schlagwortOptionen": keyword_mode_value,
            "javax.faces.ViewState": viewstate_value,
            "form:btnSuche": "",  # Submit button (empty value triggers the button)
        }

        # Add shareholder/participant name search if specified
        # This searches the "Name des Beteiligten" field
        if shareholder_name:
            form_data["form:beteiligter"] = shareholder_name

        # City filter (form:ort) — max 30 chars, supports wildcards * and ?
        if city:
            form_data["form:ort"] = city[:30]

        # Legal form filter — PrimeFaces SelectOneMenu uses _input suffix
        if legal_form_code:
            form_data["form:rechtsform_input"] = legal_form_code

        # Postal code filter (form:postleitzahl) — max 5 chars
        if postal_code:
            form_data["form:postleitzahl"] = postal_code[:5]

        # Registration number (form:registerNummer) — supports wildcards * and ?
        if register_number:
            form_data["form:registerNummer"] = register_number[:10]

        # Registry court (form:registergericht) — PrimeFaces uses _input suffix
        if register_court:
            form_data["form:registergericht_input"] = register_court

        # Results per page — PrimeFaces SelectOneMenu uses _input suffix
        if results_per_page in (10, 25, 50, 100):
            form_data["form:ergebnisseProSeite_input"] = str(results_per_page)

        # Registry type - use select dropdown value
        # For 'all' mode, we can leave it empty (all types)
        # For 'min' mode, a specific type may be required
        if registry_types:
            # Single value for select dropdown
            form_data["form:registerArt_input"] = registry_types[0] if len(registry_types) == 1 else ""
        # If no registry_types specified, leave empty for "all"

        # Add state checkboxes if specified
        # The form uses full state names: form:{StateName}_input
        if states:
            for state_code in states:
                state_name = self.STATES.get(state_code.lower())
                if state_name:
                    form_data[f"form:{state_name}_input"] = "on"

        # Include deleted companies checkbox
        if include_deleted:
            # Find the actual field name for this option
            deleted_checkbox = soup.find("input", {"type": "checkbox", "id": lambda x: x and "geloescht" in x.lower()})
            if deleted_checkbox:
                form_data[deleted_checkbox.get("name", "form:geloescht")] = "on"

        # Acquire rate limit token for search
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        try:
            search_desc = " ".join(keywords) if keywords else "[no keywords]"
            if city:
                search_desc += f" city={city}"
            if legal_form_code:
                search_desc += f" rechtsform={legal_form_code}"
            if postal_code:
                search_desc += f" plz={postal_code}"
            print(f"Submitting search for: {search_desc}")

            # Submit search with proper headers for form submission
            response = self._make_request(
                "post",
                form_url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.config.base_url,
                    "Referer": self._last_response.url,
                },
            )

            # Check if we got results or an error
            if "sucheErgebnisse" in response.url or "Search Result" in response.text:
                print(f"Search results page reached: {response.url}")
                # Store search results page for VÖ fetching
                self._search_results_response = response
            elif "erweitertesuche" in response.url:
                # Still on search page - might be a validation error
                if "error" in response.text.lower() or "fehler" in response.text.lower():
                    print("Form validation error detected")
                    # Try to extract error message
                    error_soup = BeautifulSoup(response.text, "lxml")
                    error_msgs = error_soup.find_all(class_=lambda c: c and "error" in c.lower())
                    for msg in error_msgs[:3]:
                        print(f"  Error: {msg.get_text(strip=True)[:100]}")

            # Parse first page of results
            results = self._parse_search_results(response.text)
            total_yielded = 0
            print(f"Found {len(results)} results on page 1")

            for result in results:
                if total_yielded >= max_results:
                    return
                yield result
                total_yielded += 1

            # Pagination: fetch additional pages if needed
            if total_yielded < max_results and len(results) >= results_per_page:
                page_num = 1
                current_response = response
                while total_yielded < max_results:
                    page_num += 1
                    page_results = self._fetch_next_page(current_response, page_num, results_per_page)
                    if not page_results:
                        break  # No more pages or pagination failed
                    print(f"Found {len(page_results)} results on page {page_num}")
                    for result in page_results:
                        if total_yielded >= max_results:
                            return
                        yield result
                        total_yielded += 1
                    # If we got fewer results than page size, we're on the last page
                    if len(page_results) < results_per_page:
                        break

            print(f"Total results yielded: {total_yielded}")

        except requests.RequestException as e:
            print(f"Search request error: {e}")
            self.reset_session()  # Reset session on error
            return

    def _parse_search_results(self, html: str) -> List[SearchResult]:
        """Parse HTML search results into SearchResult objects.

        The results table structure (verified 2026-02-02):
        - Table with role='grid'
        - Rows with data-ri attribute contain actual company data
        - Each row has cells with:
          - cells[1]: Court info like "Bavaria   District court Augsburg HRB 19414"
          - cells[2]: Company name
          - cells[3]: City (registered office)
          - cells[4]: Status like "currently registered"
        """
        soup = BeautifulSoup(html, "lxml")
        results = []

        # Find result table (uses role='grid' attribute)
        table = soup.find("table", {"role": "grid"})
        if not table:
            # Try alternative: look for data table
            table = soup.find("table", class_=lambda c: c and "dataTable" in c)

        if not table:
            # Check if there's a "too many results" error
            too_many = soup.find(
                string=re.compile(r"zu viele|too many|ergebnismenge.*zu groß|eingrenzen|narrow", re.IGNORECASE)
            )
            if too_many:
                print("Too many results — narrow your search (add postal_code or keywords)")
                return results

            # Check if there's an error or no results message
            no_results = soup.find(string=re.compile(r"keine.*(treffer|ergebnis)|no.*result", re.IGNORECASE))
            if no_results:
                print("No results found on page")
            return results

        # Find all data rows with data-ri attribute (these are the company rows)
        tbody = table.find("tbody")
        if tbody:
            rows = tbody.find_all("tr", {"data-ri": True})
        else:
            rows = table.find_all("tr", {"data-ri": True})

        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 5:
                continue

            try:
                # Get row index from data-ri attribute (for VÖ fetching)
                row_index = int(row.get("data-ri", -1))

                # Extract data from specific cell positions
                # cells[1]: Court/registry info
                # cells[2]: Company name
                # cells[3]: City
                # cells[4]: Status

                court_info = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                name = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                city = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                status = cells[4].get_text(strip=True) if len(cells) > 4 else ""

                # Skip if no company name
                if not name:
                    continue

                # Parse court info to extract state, court, register type, and number
                # Format: "Bavaria   District court Augsburg HRB 19414"
                # or German: "Bayern   Amtsgericht München HRB 123456"
                state = ""
                registry_court = ""
                registry_type = ""
                register_number = ""

                # English pattern
                match = re.match(
                    r"([\w\-\s]+?)\s+District court\s+([\w\-\s]+?)\s+(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)",
                    court_info,
                    re.IGNORECASE,
                )
                if not match:
                    # German pattern
                    match = re.match(
                        r"([\w\-\s]+?)\s+Amtsgericht\s+([\w\-\s]+?)\s+(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)",
                        court_info,
                        re.IGNORECASE,
                    )

                if match:
                    state = match.group(1).strip()
                    court_city = match.group(2).strip()
                    registry_type = match.group(3).upper()
                    register_number = match.group(4)
                    registry_court = f"District court {court_city}"
                else:
                    # Fallback: try to extract at least registry type and number
                    type_match = re.search(r"(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)", court_info, re.IGNORECASE)
                    if type_match:
                        registry_type = type_match.group(1).upper()
                        register_number = type_match.group(2)

                # Build native company number
                native_company_number = f"{registry_court} {registry_type} {register_number}".strip()
                if not native_company_number or native_company_number == "  ":
                    native_company_number = court_info  # Fallback to full court info

                # Normalize status
                normalized_status = status
                if status:
                    status_lower = status.lower()
                    if "aktuell" in status_lower or "currently" in status_lower or "registered" in status_lower:
                        normalized_status = "currently registered"
                    elif "gelöscht" in status_lower or "deleted" in status_lower:
                        normalized_status = "deleted"
                    elif "aufgelöst" in status_lower or "dissolved" in status_lower:
                        normalized_status = "dissolved"

                results.append(
                    SearchResult(
                        name=name,
                        native_company_number=native_company_number,
                        registry_court=registry_court,
                        registry_type=registry_type,
                        status=normalized_status if normalized_status else None,
                        state=state if state else None,  # Don't use city as state fallback
                        city=city if city else None,
                        row_index=row_index,
                    )
                )

            except Exception as e:
                print(f"Error parsing row: {e}")
                continue

        return results

    def _fetch_next_page(
        self,
        current_response: requests.Response,
        page_num: int,
        rows_per_page: int,
    ) -> List["SearchResult"]:
        """
        Fetch the next page of search results using PrimeFaces AJAX pagination.

        PrimeFaces DataTable sends an AJAX POST with partial rendering params.
        The exact component IDs are extracted from the initial response HTML.

        Args:
            current_response: The response from the previous page
            page_num: The page number to fetch (1-based, page 1 already fetched)
            rows_per_page: Number of rows per page

        Returns:
            List of SearchResult objects, or empty list if pagination fails
        """
        # Acquire rate limit token
        if not self.rate_limiter.acquire():
            print("Rate limit timeout during pagination")
            return []

        try:
            soup = BeautifulSoup(current_response.text, "lxml")

            # Find the DataTable component ID from the grid table
            table = soup.find("table", {"role": "grid"})
            if not table:
                print("No DataTable found for pagination")
                return []

            # The table's parent container typically has the PrimeFaces widget ID
            # Look for the paginator element to find the DataTable ID
            datatable_id = None

            # PrimeFaces DataTable has id like "form:ergebnisListe" or "form:data"
            # The table itself or its parent div usually has an id
            if table.get("id"):
                datatable_id = table["id"].replace("_data", "")
            else:
                # Check parent elements
                parent = table.parent
                while parent and not datatable_id:
                    if parent.get("id") and "data" in parent.get("class", []):
                        datatable_id = parent["id"]
                        break
                    parent = parent.parent if parent.parent else None

            # Also try to find paginator elements
            paginator = soup.find("div", class_=lambda c: c and "ui-paginator" in c)
            if paginator:
                # Extract DataTable ID from paginator's id (e.g., "form:ergebnisListe_paginator_top")
                pag_id = paginator.get("id", "")
                if "_paginator" in pag_id:
                    datatable_id = pag_id.split("_paginator")[0]

            if not datatable_id:
                # Fallback: search for common PrimeFaces DataTable IDs
                for candidate_id in ["form:ergebnisListe", "form:dataList", "form:data", "form:suchergebnisseForm"]:
                    if soup.find(id=candidate_id) or soup.find(id=f"{candidate_id}_data"):
                        datatable_id = candidate_id
                        break

            if not datatable_id:
                print("Could not determine DataTable component ID for pagination")
                return []

            print(f"Pagination: DataTable ID = {datatable_id}, fetching page {page_num}")

            # Get current ViewState
            viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
            viewstate_value = viewstate.get("value", "") if viewstate else ""

            # Calculate first row offset (0-based)
            first_row = (page_num - 1) * rows_per_page

            # Build PrimeFaces AJAX pagination request
            # The form on the results page is 'ergebnissForm', not 'form'
            form_name = datatable_id.split(":")[0] if ":" in datatable_id else "ergebnissForm"
            ajax_data = {
                "javax.faces.partial.ajax": "true",
                "javax.faces.source": datatable_id,
                "javax.faces.partial.execute": datatable_id,
                "javax.faces.partial.render": datatable_id,
                f"{datatable_id}_pagination": "true",
                f"{datatable_id}_first": str(first_row),
                f"{datatable_id}_rows": str(rows_per_page),
                form_name: form_name,
                "javax.faces.ViewState": viewstate_value,
            }

            # Submit AJAX request with appropriate headers
            response = self._make_request(
                "post",
                current_response.url,
                data=ajax_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.config.base_url,
                    "Referer": current_response.url,
                    "Faces-Request": "partial/ajax",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

            # PrimeFaces AJAX returns XML with partial HTML updates
            # Format: <partial-response><changes><update id="..."><![CDATA[...html...]]></update></changes></partial-response>
            # There are multiple CDATA sections — find the DataTable one (largest)
            response_text = response.text

            # Parse all update sections with their IDs
            all_updates = re.findall(r'<update id="([^"]+)"><!\[CDATA\[(.*?)\]\]></update>', response_text, re.DOTALL)

            # Find the DataTable update (matches our datatable_id)
            html_content = None
            for update_id, content in all_updates:
                if update_id == datatable_id:
                    html_content = content
                    break

            if not html_content:
                # Fallback: use the largest CDATA section (likely the DataTable)
                if all_updates:
                    html_content = max(all_updates, key=lambda x: len(x[1]))[1]
                else:
                    # Not an AJAX response — try parsing as regular HTML
                    html_content = response_text

            # Parse the results from this page
            results = self._parse_search_results(html_content)
            return results

        except requests.RequestException as e:
            print(f"Pagination request error (page {page_num}): {e}")
            return []
        except Exception as e:
            print(f"Pagination parsing error (page {page_num}): {e}")
            return []

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Get current rate limit status."""
        return {
            "requests_made": self.rate_limiter.requests_made,
            "tokens_available": self.rate_limiter.tokens_available,
            "max_per_hour": self.config.requests_per_hour,
        }

    def fetch_announcements(
        self,
        search_result: SearchResult,
    ) -> List[Announcement]:
        """
        Fetch announcements (Veröffentlichungen) for a company from search results.

        This method clicks the VÖ (Veröffentlichungen) button for a company
        in the current search results to retrieve its announcements.

        IMPORTANT: This must be called while the search results page is still
        active (i.e., after a search() call, before a new search or reset).

        Args:
            search_result: A SearchResult object with row_index set

        Returns:
            List of Announcement objects
        """
        if not hasattr(self, "_search_results_response") or not self._search_results_response:
            print("No search results page available. Run search() first.")
            return []

        if search_result.row_index is None or search_result.row_index < 0:
            print("SearchResult has no valid row_index")
            return []

        # Parse the search results page
        soup = BeautifulSoup(self._search_results_response.text, "lxml")
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
        viewstate_value = viewstate.get("value", "") if viewstate else ""

        # Find the VÖ link for this row
        # Pattern: ergebnissForm:selectedSuchErgebnisFormTable:{row}:j_idt227:5:fade1_
        # The j_idt number can vary, so use regex
        row_idx = search_result.row_index
        vo_link = soup.find(
            "a", id=re.compile(f"ergebnissForm:selectedSuchErgebnisFormTable:{row_idx}:j_idt\\d+:5:fade")
        )

        if not vo_link:
            # Try alternate pattern without trailing underscore
            vo_link = soup.find("a", id=re.compile(f"ergebnissForm:selectedSuchErgebnisFormTable:{row_idx}:.*:5:"))

        if not vo_link:
            print(f"No VÖ link found for row {row_idx}")
            return []

        link_id = vo_link.get("id")

        # Build form data to click the VÖ button
        form_data = {
            "ergebnissForm": "ergebnissForm",
            "property2": "",
            "property": "Global.Dokumentart.VÖ",
            link_id: link_id,
            "javax.faces.ViewState": viewstate_value,
        }

        try:
            # Acquire rate limit token
            if not self.rate_limiter.acquire(timeout=30):
                print("Rate limit timeout for VÖ request")
                return []

            # Submit the VÖ request
            response = self._make_request(
                "post",
                self._search_results_response.url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.config.base_url,
                    "Referer": self._search_results_response.url,
                },
            )

            # Parse the announcements page
            return self._parse_announcements(
                response.text,
                company_name=search_result.name,
                native_company_number=search_result.native_company_number,
            )

        except requests.RequestException as e:
            print(f"VÖ request error: {e}")
            return []

    def _parse_announcements(
        self,
        html: str,
        company_name: str,
        native_company_number: str,
    ) -> List[Announcement]:
        """
        Parse announcements from the VÖ (Veröffentlichungen) page.

        The page structure:
        - Main container: div.ui-datalist with header "Veröffentlichungen"
        - List items: li.ui-datalist-item containing announcement text
        - Empty state: div.ui-datalist-empty-message
        """
        soup = BeautifulSoup(html, "lxml")
        announcements = []

        # Find all list items
        list_items = soup.find_all("li", class_="ui-datalist-item")

        if not list_items:
            # Check for empty message
            empty_msg = soup.find("div", class_="ui-datalist-empty-message")
            if empty_msg:
                return []  # No announcements

        for item in list_items:
            text = item.get_text(separator="\n", strip=True)

            # Try to extract date from text
            # Common patterns: "12.03.2024", "2024-03-12"
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})", text)
            announcement_date = date_match.group(1) if date_match else None

            # Try to determine announcement type
            announcement_type = self._classify_announcement_type(text)

            # Try to extract capital amounts
            capital_old, capital_new = self._extract_capital_amounts(text)

            # Extract purpose (Gegenstand) — present in neueintragung and some amendments
            purpose = self._extract_purpose(text)

            # Extract address
            postal_code, street = self._extract_address(text)

            # Extract representation rules (Vertretungsregelung)
            representation_rules = self._extract_representation_rules(text)

            announcements.append(
                Announcement(
                    company_name=company_name,
                    native_company_number=native_company_number,
                    announcement_date=announcement_date,
                    announcement_type=announcement_type,
                    text=text,
                    capital_old=capital_old,
                    capital_new=capital_new,
                    purpose=purpose,
                    postal_code=postal_code,
                    street=street,
                    representation_rules=representation_rules,
                )
            )

        return announcements

    def _classify_announcement_type(self, text: str) -> Optional[str]:
        """
        Classify the type of announcement based on its text content.

        Returns one of:
        - neueintragung: New company registration
        - kapitalerhoehung: Capital increase
        - kapitalherabsetzung: Capital decrease
        - geschaeftsfuehrer: Managing director change
        - sitzverlegung: Registered office change
        - umwandlung: Transformation (merger, split, etc.)
        - aufloesung: Dissolution
        - loeschung: Deletion from register
        - prokura: Procuration (power of attorney) change
        - sonstiges: Other
        """
        text_lower = text.lower()

        # Check patterns in order of specificity
        if any(kw in text_lower for kw in ["neueintragung", "erstmalige eintragung", "neue firma", "ist eingetragen"]):
            return "neueintragung"

        if any(
            kw in text_lower
            for kw in [
                "kapitalerhöhung",
                "erhöhung des stammkapitals",
                "erhöhung des grundkapitals",
                "kapital erhöht",
                "capital increase",
            ]
        ):
            return "kapitalerhoehung"

        if any(
            kw in text_lower
            for kw in [
                "kapitalherabsetzung",
                "herabsetzung des stammkapitals",
                "kapital herabgesetzt",
                "capital decrease",
            ]
        ):
            return "kapitalherabsetzung"

        if any(
            kw in text_lower
            for kw in [
                "geschäftsführer",
                "geschäftsführerin",
                "managing director",
                "bestellt",
                "abberufen",
                "nicht mehr geschäftsführer",
            ]
        ):
            return "geschaeftsfuehrer"

        if any(kw in text_lower for kw in ["sitzverlegung", "sitz verlegt", "neuer sitz", "registered office changed"]):
            return "sitzverlegung"

        if any(
            kw in text_lower
            for kw in ["umwandlung", "verschmelzung", "spaltung", "formwechsel", "merger", "transformation"]
        ):
            return "umwandlung"

        if any(kw in text_lower for kw in ["auflösung", "liquidation", "dissolution", "aufgelöst"]):
            return "aufloesung"

        if any(kw in text_lower for kw in ["löschung", "gelöscht", "deletion", "von amts wegen gelöscht"]):
            return "loeschung"

        if any(kw in text_lower for kw in ["prokura", "prokurist", "power of attorney"]):
            return "prokura"

        return "sonstiges"

    def _extract_capital_amounts(self, text: str) -> tuple:
        """
        Extract old and new capital amounts from announcement text.

        Returns:
            (capital_old, capital_new) tuple, values are float or None
        """
        # Common patterns for German capital amounts:
        # "25.000,00 EUR" or "25.000 EUR" or "EUR 25.000,00"
        # "Stammkapital: 25.000,00 EUR"

        capital_pattern = r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR|€)"

        # Find all capital amounts
        matches = re.findall(capital_pattern, text)

        if not matches:
            return (None, None)

        # Convert to float
        def to_float(s):
            # "25.000,00" -> 25000.00
            return float(s.replace(".", "").replace(",", "."))

        amounts = [to_float(m) for m in matches]

        # If two amounts, likely old -> new
        if len(amounts) >= 2:
            return (amounts[0], amounts[1])
        elif len(amounts) == 1:
            # Single amount - probably the new capital
            return (None, amounts[0])

        return (None, None)

    @staticmethod
    def _extract_announcement_date(
        onclick: str,
        text: str,
        fallback: Optional[str] = None,
    ) -> Optional[str]:
        """
        Extract an announcement date with a chain of fallbacks. Returns a
        string in DD.MM.YYYY format (portal convention) or None.

        Search order:
          1. JS Date string in onclick  ("Mon Feb 02 00:00:00 CET 2026")
          2. ISO-like date in onclick   ("2026-02-02")
          3. German DD.MM.YYYY anywhere in the row text
          4. ISO YYYY-MM-DD in the row text
          5. fallback (typically the search's date_from)
        """
        if onclick:
            m = re.search(r"'(\w+ \w+ \d+ \d+:\d+:\d+ \w+ \d+)'", onclick)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%a %b %d %H:%M:%S %Z %Y")
                    return dt.strftime("%d.%m.%Y")
                except Exception:  # noqa: BLE001
                    pass
            m = re.search(r"(\d{4}-\d{2}-\d{2})", onclick)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d.%m.%Y")
                except Exception:  # noqa: BLE001
                    return m.group(1)

        if text:
            m = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
            if m:
                return m.group(1)
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
            if m:
                try:
                    return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d.%m.%Y")
                except Exception:  # noqa: BLE001
                    return m.group(1)

        return fallback

    @staticmethod
    def _extract_purpose(text: str) -> Optional[str]:
        """
        Extract business purpose (Gegenstand) from announcement text.

        German Handelsregister announcements for new registrations typically contain:
        "Gegenstand: <purpose description>." or
        "Gegenstand des Unternehmens: <purpose description>."
        """
        # Pattern 1: "Gegenstand: ..." or "Gegenstand des Unternehmens: ..."
        m = re.search(
            r"Gegenstand(?:\s+des\s+Unternehmens)?:\s*(.+?)(?:\.\s*(?:Stammkapital|Kapital|Geschäftsführer|Alleiniger|Vertretung|Bestellt|Sitz|Eingetragen|Rechtsform|Gesellschaft mit)|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            purpose = m.group(1).strip().rstrip(".")
            if len(purpose) > 10:  # Ignore very short matches
                return purpose[:1000]

        # Pattern 2: "Unternehmensgegenstand: ..."
        m = re.search(
            r"Unternehmensgegenstand:\s*(.+?)(?:\.\s*(?:Stammkapital|Kapital|Geschäftsführer|Sitz)|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            purpose = m.group(1).strip().rstrip(".")
            if len(purpose) > 10:
                return purpose[:1000]

        return None

    @staticmethod
    def _extract_address(text: str) -> tuple:
        """
        Extract postal code and street from announcement text.

        Returns:
            (postal_code, street) tuple
        """
        # Common patterns: "Geschäftsanschrift: Musterstr. 1, 12345 Berlin"
        # or "Sitz: Berlin, Geschäftsanschrift: Musterstr. 1, 12345 Berlin"
        m = re.search(
            r"Geschäftsanschrift:\s*(.+?),\s*(\d{5})\s+\w",
            text,
            re.IGNORECASE,
        )
        if m:
            return (m.group(2), m.group(1).strip())

        # Try standalone postal code pattern near address context
        m = re.search(r"(\d{5})\s+(?:Berlin|München|Hamburg|Köln|Frankfurt|Stuttgart|Düsseldorf|Leipzig|Dresden|Hannover|Nürnberg|Bremen|Essen|Dortmund|Bonn|Mannheim|Augsburg|Wiesbaden|Karlsruhe|Heidelberg|Freiburg|Aachen)", text)
        if m:
            return (m.group(1), None)

        return (None, None)

    @staticmethod
    def _extract_representation_rules(text: str) -> Optional[str]:
        """
        Extract representation rules (Vertretungsregelung) from announcement text.

        Common patterns in German Handelsregister announcements:
        - "Vertretungsregelung: Jeder Geschäftsführer vertritt einzeln."
        - "Ist nur ein Geschäftsführer bestellt, vertritt er allein."
        - "Einzelvertretungsbefugnis"
        - "Gemeinsame Vertretung durch zwei Geschäftsführer"
        """
        # Pattern 1: explicit "Vertretungsregelung:" label
        m = re.search(
            r"Vertretungsregelung:\s*(.+?)(?:\.\s*(?:Geschäftsführer|Bestellt|Prokura|Stammkapital|Rechtsform|Eingetragen|Geschäftsanschrift|Gegenstand)|\.?\s*$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            rule = m.group(1).strip().rstrip(".")
            if len(rule) > 5:
                return rule[:500]

        # Pattern 2: "Einzelvertretungsbefugnis" or "Gesamtvertretung" standalone
        m = re.search(
            r"((?:Einzel|Gesamt)vertretungsbefugnis[^.]*|(?:Jeder|Jede)\s+Geschäftsführer(?:in)?\s+(?:ist\s+)?(?:einzeln\s+)?vertretungsberechtigt[^.]*|"
            r"Ist\s+nur\s+ein\s+Geschäftsführer\s+bestellt[^.]*vertritt[^.]*)",
            text,
            re.IGNORECASE,
        )
        if m:
            rule = m.group(0).strip().rstrip(".")
            if len(rule) > 5:
                return rule[:500]

        # Pattern 3: "Mit der Befugnis ... im Namen der Gesellschaft"
        m = re.search(
            r"(Mit\s+der\s+Befugnis[^.]+Gesellschaft[^.]*)",
            text,
            re.IGNORECASE,
        )
        if m:
            rule = m.group(1).strip().rstrip(".")
            if len(rule) > 5:
                return rule[:500]

        return None

    def search_announcements(
        self,
        date_from: str,
        date_to: str,
        state: Optional[str] = None,
        category: Optional[str] = None,
        max_results: int = 100,
    ) -> Iterator[Announcement]:
        """
        Search for register announcements (Registerbekanntmachungen) by date range.

        This uses the dedicated Registerbekanntmachungen search page, which is
        different from the VÖ tab. This page allows searching all announcements
        by date range, state, court, and category.

        Args:
            date_from: Start date in format "DD.MM.YYYY"
            date_to: End date in format "DD.MM.YYYY"
            state: Optional state code (e.g., 'by' for Bayern)
            category: Optional category:
                      - "1": Löschungsankündigung (deletion announcement)
                      - "2": Umwandlungsgesetz (transformation law)
                      - "3": Einreichung neuer Dokumente (new documents)
                      - "4": Sonstige (other)
                      - "5": Sonderregisterbekanntmachung (special)
                      - "" or None: All categories
            max_results: Maximum results to return

        Yields:
            Announcement objects
        """
        # Acquire rate limit token
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        # Navigate to Registerbekanntmachungen page
        bk_url = f"{self.config.base_url}/rp_web/bekanntmachungen/welcome.xhtml"

        try:
            response = self._make_request("get", bk_url)
        except requests.RequestException as e:
            print(f"Error navigating to Registerbekanntmachungen: {e}")
            return

        soup = BeautifulSoup(response.text, "lxml")
        viewstate = soup.find("input", {"name": "javax.faces.ViewState"})
        viewstate_value = viewstate.get("value", "") if viewstate else ""

        # Map state code to state name
        state_name = ""
        if state:
            state_name = self.STATES.get(state.lower(), "")

        # Build form data
        form_data = {
            "bekanntMachungenForm": "bekanntMachungenForm",
            "bekanntMachungenForm:datum_von_input": date_from,
            "bekanntMachungenForm:datum_bis_input": date_to,
            "bekanntMachungenForm:land_input": state_name,
            "bekanntMachungenForm:registergericht_input": "",
            "bekanntMachungenForm:sitz": "",
            "bekanntMachungenForm:kategorie_input": category or "",
            "javax.faces.ViewState": viewstate_value,
            "bekanntMachungenForm:rrbSuche": "",
        }

        # Acquire rate limit token for search
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        try:
            print(f"Searching announcements from {date_from} to {date_to}...")

            search_response = self._make_request(
                "post",
                response.url,
                data=form_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.config.base_url,
                    "Referer": response.url,
                },
            )

            # Parse results — pass the search's date range so we can fall
            # back to it when per-row date extraction fails.
            announcements = self._parse_bekanntmachungen_results(
                search_response.text,
                date_from=date_from,
            )
            print(f"Found {len(announcements)} announcements")

            for i, ann in enumerate(announcements):
                if i >= max_results:
                    break
                yield ann

        except requests.RequestException as e:
            print(f"Announcement search error: {e}")
            return

    def _parse_bekanntmachungen_results(
        self,
        html: str,
        date_from: Optional[str] = None,
    ) -> List[Announcement]:
        """
        Parse announcements from the Registerbekanntmachungen results page.

        Each announcement is a link (a.ui-commandlink) containing a label with:
        - Category (e.g., "Löschungsankündigung")
        - State and court (e.g., "Bayern Amtsgericht München HRB 12345")
        - Company name and city

        date_from is used as a fallback when per-row date extraction fails —
        better a search-window lower bound than NULL (the old behaviour
        produced 0 dated rows across 31k announcements on production).
        """
        soup = BeautifulSoup(html, "lxml")
        announcements = []

        # Find datalist container
        datalist = soup.find("div", class_="ui-datalist")
        if not datalist:
            return announcements

        # Find all announcement links
        items = datalist.find_all("a", class_="ui-commandlink")

        for item in items:
            label = item.find("label")
            if not label:
                continue

            text = label.get_text(separator="\n", strip=True)
            lines = [l.strip() for l in text.split("\n") if l.strip()]

            if len(lines) < 3:
                continue

            # Parse the announcement
            # Line 0: Category (e.g., "Löschungsankündigung")
            # Line 1: State Court RegistryType Number (e.g., "Bayern Amtsgericht München HRB 12345")
            # Line 2: Company Name – City

            category = lines[0]
            court_info = lines[1] if len(lines) > 1 else ""
            company_info = lines[2] if len(lines) > 2 else ""

            # Extract registry number from court info
            registry_match = re.search(r"(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)", court_info)
            registry_type = registry_match.group(1) if registry_match else ""
            registry_number = registry_match.group(2) if registry_match else ""

            # Extract company name and city
            if "–" in company_info:
                parts = company_info.split("–")
                company_name = parts[0].strip()
                city = parts[1].strip() if len(parts) > 1 else ""
            else:
                company_name = company_info
                city = ""

            # Build native company number
            native_number = f"{registry_type} {registry_number}".strip()

            # Extract date with a chain of fallbacks — the portal has
            # historically changed this field several times.
            onclick = item.get("onclick", "")
            announcement_date = self._extract_announcement_date(
                onclick=onclick,
                text=text,
                fallback=date_from,
            )

            # Map category to our standard types
            announcement_type = self._classify_announcement_type(category)

            # Extract state from court_info (first word(s) before "Amtsgericht")
            state_match = re.match(r"^(.+?)\s+(?:Amtsgericht|District court)", court_info)
            state_from_court = state_match.group(1).strip() if state_match else None

            announcements.append(
                Announcement(
                    company_name=company_name,
                    native_company_number=native_number,
                    announcement_date=announcement_date,
                    announcement_type=announcement_type,
                    text=text,
                    city=city if city else None,
                    state=state_from_court,
                    registry_type=registry_type if registry_type else None,
                )
            )

        return announcements


def create_daily_scan_job(
    db: "Database",
    keywords: List[str],
    max_requests: int = 50,
) -> Dict[str, int]:
    """
    Run a daily scan for new companies.

    This function is designed to be called by a scheduler (e.g., cron)
    and stays well under the 60 req/hr limit.

    Args:
        db: Database instance
        keywords: List of keywords to search for
        max_requests: Maximum requests to use (default 50, leaving buffer)

    Returns:
        Statistics dict with new_companies, total_checked
    """
    from processing.filters import AIRoboticsFilter

    source = BundesAPISource()
    filter_ = AIRoboticsFilter()

    stats = {
        "new_companies": 0,
        "total_checked": 0,
        "requests_used": 0,
    }

    for keyword in keywords:
        if source.rate_limiter.requests_made >= max_requests:
            print(f"Reached max requests ({max_requests}), stopping")
            break

        print(f"Searching for: {keyword}")

        try:
            for result in source.search(
                keywords=[keyword],
                keyword_mode="all",  # Use 'all' mode - works without specifying registry type
                max_results=50,
            ):
                stats["total_checked"] += 1

                # Check if already in database
                existing = db.get_company_by_native_number(result.native_company_number)
                if existing:
                    continue

                # Apply AI/robotics filter
                filter_result = filter_.filter_company(
                    name=result.name,
                    status=result.status,
                )

                if filter_result.passes:
                    # Insert new company
                    company_id = db.insert_company(
                        company_number=f"bundesapi_{hash(result.native_company_number) & 0xFFFFFFFF:08x}",
                        name=result.name,
                        source="bundesapi",
                        native_company_number=result.native_company_number,
                        current_status=result.status,
                        registry_court=result.registry_court,
                        registry_type=result.registry_type,
                        state=result.state,
                        ai_robotics_score=filter_result.relevance_score,
                        matched_keywords=filter_result.matched_keywords,
                        tech_categories=filter_result.tech_categories,
                    )

                    # Add to enrichment queue
                    db.add_to_enrichment_queue(company_id, priority=1, reason="new_from_bundesapi")

                    stats["new_companies"] += 1
                    print(f"  New: {result.name} (score: {filter_result.relevance_score})")

        except Exception as e:
            print(f"Error searching for '{keyword}': {e}")
            continue

    stats["requests_used"] = source.rate_limiter.requests_made
    return stats
