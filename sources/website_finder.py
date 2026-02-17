"""
Website Finder - Discover and validate company websites.

Strategy:
1. Domain guessing: try predictable patterns ({name}.de, .com, .io)
2. DuckDuckGo search: fallback for companies where guessing fails
3. Validation: check HTTP status, title match, Impressum presence
4. Impressum deep validation: fetch /impressum, parse legal name + HRB number
   for definitive company match (confidence = 1.0)

No API keys required.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
REQUEST_TIMEOUT = 10

# Domains that appear in search results but are NOT company websites
FALSE_POSITIVE_DOMAINS = {
    # Social media
    'linkedin.com', 'xing.com', 'twitter.com', 'x.com',
    'facebook.com', 'instagram.com', 'tiktok.com',
    # Business directories / aggregators
    'crunchbase.com', 'pitchbook.com', 'northdata.com', 'northdata.de',
    'firmenwissen.de', 'unternehmensregister.de', 'handelsregister.de',
    'wer-zu-wem.de', 'kununu.de', 'glassdoor.com', 'glassdoor.de',
    'dnb.com', 'implisense.com', 'companyhouse.de',
    # News
    'gruenderszene.de', 'deutsche-startups.de', 't3n.de',
    'handelsblatt.com', 'manager-magazin.de', 'sifted.eu',
    'techcrunch.com', 'eu-startups.com',
    # Job boards
    'indeed.com', 'indeed.de', 'stepstone.de', 'jobs.de',
    # Generic
    'wikipedia.org', 'bloomberg.com', 'reuters.com',
    'youtube.com', 'github.com', 'medium.com',
    'apple.com', 'play.google.com',
}

# German legal form suffixes to strip from company names
LEGAL_FORMS = [
    'gmbh & co. kg', 'gmbh & co kg', 'gmbh & co. ohg',
    'ug (haftungsbeschränkt)', 'ug haftungsbeschränkt',
    'ggmbh', 'gmbh', 'ug', 'ag', 'se', 'kg', 'ohg', 'gbr', 'e.v.', 'e.v',
    'mbh', 'eg', 'partg', 'kgaa',
]

# Common suffixes that aren't part of the brand name
STRIP_SUFFIXES = [
    'bank', 'holding', 'group', 'gruppe', 'deutschland', 'germany',
    'europe', 'international', 'solutions', 'technologies', 'systems',
    'services',
]

# Parked domain indicators
PARKED_INDICATORS = [
    'domain steht zum verkauf', 'domain is for sale', 'buy this domain',
    'diese domain kaufen', 'domain parking', 'sedo.com', 'sedoparking',
    'hugedomains', 'afternic', 'dan.com', 'godaddy',
    'parked free', 'is available for purchase',
]


@dataclass
class ImpressumData:
    """Parsed data from a company's Impressum page."""
    legal_name: Optional[str] = None
    registry_number: Optional[str] = None  # e.g. 'HRB 187497'
    registry_court: Optional[str] = None  # e.g. 'Amtsgericht Charlottenburg'
    address: Optional[str] = None
    managing_directors: List[str] = field(default_factory=list)


@dataclass
class WebsiteResult:
    """Result of a website lookup."""
    url: str
    confidence: float  # 0.0 - 1.0
    source: str  # 'domain_guess', 'search'
    title_match: bool = False
    has_impressum: bool = False
    impressum_url: Optional[str] = None
    impressum_verified: bool = False
    impressum_data: Optional[ImpressumData] = None
    is_parked: bool = False


def normalize_company_name(name: str) -> str:
    """
    Strip legal forms and suffixes to get the brand name.

    'Trade Republic Bank GmbH' -> 'trade republic'
    'KI Solutions UG (haftungsbeschränkt)' -> 'ki solutions'
    """
    result = name.strip()

    # Strip legal forms (longest first to catch compound forms)
    lower = result.lower()
    for lf in LEGAL_FORMS:
        # Match at end or before punctuation
        pattern = r'\s*\b' + re.escape(lf) + r'\s*$'
        lower_new = re.sub(pattern, '', lower, flags=re.IGNORECASE)
        if lower_new != lower:
            result = result[:len(lower_new)].strip()
            lower = lower_new.strip()

    # Strip trailing common suffixes only if something remains
    for suffix in STRIP_SUFFIXES:
        if lower.endswith(' ' + suffix):
            candidate = result[:-(len(suffix) + 1)].strip()
            if len(candidate.split()) >= 1 and len(candidate) >= 3:
                result = candidate
                lower = result.lower()

    # Strip trailing punctuation
    result = result.strip(' .-,&')

    return result.lower().strip()


