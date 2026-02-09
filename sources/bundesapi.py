"""
BundesAPI Handelsregister scraper.

Queries the official German Handelsregister portal (handelsregister.de)
with strict rate limiting to comply with legal requirements.

IMPORTANT: The official portal has a 60 requests/hour limit.
Exceeding this limit can violate German criminal law (§303a, b StGB).

Based on the approach from: https://github.com/bundesAPI/handelsregister

Form field names (verified 2026-02-02):
- Keywords: form:schlagwoerter (textarea)
- Keyword mode: form:schlagwortOptionen (radio: 1=all, 2=at least one, 3=exact)
- Register type: form:registerArt_input (select: "", HRA, HRB, GnR, PR, VR, GsR)
- State checkboxes: form:{StateName}_input (e.g., form:Bayern_input)
- Submit: form:btnSuche
"""

import time
import re
from datetime import datetime
from typing import Iterator, Optional, List, Dict, Any
from dataclasses import dataclass
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup


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

            self.tokens = min(
                float(self.rate),
                self.tokens + elapsed * (self.rate / self.per_seconds)
            )
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
        return min(
            float(self.rate),
            self.tokens + elapsed * (self.rate / self.per_seconds)
        )


@dataclass
class SearchResult:
    """A single search result from Handelsregister."""
    name: str
    native_company_number: str
    registry_court: str
    registry_type: str
    status: Optional[str]
    state: Optional[str]
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
        'bw': 'Baden-Württemberg',
        'by': 'Bayern',
        'be': 'Berlin',
        'bb': 'Brandenburg',
        'hb': 'Bremen',
        'hh': 'Hamburg',
        'he': 'Hessen',
        'mv': 'Mecklenburg-Vorpommern',
        'ni': 'Niedersachsen',
        'nw': 'Nordrhein-Westfalen',
        'rp': 'Rheinland-Pfalz',
        'sl': 'Saarland',
        'sn': 'Sachsen',
        'st': 'Sachsen-Anhalt',
        'sh': 'Schleswig-Holstein',
        'th': 'Thüringen',
    }

    # Valid register types
    REGISTER_TYPES = ['HRA', 'HRB', 'GnR', 'PR', 'VR', 'GsR']

    def __init__(self, config: Optional[BundesAPIConfig] = None):
        self.config = config or BundesAPIConfig()
        self.rate_limiter = TokenBucketRateLimiter(
            rate=self.config.requests_per_hour,
            per_seconds=3600
        )
        self._session = None
        self._initialized = False

    @property
    def session(self) -> requests.Session:
        """Lazy-initialize requests session with browser-like headers."""
        if self._session is None:
            self._session = requests.Session()
            # Use comprehensive browser-like headers to avoid being blocked
            self._session.headers.update({
                'User-Agent': self.config.user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'same-origin',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
            })
        return self._session

    def reset_session(self):
        """Reset the session (useful after errors)."""
        if self._session:
            self._session.close()
        self._session = None
        self._initialized = False
        self._last_response = None

    def _make_request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Make a request with retry logic."""
        kwargs.setdefault('timeout', self.config.timeout)

        for attempt in range(self.config.max_retries):
            try:
                if method.lower() == 'get':
                    response = self.session.get(url, **kwargs)
                else:
                    response = self.session.post(url, **kwargs)
                response.raise_for_status()
                return response
            except (requests.RequestException, ConnectionError) as e:
                if attempt < self.config.max_retries - 1:
                    print(f"Request failed (attempt {attempt + 1}), retrying in {self.config.retry_delay}s...")
                    time.sleep(self.config.retry_delay)
                else:
                    raise

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
            response = self._make_request('get', advanced_search_url)

            # Verify we're on the advanced search page by checking for the keywords field
            if 'schlagwoerter' in response.text.lower() or 'schlagwort' in response.text.lower():
                self._initialized = True
                self._last_response = response
                print("Successfully reached advanced search page")
                return True
            else:
                print(f"Did not reach advanced search page. URL: {response.url}")
                # Try to diagnose the issue
                if 'session' in response.text.lower() and 'abgelaufen' in response.text.lower():
                    print("Session expired message detected")
                return False

        except requests.RequestException as e:
            print(f"Error initializing session: {e}")
            self.reset_session()
            return False

    def search(
        self,
        keywords: List[str],
        keyword_mode: str = 'all',  # Changed default to 'all' - more reliable
        states: Optional[List[str]] = None,
        registry_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        max_results: int = 100,
        shareholder_name: Optional[str] = None,  # NEW: Search by shareholder/participant name
    ) -> Iterator[SearchResult]:
        """
        Search for companies matching criteria.

        Args:
            keywords: Search terms for company name
            keyword_mode: 'all' (contain all keywords), 'min' (at least one), or 'exact'
            states: List of state codes (e.g., ['by', 'be']) - maps to full state names
            registry_types: List of registry types (HRA, HRB, GnR, PR, VR, GsR)
            include_deleted: Include deleted/dissolved companies
            max_results: Maximum results to return
            shareholder_name: Search by shareholder/participant name (Name des Beteiligten)

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
        mode_map = {'all': '1', 'min': '2', 'exact': '3'}
        keyword_mode_value = mode_map.get(keyword_mode, '1')

        # Get the search form from the last response
        soup = BeautifulSoup(self._last_response.text, 'lxml')

        # Find the search form
        search_form = soup.find('form', {'name': 'form'}) or soup.find('form', id='form')
        if not search_form:
            print("Could not find search form on page")
            # Debug: print what forms are available
            forms = soup.find_all('form')
            print(f"Available forms: {[f.get('name', f.get('id', 'unnamed')) for f in forms]}")
            return

        # Get form action
        form_action = search_form.get('action', '')
        form_url = urljoin(self.config.base_url, form_action) if form_action else self._last_response.url

        # Get ViewState (required for JSF forms)
        viewstate = soup.find('input', {'name': 'javax.faces.ViewState'})
        viewstate_value = viewstate.get('value', '') if viewstate else ''

        if not viewstate_value:
            print("Warning: No ViewState found - form submission may fail")

        # Build search form data with verified field names
        form_data = {
            'form': 'form',
            'suchTyp': 'e',  # Extended search type
            'form:schlagwoerter': ' '.join(keywords) if keywords else '',
            'form:schlagwortOptionen': keyword_mode_value,
            'javax.faces.ViewState': viewstate_value,
            'form:btnSuche': '',  # Submit button (empty value triggers the button)
        }

        # Add shareholder/participant name search if specified
        # This searches the "Name des Beteiligten" field
        if shareholder_name:
            form_data['form:beteiligter'] = shareholder_name

        # Registry type - use select dropdown value
        # For 'all' mode, we can leave it empty (all types)
        # For 'min' mode, a specific type may be required
        if registry_types:
            # Single value for select dropdown
            form_data['form:registerArt_input'] = registry_types[0] if len(registry_types) == 1 else ''
        # If no registry_types specified, leave empty for "all"

        # Add state checkboxes if specified
        # The form uses full state names: form:{StateName}_input
        if states:
            for state_code in states:
                state_name = self.STATES.get(state_code.lower())
                if state_name:
                    form_data[f'form:{state_name}_input'] = 'on'

        # Include deleted companies checkbox
        if include_deleted:
            # Find the actual field name for this option
            deleted_checkbox = soup.find('input', {'type': 'checkbox', 'id': lambda x: x and 'geloescht' in x.lower()})
            if deleted_checkbox:
                form_data[deleted_checkbox.get('name', 'form:geloescht')] = 'on'

        # Acquire rate limit token for search
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        try:
            print(f"Submitting search for: {' '.join(keywords)}")

            # Submit search with proper headers for form submission
            response = self._make_request(
                'post',
                form_url,
                data=form_data,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': self.config.base_url,
                    'Referer': self._last_response.url,
                }
            )

            # Check if we got results or an error
            if 'sucheErgebnisse' in response.url or 'Search Result' in response.text:
                print(f"Search results page reached: {response.url}")
                # Store search results page for VÖ fetching
                self._search_results_response = response
            elif 'erweitertesuche' in response.url:
                # Still on search page - might be a validation error
                if 'error' in response.text.lower() or 'fehler' in response.text.lower():
                    print("Form validation error detected")
                    # Try to extract error message
                    error_soup = BeautifulSoup(response.text, 'lxml')
                    error_msgs = error_soup.find_all(class_=lambda c: c and 'error' in c.lower())
                    for msg in error_msgs[:3]:
                        print(f"  Error: {msg.get_text(strip=True)[:100]}")

            # Parse results (now includes row_index for VÖ fetching)
            results = self._parse_search_results(response.text)
            print(f"Found {len(results)} results")

            for i, result in enumerate(results):
                if i >= max_results:
                    break
                yield result

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
        soup = BeautifulSoup(html, 'lxml')
        results = []

        # Find result table (uses role='grid' attribute)
        table = soup.find('table', {'role': 'grid'})
        if not table:
            # Try alternative: look for data table
            table = soup.find('table', class_=lambda c: c and 'dataTable' in c)

        if not table:
            # Check if there's an error or no results message
            no_results = soup.find(string=re.compile(r'keine.*(treffer|ergebnis)|no.*result', re.IGNORECASE))
            if no_results:
                print("No results found on page")
            return results

        # Find all data rows with data-ri attribute (these are the company rows)
        tbody = table.find('tbody')
        if tbody:
            rows = tbody.find_all('tr', {'data-ri': True})
        else:
            rows = table.find_all('tr', {'data-ri': True})

        for row in rows:
            cells = row.find_all('td')
            if len(cells) < 5:
                continue

            try:
                # Get row index from data-ri attribute (for VÖ fetching)
                row_index = int(row.get('data-ri', -1))

                # Extract data from specific cell positions
                # cells[1]: Court/registry info
                # cells[2]: Company name
                # cells[3]: City
                # cells[4]: Status

                court_info = cells[1].get_text(strip=True) if len(cells) > 1 else ''
                name = cells[2].get_text(strip=True) if len(cells) > 2 else ''
                city = cells[3].get_text(strip=True) if len(cells) > 3 else ''
                status = cells[4].get_text(strip=True) if len(cells) > 4 else ''

                # Skip if no company name
                if not name:
                    continue

                # Parse court info to extract state, court, register type, and number
                # Format: "Bavaria   District court Augsburg HRB 19414"
                # or German: "Bayern   Amtsgericht München HRB 123456"
                state = ''
                registry_court = ''
                registry_type = ''
                register_number = ''

                # English pattern
                match = re.match(
                    r'([\w\-\s]+?)\s+District court\s+([\w\-\s]+?)\s+(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)',
                    court_info, re.IGNORECASE
                )
                if not match:
                    # German pattern
                    match = re.match(
                        r'([\w\-\s]+?)\s+Amtsgericht\s+([\w\-\s]+?)\s+(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)',
                        court_info, re.IGNORECASE
                    )

                if match:
                    state = match.group(1).strip()
                    court_city = match.group(2).strip()
                    registry_type = match.group(3).upper()
                    register_number = match.group(4)
                    registry_court = f"District court {court_city}"
                else:
                    # Fallback: try to extract at least registry type and number
                    type_match = re.search(r'(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)', court_info, re.IGNORECASE)
                    if type_match:
                        registry_type = type_match.group(1).upper()
                        register_number = type_match.group(2)

                # Build native company number
                native_company_number = f"{registry_court} {registry_type} {register_number}".strip()
                if not native_company_number or native_company_number == '  ':
                    native_company_number = court_info  # Fallback to full court info

                # Normalize status
                normalized_status = status
                if status:
                    status_lower = status.lower()
                    if 'aktuell' in status_lower or 'currently' in status_lower or 'registered' in status_lower:
                        normalized_status = 'currently registered'
                    elif 'gelöscht' in status_lower or 'deleted' in status_lower:
                        normalized_status = 'deleted'
                    elif 'aufgelöst' in status_lower or 'dissolved' in status_lower:
                        normalized_status = 'dissolved'

                results.append(SearchResult(
                    name=name,
                    native_company_number=native_company_number,
                    registry_court=registry_court,
                    registry_type=registry_type,
                    status=normalized_status if normalized_status else None,
                    state=state if state else None,  # Don't use city as state fallback
                    row_index=row_index,
                ))

            except Exception as e:
                print(f"Error parsing row: {e}")
                continue

        return results

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """Get current rate limit status."""
        return {
            'requests_made': self.rate_limiter.requests_made,
            'tokens_available': self.rate_limiter.tokens_available,
            'max_per_hour': self.config.requests_per_hour,
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
        if not hasattr(self, '_search_results_response') or not self._search_results_response:
            print("No search results page available. Run search() first.")
            return []

        if search_result.row_index is None or search_result.row_index < 0:
            print("SearchResult has no valid row_index")
            return []

        # Parse the search results page
        soup = BeautifulSoup(self._search_results_response.text, 'lxml')
        viewstate = soup.find('input', {'name': 'javax.faces.ViewState'})
        viewstate_value = viewstate.get('value', '') if viewstate else ''

        # Find the VÖ link for this row
        # Pattern: ergebnissForm:selectedSuchErgebnisFormTable:{row}:j_idt227:5:fade1_
        # The j_idt number can vary, so use regex
        row_idx = search_result.row_index
        vo_link = soup.find('a', id=re.compile(
            f'ergebnissForm:selectedSuchErgebnisFormTable:{row_idx}:j_idt\\d+:5:fade'
        ))

        if not vo_link:
            # Try alternate pattern without trailing underscore
            vo_link = soup.find('a', id=re.compile(
                f'ergebnissForm:selectedSuchErgebnisFormTable:{row_idx}:.*:5:'
            ))

        if not vo_link:
            print(f"No VÖ link found for row {row_idx}")
            return []

        link_id = vo_link.get('id')

        # Build form data to click the VÖ button
        form_data = {
            'ergebnissForm': 'ergebnissForm',
            'property2': '',
            'property': 'Global.Dokumentart.VÖ',
            link_id: link_id,
            'javax.faces.ViewState': viewstate_value,
        }

        try:
            # Acquire rate limit token
            if not self.rate_limiter.acquire(timeout=30):
                print("Rate limit timeout for VÖ request")
                return []

            # Submit the VÖ request
            response = self._make_request(
                'post',
                self._search_results_response.url,
                data=form_data,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': self.config.base_url,
                    'Referer': self._search_results_response.url,
                }
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
        soup = BeautifulSoup(html, 'lxml')
        announcements = []

        # Find all list items
        list_items = soup.find_all('li', class_='ui-datalist-item')

        if not list_items:
            # Check for empty message
            empty_msg = soup.find('div', class_='ui-datalist-empty-message')
            if empty_msg:
                return []  # No announcements

        for item in list_items:
            text = item.get_text(separator='\n', strip=True)

            # Try to extract date from text
            # Common patterns: "12.03.2024", "2024-03-12"
            date_match = re.search(r'(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})', text)
            announcement_date = date_match.group(1) if date_match else None

            # Try to determine announcement type
            announcement_type = self._classify_announcement_type(text)

            # Try to extract capital amounts
            capital_old, capital_new = self._extract_capital_amounts(text)

            announcements.append(Announcement(
                company_name=company_name,
                native_company_number=native_company_number,
                announcement_date=announcement_date,
                announcement_type=announcement_type,
                text=text,
                capital_old=capital_old,
                capital_new=capital_new,
            ))

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
        if any(kw in text_lower for kw in ['neueintragung', 'erstmalige eintragung', 'neue firma', 'ist eingetragen']):
            return 'neueintragung'

        if any(kw in text_lower for kw in ['kapitalerhöhung', 'erhöhung des stammkapitals', 'erhöhung des grundkapitals',
                                            'kapital erhöht', 'capital increase']):
            return 'kapitalerhoehung'

        if any(kw in text_lower for kw in ['kapitalherabsetzung', 'herabsetzung des stammkapitals',
                                            'kapital herabgesetzt', 'capital decrease']):
            return 'kapitalherabsetzung'

        if any(kw in text_lower for kw in ['geschäftsführer', 'geschäftsführerin', 'managing director',
                                            'bestellt', 'abberufen', 'nicht mehr geschäftsführer']):
            return 'geschaeftsfuehrer'

        if any(kw in text_lower for kw in ['sitzverlegung', 'sitz verlegt', 'neuer sitz', 'registered office changed']):
            return 'sitzverlegung'

        if any(kw in text_lower for kw in ['umwandlung', 'verschmelzung', 'spaltung', 'formwechsel',
                                            'merger', 'transformation']):
            return 'umwandlung'

        if any(kw in text_lower for kw in ['auflösung', 'liquidation', 'dissolution', 'aufgelöst']):
            return 'aufloesung'

        if any(kw in text_lower for kw in ['löschung', 'gelöscht', 'deletion', 'von amts wegen gelöscht']):
            return 'loeschung'

        if any(kw in text_lower for kw in ['prokura', 'prokurist', 'power of attorney']):
            return 'prokura'

        return 'sonstiges'

    def _extract_capital_amounts(self, text: str) -> tuple:
        """
        Extract old and new capital amounts from announcement text.

        Returns:
            (capital_old, capital_new) tuple, values are float or None
        """
        # Common patterns for German capital amounts:
        # "25.000,00 EUR" or "25.000 EUR" or "EUR 25.000,00"
        # "Stammkapital: 25.000,00 EUR"

        capital_pattern = r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:EUR|€)'

        # Find all capital amounts
        matches = re.findall(capital_pattern, text)

        if not matches:
            return (None, None)

        # Convert to float
        def to_float(s):
            # "25.000,00" -> 25000.00
            return float(s.replace('.', '').replace(',', '.'))

        amounts = [to_float(m) for m in matches]

        # If two amounts, likely old -> new
        if len(amounts) >= 2:
            return (amounts[0], amounts[1])
        elif len(amounts) == 1:
            # Single amount - probably the new capital
            return (None, amounts[0])

        return (None, None)

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
            response = self._make_request('get', bk_url)
        except requests.RequestException as e:
            print(f"Error navigating to Registerbekanntmachungen: {e}")
            return

        soup = BeautifulSoup(response.text, 'lxml')
        viewstate = soup.find('input', {'name': 'javax.faces.ViewState'})
        viewstate_value = viewstate.get('value', '') if viewstate else ''

        # Map state code to state name
        state_name = ''
        if state:
            state_name = self.STATES.get(state.lower(), '')

        # Build form data
        form_data = {
            'bekanntMachungenForm': 'bekanntMachungenForm',
            'bekanntMachungenForm:datum_von_input': date_from,
            'bekanntMachungenForm:datum_bis_input': date_to,
            'bekanntMachungenForm:land_input': state_name,
            'bekanntMachungenForm:registergericht_input': '',
            'bekanntMachungenForm:sitz': '',
            'bekanntMachungenForm:kategorie_input': category or '',
            'javax.faces.ViewState': viewstate_value,
            'bekanntMachungenForm:rrbSuche': '',
        }

        # Acquire rate limit token for search
        if not self.rate_limiter.acquire():
            print("Rate limit timeout")
            return

        try:
            print(f"Searching announcements from {date_from} to {date_to}...")

            search_response = self._make_request(
                'post',
                response.url,
                data=form_data,
                headers={
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': self.config.base_url,
                    'Referer': response.url,
                }
            )

            # Parse results
            announcements = self._parse_bekanntmachungen_results(search_response.text)
            print(f"Found {len(announcements)} announcements")

            for i, ann in enumerate(announcements):
                if i >= max_results:
                    break
                yield ann

        except requests.RequestException as e:
            print(f"Announcement search error: {e}")
            return

    def _parse_bekanntmachungen_results(self, html: str) -> List[Announcement]:
        """
        Parse announcements from the Registerbekanntmachungen results page.

        Each announcement is a link (a.ui-commandlink) containing a label with:
        - Category (e.g., "Löschungsankündigung")
        - State and court (e.g., "Bayern Amtsgericht München HRB 12345")
        - Company name and city
        """
        soup = BeautifulSoup(html, 'lxml')
        announcements = []

        # Find datalist container
        datalist = soup.find('div', class_='ui-datalist')
        if not datalist:
            return announcements

        # Find all announcement links
        items = datalist.find_all('a', class_='ui-commandlink')

        for item in items:
            label = item.find('label')
            if not label:
                continue

            text = label.get_text(separator='\n', strip=True)
            lines = [l.strip() for l in text.split('\n') if l.strip()]

            if len(lines) < 3:
                continue

            # Parse the announcement
            # Line 0: Category (e.g., "Löschungsankündigung")
            # Line 1: State Court RegistryType Number (e.g., "Bayern Amtsgericht München HRB 12345")
            # Line 2: Company Name – City

            category = lines[0]
            court_info = lines[1] if len(lines) > 1 else ''
            company_info = lines[2] if len(lines) > 2 else ''

            # Extract registry number from court info
            registry_match = re.search(r'(HRB|HRA|GnR|PR|VR|GsR)\s*(\d+)', court_info)
            registry_type = registry_match.group(1) if registry_match else ''
            registry_number = registry_match.group(2) if registry_match else ''

            # Extract company name and city
            if '–' in company_info:
                parts = company_info.split('–')
                company_name = parts[0].strip()
                city = parts[1].strip() if len(parts) > 1 else ''
            else:
                company_name = company_info
                city = ''

            # Build native company number
            native_number = f"{registry_type} {registry_number}".strip()

            # Extract date from onclick if available
            onclick = item.get('onclick', '')
            date_match = re.search(r"'(\w+ \w+ \d+ \d+:\d+:\d+ \w+ \d+)'", onclick)
            announcement_date = None
            if date_match:
                # Parse date like "Mon Feb 02 00:00:00 CET 2026"
                try:
                    dt = datetime.strptime(date_match.group(1), "%a %b %d %H:%M:%S %Z %Y")
                    announcement_date = dt.strftime("%d.%m.%Y")
                except:
                    pass

            # Map category to our standard types
            announcement_type = self._classify_announcement_type(category)

            announcements.append(Announcement(
                company_name=company_name,
                native_company_number=native_number,
                announcement_date=announcement_date,
                announcement_type=announcement_type,
                text=text,
            ))

        return announcements


def create_daily_scan_job(
    db: 'Database',
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
        'new_companies': 0,
        'total_checked': 0,
        'requests_used': 0,
    }

    for keyword in keywords:
        if source.rate_limiter.requests_made >= max_requests:
            print(f"Reached max requests ({max_requests}), stopping")
            break

        print(f"Searching for: {keyword}")

        try:
            for result in source.search(
                keywords=[keyword],
                keyword_mode='all',  # Use 'all' mode - works without specifying registry type
                max_results=50,
            ):
                stats['total_checked'] += 1

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
                        source='bundesapi',
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
                    db.add_to_enrichment_queue(company_id, priority=1, reason='new_from_bundesapi')

                    stats['new_companies'] += 1
                    print(f"  New: {result.name} (score: {filter_result.relevance_score})")

        except Exception as e:
            print(f"Error searching for '{keyword}': {e}")
            continue

    stats['requests_used'] = source.rate_limiter.requests_made
    return stats
