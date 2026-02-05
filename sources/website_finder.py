"""
Website Finder - Discover and validate company websites.

Strategy:
1. Domain guessing: try predictable patterns ({name}.de, .com, .io)
2. DuckDuckGo search: fallback for companies where guessing fails
3. Validation: check HTTP status, title match, Impressum presence

No API keys required.
"""

import re
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlparse

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
    'gmbh', 'ug', 'ag', 'se', 'kg', 'ohg', 'gbr', 'e.v.', 'e.v',
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
class WebsiteResult:
    """Result of a website lookup."""
    url: str
    confidence: float  # 0.0 - 1.0
    source: str  # 'domain_guess', 'search'
    title_match: bool = False
    has_impressum: bool = False
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
        if domain not in candidates:
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
            stream=True,
        )
        # Read max 500KB
        content = resp.raw.read(512_000).decode('utf-8', errors='replace')
        resp.close()
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
        for a in soup.find_all('a', href=True):
            link_text = (a.get_text() or '').lower()
            href = a['href'].lower()
            if 'impressum' in link_text or 'impressum' in href or 'imprint' in link_text or 'imprint' in href:
                has_impressum = True
                confidence += 0.2
                break

    return WebsiteResult(
        url=url,
        confidence=min(confidence, 1.0),
        source='',
        title_match=title_match,
        has_impressum=has_impressum,
        is_parked=is_parked,
    )


def _search_duckduckgo(company_name: str, max_results: int = 5) -> List[Tuple[str, str]]:
    """
    Search DuckDuckGo for a company website.

    Returns list of (url, title) tuples, filtered for false positives.
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logger.warning("duckduckgo-search not installed, skipping web search")
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

    def find(self, company_name: str) -> Optional[WebsiteResult]:
        """
        Find the website for a company.

        Tries domain guessing first, then DuckDuckGo search.
        Validates all candidates and returns the best one above
        the confidence threshold.

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
                    # High confidence — return immediately
                    logger.debug("Domain guess hit: %s -> %s (conf=%.2f)",
                                 company_name, url, result.confidence)
                    return result

                if not best or result.confidence > best.confidence:
                    best = result

        # Early return if domain guessing found something decent
        if best and best.confidence >= self.min_confidence and not self.enable_search:
            return best

        # Phase 2: DuckDuckGo search (slower, rate-limited)
        if self.enable_search:
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

        # Return best if above threshold
        if best and best.confidence >= self.min_confidence:
            logger.info("Found website: %s -> %s (conf=%.2f, src=%s)",
                        company_name, best.url, best.confidence, best.source)
            return best

        logger.debug("No website found for: %s", company_name)
        return None