def generate_domain_candidates(name: str) -> List[str]:
    """
    Generate plausible domain names from a company name.

    'Trade Republic' -> ['traderepublic.de', 'trade-republic.de', 'traderepublic.com', ...]
    'one.five' -> ['onefive.de', 'one-five.de', 'onefive.com', ...]
    """
    brand = normalize_company_name(name)
    if not brand:
        return []

    candidates = []

    def _add(domain: str):
        # Skip absurdly long domains (label max is 63 chars per DNS spec)
        label = domain.split('.')[0]
        if len(label) > 40 or domain in candidates:
            return
        candidates.append(domain)

    tlds = ['.de', '.com', '.io', '.tech', '.ai']

    # Check if the brand name IS a domain (e.g. "mercury.ai", "disco.ai")
    known_tlds = {'.ai', '.io', '.de', '.com', '.tech', '.app', '.co', '.net', '.org'}
    if '.' in brand:
        last_dot = brand.rfind('.')
        suffix = brand[last_dot:]
        if suffix in known_tlds:
            # The name itself is a domain!
            _add(brand)

    # Handle dots in names (e.g. "one.five" -> "onefive", "one-five")
    if '.' in brand and not brand.endswith('.'):
        no_dots = brand.replace('.', '').replace(' ', '')
        dot_to_dash = brand.replace('.', '-').replace(' ', '-')
        for base in [no_dots, dot_to_dash]:
            for tld in tlds[:3]:  # .de, .com, .io
                _add(base + tld)

    words = brand.replace('.', ' ').split()

    # Join words without separator
    joined = ''.join(words)
    # Join words with hyphen
    hyphenated = '-'.join(words)

    for base in [joined, hyphenated]:
        for tld in tlds:
            _add(base + tld)

    # For single long words, try common prefixes/suffixes
    if len(words) == 1 and len(words[0]) >= 4:
        word = words[0]
        for tld in ['.de', '.com']:
            _add(f'get{word}{tld}')
            _add(f'{word}app{tld}')
            _add(f'{word}hq{tld}')

    return candidates


def _is_false_positive_domain(url: str) -> bool:
    """Check if URL belongs to a known non-company domain."""
    try:
        netloc = urlparse(url).netloc.lower().lstrip('www.')
        # Check exact match and parent domain
        for fp in FALSE_POSITIVE_DOMAINS:
            if netloc == fp or netloc.endswith('.' + fp):
                return True
    except Exception:
        pass
    return False


def _check_domain(domain: str) -> Optional[str]:
    """
    Test if a domain responds. Returns final URL after redirects, or None.
    """
    for scheme in ['https://', 'http://']:
        url = scheme + domain
        try:
            resp = requests.head(
                url,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
                headers={'User-Agent': USER_AGENT},
            )
            if resp.status_code < 400:
                final_url = resp.url
                # Reject if redirected to a false positive
                if _is_false_positive_domain(final_url):
                    return None
                return final_url
        except requests.RequestException:
            continue
    return None


def _validate_website(url: str, company_name: str) -> WebsiteResult:
    """
    Validate that a URL is the actual company website.

    Checks page title, Impressum link, parked domain indicators.
    Returns WebsiteResult with confidence score.
    """
    brand = normalize_company_name(company_name)
    confidence = 0.0
    title_match = False
    has_impressum = False
    is_parked = False

    # Domain contains company name words -> strong signal
    domain = urlparse(url).netloc.lower().lstrip('www.')
    domain_base = domain.split('.')[0]
    # Split brand on spaces, dots, hyphens to get individual words
    brand_words = set(re.split(r'[\s.\-]+', brand))
    domain_words = set(re.split(r'[-.]', domain_base))
    if brand_words & domain_words:
        confidence += 0.3
    elif any(w in domain_base for w in brand_words if len(w) >= 3):
        confidence += 0.2

    # Fetch page content
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={'User-Agent': USER_AGENT},
        )
        # Limit to 500KB decoded content
        content = resp.text[:512_000]
    except requests.RequestException:
        # URL responded to HEAD but GET failed — still possible website
        return WebsiteResult(
            url=url, confidence=max(confidence, 0.2),
            source='', title_match=False,
        )

    content_lower = content.lower()

    # Check for parked domain
    for indicator in PARKED_INDICATORS:
        if indicator in content_lower:
            is_parked = True
            confidence -= 0.5
            break

    if not is_parked:
        # Parse HTML
        soup = BeautifulSoup(content, 'lxml')

        # Title match
        title_tag = soup.find('title')
        if title_tag and title_tag.string:
            page_title = title_tag.string.strip()
            ratio = fuzz.token_set_ratio(brand, page_title.lower())
            if ratio >= 70:
                title_match = True
                confidence += 0.3
            elif ratio >= 50:
                confidence += 0.1

        # Meta tags
        for meta in soup.find_all('meta'):
            meta_content = (meta.get('content') or '').lower()
            meta_name = (meta.get('name') or meta.get('property') or '').lower()
            if meta_name in ('og:site_name', 'og:title', 'description'):
                if fuzz.token_set_ratio(brand, meta_content) >= 60:
                    confidence += 0.1
                    break

        # Impressum link (legally required for German companies)
        impressum_url = None
        for a in soup.find_all('a', href=True):
            link_text = (a.get_text() or '').lower()
            href = a['href'].lower()
            if 'impressum' in link_text or 'impressum' in href or 'imprint' in link_text or 'imprint' in href:
                has_impressum = True
                impressum_url = _resolve_impressum_url(url, a['href'])
                confidence += 0.2
                break

    return WebsiteResult(
        url=url,
        confidence=min(confidence, 1.0),
        source='',
        title_match=title_match,
        has_impressum=has_impressum,
        impressum_url=impressum_url if not is_parked else None,
        is_parked=is_parked,
    )


def _resolve_impressum_url(base_url: str, href: str) -> str:
    """Resolve an Impressum href to an absolute URL."""
    if href.startswith(('http://', 'https://')):
        return href
    return urljoin(base_url, href)


# Regex patterns for Impressum parsing
# Registry number: HRB 12345, HRA 6789
_RE_REGISTRY_NUMBER = re.compile(
    r'\b(HR[AB])\s*[\-:]?\s*(\d{3,7})\s*(?:\w)?',
    re.IGNORECASE,
)

# Registry court: Amtsgericht Berlin-Charlottenburg, AG München
# Captures the city name (e.g. "Charlottenburg", "Aachen", "München")
_RE_REGISTRY_COURT = re.compile(
    r'(?:'
    r'Registergericht[ \t]*:[ \t]*(?:\b(?:AG|Amtsgericht)\s+)?'     # "Registergericht: Amtsgericht X"
    r'|\bAmtsgericht\s+'                                              # "Amtsgericht X" (full word only)
    r'|\bDistrict\s+Court\s+(?:of\s+)?'                              # "District Court of X"
    r'|eingetragen\s+(?:im|beim)\s+Handelsregister\s+(?:des\s+)?\b(?:AG|Amtsgericht)\s+'  # "eingetragen im HR des AG X"
    r'|commercial\s+register\s+(?:of\s+)?(?:the\s+)?(?:District\s+Court\s+(?:of\s+)?)?' # "commercial register of the District Court of X"
    r')'
    r'([A-ZÄÖÜ][a-zäöüß]+(?:[\-][A-Za-zÄÖÜäöüß]+){0,2})',  # City name (hyphenated ok)
    re.IGNORECASE,
)

# German legal forms in Impressum - used to find the full legal name
# Uses [ \t] instead of \s to avoid matching across newlines
_RE_LEGAL_NAME = re.compile(
    r'([A-ZÄÖÜa-zäöüß][A-Za-zÄÖÜäöüß0-9\.\- \t&]{1,60}?'
    r'[ \t]+(?:GmbH[ \t]*&[ \t]*Co\.?[ \t]*KG|GmbH|UG[ \t]*\(?haftungsbeschränkt\)?|AG|SE|KG|OHG|e\.?[ \t]*V\.?|KGaA|eG|mbH))'
    r'(?:[ \t\n]|$|,|\.|<)',
)


def _fetch_and_parse_impressum(impressum_url: str) -> Optional[ImpressumData]:
    """
    Fetch an Impressum page and parse structured company data.

    German law (§5 TMG / §18 MStV) requires Impressum to contain:
    - Full legal company name
    - Address
    - Registry court and number (e.g. Amtsgericht Berlin, HRB 12345)
    - Managing director(s)
    """
    try:
        resp = requests.get(
            impressum_url,
            timeout=REQUEST_TIMEOUT,
            headers={'User-Agent': USER_AGENT},
        )
        if resp.status_code >= 400:
            return None
        content = resp.text[:512_000]
    except requests.RequestException:
        return None

    soup = BeautifulSoup(content, 'lxml')

    # Remove script/style elements
    for tag in soup.find_all(['script', 'style', 'nav', 'header', 'footer']):
        tag.decompose()

    text = soup.get_text(separator='\n')
    # Clean up whitespace
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text_block = '\n'.join(lines)

    data = ImpressumData()

    # Extract registry number (HRB/HRA)
    m = _RE_REGISTRY_NUMBER.search(text_block)
    if m:
        data.registry_number = f'{m.group(1).upper()} {m.group(2)}'

    # Extract registry court
    m = _RE_REGISTRY_COURT.search(text_block)
    if m:
        court = m.group(1).strip().rstrip('.,;:')
        data.registry_court = court

    # Extract legal name
    # Strategy: find lines containing a German legal form suffix
    # The Impressum typically has the legal name near the top
    m = _RE_LEGAL_NAME.search(text_block)
    if m:
        data.legal_name = m.group(1).strip()

    # Extract address (German postal code pattern: "Straße 5, 10997 Berlin")
    # Require at least one lowercase letter in street to avoid matching headers like "IMPRINT"
    addr_match = re.search(
        r'(?:^|\n)([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß\.\- ]*[a-zäöüß][A-Za-zÄÖÜäöüß\.\- ]*\d+[a-z]?)[ \t]*[,\n][ \t]*(\d{5})[ \t]+([A-Za-zÄÖÜäöüß][\w\s\-]+)',
        text_block,
    )
    if addr_match:
        street = addr_match.group(1).strip()
        plz = addr_match.group(2)
        city = addr_match.group(3).strip()
        data.address = f'{street}, {plz} {city}'

    # Extract managing directors (Geschäftsführer)
    gf_match = re.search(
        r'(?:Geschäftsführe(?:r|rin)|Managing\s+Director|Vertretungsberechtig(?:t|te)r?|'
        r'Vorstand|CEO|Vertreten\s+durch)\s*[:\s]+(.+?)(?:\n\n|\n[A-Z]|\Z)',
        text_block,
        re.IGNORECASE | re.DOTALL,
    )
    if gf_match:
        gf_text = gf_match.group(1).strip()
        # Split on common separators: comma, newline, "und", "and"
        names = re.split(r'\s*[,\n]\s*|\s+und\s+|\s+and\s+', gf_text)
        data.managing_directors = [
            n.strip() for n in names
            if n.strip() and len(n.strip()) > 2 and not n.strip().startswith(('Tel', 'Fax', 'E-', 'USt'))
        ][:5]  # Cap at 5

    # Only return if we found at least one useful field
    if data.legal_name or data.registry_number:
        return data

    return None


def _match_impressum_to_company(
    impressum: ImpressumData,
    company_name: str,
    native_company_number: Optional[str] = None,
    registry_court: Optional[str] = None,
) -> float:
    """
    Match Impressum data against a company record.

    Returns a confidence boost (0.0 to 1.0):
    - Registry number match: 1.0 (definitive proof)
    - Legal name exact match: 0.8
    - Legal name fuzzy match + court match: 0.7
    - Legal name fuzzy match alone: 0.4
    """
    boost = 0.0

    # Match 1: Registry number (definitive — HRB numbers are unique per court)
    if impressum.registry_number and native_company_number:
        # Normalize both: strip spaces, uppercase
        imp_num = re.sub(r'\s+', ' ', impressum.registry_number.upper().strip())
        db_num = re.sub(r'\s+', ' ', native_company_number.upper().strip())
        # Also try without the prefix (some DBs store just the number)
        imp_digits = re.search(r'\d+', imp_num)
        db_digits = re.search(r'\d+', db_num)

        if imp_num == db_num:
            return 1.0  # Exact HRB match — definitive
        elif imp_digits and db_digits and imp_digits.group() == db_digits.group():
            # Same number, check if type prefix matches too
            imp_type = re.match(r'(HR[AB])', imp_num)
            db_type = re.match(r'(HR[AB])', db_num)
            if imp_type and db_type and imp_type.group() == db_type.group():
                return 1.0
            # Same digits, might be same company
            boost = max(boost, 0.6)

    # Match 2: Legal name from Impressum vs company name
    if impressum.legal_name:
        # Compare full legal names
        ratio = fuzz.token_set_ratio(
            company_name.lower(),
            impressum.legal_name.lower(),
        )
        if ratio >= 90:
            boost = max(boost, 0.8)  # Near-exact legal name match
        elif ratio >= 75:
            boost = max(boost, 0.5)
        elif ratio >= 60:
            boost = max(boost, 0.3)

        # Also compare normalized brand names
        brand = normalize_company_name(company_name)
        imp_brand = normalize_company_name(impressum.legal_name)
        brand_ratio = fuzz.token_set_ratio(brand, imp_brand)
        if brand_ratio >= 85:
            boost = max(boost, 0.6)

    # Match 3: Registry court match (supplementary signal)
    if impressum.registry_court and registry_court:
        court_ratio = fuzz.token_set_ratio(
            registry_court.lower(),
            impressum.registry_court.lower(),
        )
        if court_ratio >= 70:
            boost = min(boost + 0.15, 1.0)

    return boost


def _search_duckduckgo(company_name: str, max_results: int = 5) -> List[Tuple[str, str]]:
    """
    Search DuckDuckGo for a company website.

    Returns list of (url, title) tuples, filtered for false positives.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            logger.warning("ddgs/duckduckgo-search not installed, skipping web search")
            return []

    results = []
    queries = [
        f'{company_name} offizielle Webseite',
        f'{company_name} official website',
    ]

    for query in queries:
        try:
            ddg_results = DDGS().text(query, region='de-de', max_results=max_results)
            for r in ddg_results:
                url = r.get('href', '')
                title = r.get('title', '')
                if url and not _is_false_positive_domain(url):
                    results.append((url, title))
            if results:
                break  # First query gave results
        except Exception as e:
            logger.debug("DDG search failed for '%s': %s", query, e)
            time.sleep(5)

    return results


class WebsiteFinder:
    """
    Find and validate company websites.

    Combines domain guessing (fast, free) with DuckDuckGo search (fallback),
    then validates candidates for company name match and Impressum presence.
    """

    def __init__(
        self,
        min_confidence: float = 0.4,
        enable_search: bool = True,
        search_delay: float = 3.0,
    ):
        """
        Args:
            min_confidence: Minimum confidence to accept a result
            enable_search: Whether to use DuckDuckGo search as fallback
            search_delay: Seconds between DDG searches
        """
        self.min_confidence = min_confidence
        self.enable_search = enable_search
        self.search_delay = search_delay
        self._last_search_time = 0.0

    def find(
        self,
        company_name: str,
        native_company_number: Optional[str] = None,
        registry_court: Optional[str] = None,
    ) -> Optional[WebsiteResult]:
        """
        Find the website for a company.

        Tries domain guessing first, then DuckDuckGo search, then
        verifies via Impressum deep validation for definitive matching.

        Args:
            company_name: Full legal company name
            native_company_number: HRB/HRA number from DB (e.g. 'HRB 12345')
            registry_court: Registry court from DB (e.g. 'Amtsgericht Berlin')

        Returns:
            WebsiteResult or None if no confident match found
        """
        if not company_name or len(company_name) < 2:
            return None

        best = None

        # Phase 1: Domain guessing (fast, no external API)
        candidates = generate_domain_candidates(company_name)
        for domain in candidates:
            url = _check_domain(domain)
            if url:
                result = _validate_website(url, company_name)
                result.source = 'domain_guess'
                # Domain guess that responds + validates is high confidence
                result.confidence += 0.2
                result.confidence = min(result.confidence, 1.0)

                if result.confidence >= 0.7:
                    # High confidence — try Impressum verification
                    logger.debug("Domain guess hit: %s -> %s (conf=%.2f)",
                                 company_name, url, result.confidence)
                    best = result
                    break

                if not best or result.confidence > best.confidence:
                    best = result

        # Early return if domain guessing found something decent
        if best and best.confidence >= self.min_confidence and not self.enable_search:
            # Still try Impressum before returning
            best = self._try_impressum_verification(
                best, company_name, native_company_number, registry_court,
            )
            return best

        # Phase 2: DuckDuckGo search (slower, rate-limited)
        if self.enable_search and (not best or best.confidence < 0.7):
            # Respect delay between searches
            elapsed = time.time() - self._last_search_time
            if elapsed < self.search_delay:
                time.sleep(self.search_delay - elapsed)

            search_results = _search_duckduckgo(company_name)
            self._last_search_time = time.time()

            for url, title in search_results[:3]:
                result = _validate_website(url, company_name)
                result.source = 'search'

                if not best or result.confidence > best.confidence:
                    best = result

                if best and best.confidence >= 0.7:
                    break

        # Phase 3: Impressum deep validation
        if best and best.confidence >= self.min_confidence:
            best = self._try_impressum_verification(
                best, company_name, native_company_number, registry_court,
            )
            logger.info("Found website: %s -> %s (conf=%.2f, src=%s, impressum=%s)",
                        company_name, best.url, best.confidence, best.source,
                        'verified' if best.impressum_verified else 'no')
            return best

        logger.debug("No website found for: %s", company_name)
        return None

    def _try_impressum_verification(
        self,
        result: WebsiteResult,
        company_name: str,
        native_company_number: Optional[str],
        registry_court: Optional[str],
    ) -> WebsiteResult:
        """
        Attempt Impressum deep validation on a candidate website.

        Fetches the Impressum page, parses legal data, and matches
        against the company record. Can boost confidence up to 1.0.
        """
        # Need an Impressum URL to verify
        impressum_url = result.impressum_url

        # If we didn't find an Impressum link, try common paths
        if not impressum_url and not result.is_parked:
            base = result.url.rstrip('/')
            for path in ['/impressum', '/imprint', '/legal/impressum']:
                test_url = base + path
                try:
                    resp = requests.head(
                        test_url,
                        timeout=REQUEST_TIMEOUT,
                        headers={'User-Agent': USER_AGENT},
                        allow_redirects=True,
                    )
                    if resp.status_code < 400:
                        impressum_url = resp.url
                        result.has_impressum = True
                        result.impressum_url = impressum_url
                        break
                except requests.RequestException:
                    continue

        if not impressum_url:
            return result

        # Fetch and parse Impressum
        impressum_data = _fetch_and_parse_impressum(impressum_url)
        if not impressum_data:
            return result

        result.impressum_data = impressum_data

        # Match against company record
        boost = _match_impressum_to_company(
            impressum_data, company_name, native_company_number, registry_court,
        )

        if boost > 0:
            old_conf = result.confidence
            result.confidence = min(result.confidence + boost, 1.0)
            if boost >= 0.8:
                result.impressum_verified = True
            logger.debug(
                "Impressum verification: %s conf %.2f -> %.2f (boost=%.2f, "
                "legal_name=%s, reg=%s, court=%s)",
                company_name, old_conf, result.confidence, boost,
                impressum_data.legal_name, impressum_data.registry_number,
                impressum_data.registry_court,
            )

        return result
