"""
Officer LinkedIn search and matching.

Searches for officer LinkedIn profiles using search engines and
extracts career data from search snippets (no direct LinkedIn scraping).

Uses the snippet-first approach: extract info from DuckDuckGo/Brave
search result titles and snippets to avoid hitting linkedin.com directly
(which aggressively blocks cloud IPs).
"""

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Optional

logger = logging.getLogger(__name__)


# High-value companies to detect in snippets (lowercase for matching)
HIGH_VALUE_COMPANIES = [
    # FAANG / Big Tech
    "google",
    "meta",
    "facebook",
    "amazon",
    "apple",
    "microsoft",
    "netflix",
    "spotify",
    "uber",
    "airbnb",
    "stripe",
    "palantir",
    "salesforce",
    "oracle",
    "sap",
    "adobe",
    "intel",
    "nvidia",
    "tesla",
    "spacex",
    "openai",
    "anthropic",
    "deepmind",
    # European Tech
    "klarna",
    "n26",
    "revolut",
    "wise",
    "adyen",
    "delivery hero",
    "zalando",
    "celonis",
    "personio",
    "flixbus",
    "trade republic",
    "wefox",
    "gorillas",
    "contentful",
    "mambu",
    "messagebird",
    "mollie",
    "bitpanda",
    "scalable capital",
    # Consulting
    "mckinsey",
    "bcg",
    "bain",
    "roland berger",
    "deloitte",
    "accenture",
    "pwc",
    "kpmg",
    "ernst & young",
    # VC / PE
    "sequoia",
    "a16z",
    "andreessen horowitz",
    "index ventures",
    "earlybird",
    "project a",
    "hv capital",
    "lakestar",
    "rocket internet",
]

# Legal form suffixes to strip from company names
_LEGAL_FORMS_RE = re.compile(
    r"\s*(?:gGmbH|gmbh\s*&\s*co\.?\s*(?:kg|ohg)?|ug\s*(?:\(haftungsbeschränkt\))?"
    r"|gmbh|ag|se|kg|ohg|gbr|e\.?\s*k\.?|mbh|e\.?\s*v\.?|partg|kgaa)\s*$",
    re.IGNORECASE,
)


@dataclass
class OfficerLinkedInMatch:
    """Result of matching an officer to a LinkedIn profile."""

    linkedin_url: str
    name_from_search: str
    headline: Optional[str] = None
    location: Optional[str] = None
    snippet: str = ""
    previous_companies: List[str] = field(default_factory=list)
    match_confidence: float = 0.0
    source: str = "search_snippet"


def _clean_company_name(company_name: str) -> str:
    """Strip legal form suffixes from company name for better search matching."""
    return _LEGAL_FORMS_RE.sub("", company_name).strip()


def build_search_query(officer_name: str, company_name: str) -> str:
    """
    Build a search query to find an officer's LinkedIn profile.

    Uses DDG-friendly format (no site: prefix needed for DDG HTML).
    """
    clean_company = _clean_company_name(company_name)
    return f'linkedin.com/in "{officer_name}" "{clean_company}"'


def build_fallback_query(officer_name: str, company_city: str = None) -> str:
    """Fallback query without company name (broader search)."""
    parts = [f'linkedin.com/in "{officer_name}"']
    if company_city:
        parts.append(f'"{company_city}"')
    return " ".join(parts)


def _extract_name_and_headline(title: str):
    """
    Extract name and headline from a LinkedIn search result title.

    Common patterns:
    - "Max Mustermann - CEO at StartupX | LinkedIn"
    - "Max Mustermann - CEO at StartupX - LinkedIn"
    - "Max Mustermann | LinkedIn"
    """
    # Remove LinkedIn suffix
    clean = title.replace(" | LinkedIn", "").replace(" - LinkedIn", "").strip()

    if " - " in clean:
        parts = clean.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()

    return clean, None


def _extract_companies_from_text(text: str) -> List[str]:
    """Extract high-value company names mentioned in text."""
    text_lower = text.lower()
    found = []
    for company in HIGH_VALUE_COMPANIES:
        # Use word boundary for short names to avoid false positives
        if len(company) <= 4:
            if re.search(r"\b" + re.escape(company) + r"\b", text_lower):
                found.append(company.title())
        else:
            if company in text_lower:
                found.append(company.title())
    # Deduplicate while preserving order
    seen = set()
    result = []
    for c in found:
        if c.lower() not in seen:
            seen.add(c.lower())
            result.append(c)
    return result


def _extract_location(text: str) -> Optional[str]:
    """Extract location from snippet text."""
    # Common LinkedIn snippet location patterns
    location_patterns = [
        r"(?:located?\s+in|based\s+in|from)\s+([^.·|,]{3,40})",
        r"(?:^|\.\s+)([A-Z][a-zäöü]+(?:,\s*(?:Germany|Deutschland|Austria|Switzerland|Österreich|Schweiz)))",
    ]
    for pattern in location_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()[:100]

    # Try known DACH cities
    text_lower = text.lower()
    for city in [
        "berlin",
        "munich",
        "münchen",
        "hamburg",
        "frankfurt",
        "cologne",
        "köln",
        "düsseldorf",
        "stuttgart",
        "vienna",
        "wien",
        "zurich",
        "zürich",
        "graz",
        "basel",
        "bern",
    ]:
        if city in text_lower:
            return city.title()

    return None


def _calculate_match_confidence(
    officer_name: str,
    name_from_search: str,
    company_name: str,
    snippet: str,
    title: str,
    company_city: str = None,
    location: str = None,
) -> float:
    """
    Score how confident we are that this LinkedIn profile belongs to the officer.

    Three factors:
    - Name match quality (up to 0.50)
    - Company name in snippet (up to 0.30)
    - Location match (up to 0.20)
    """
    score = 0.0
    combined_text = f"{title} {snippet}".lower()

    # 1. Name match (up to 0.50)
    name_sim = SequenceMatcher(
        None,
        officer_name.lower().strip(),
        name_from_search.lower().strip(),
    ).ratio()

    if name_sim >= 0.95:
        score += 0.50
    elif name_sim >= 0.85:
        score += 0.40
    elif name_sim >= 0.70:
        score += 0.25
    else:
        score += name_sim * 0.30

    # 2. Company name match (up to 0.30)
    clean_company = _clean_company_name(company_name).lower()

    if clean_company and clean_company in combined_text:
        score += 0.30
    elif clean_company:
        # Partial match: check first significant word of company name
        words = [w for w in clean_company.split() if len(w) > 3]
        if words and words[0] in combined_text:
            score += 0.15

    # 3. Location match (up to 0.20)
    if company_city and location:
        if company_city.lower() in location.lower():
            score += 0.20
        elif location.lower() in company_city.lower():
            score += 0.20
        elif any(loc in location.lower() for loc in ["germany", "deutschland", "austria", "schweiz", "switzerland"]):
            score += 0.10
    elif company_city:
        # Check if city appears anywhere in text
        if company_city.lower() in combined_text:
            score += 0.15

    return min(score, 1.0)


def parse_search_result(
    title: str,
    snippet: str,
    url: str,
    officer_name: str,
    company_name: str,
    company_city: str = None,
) -> OfficerLinkedInMatch:
    """
    Parse a search result and score how well it matches the officer.

    Extracts name, headline, location, previous companies from
    the title and snippet text.
    """
    name_from_search, headline = _extract_name_and_headline(title)
    previous_companies = _extract_companies_from_text(f"{title} {snippet}")
    location = _extract_location(f"{title} {snippet}")

    confidence = _calculate_match_confidence(
        officer_name=officer_name,
        name_from_search=name_from_search or "",
        company_name=company_name,
        snippet=snippet,
        title=title,
        company_city=company_city,
        location=location,
    )

    return OfficerLinkedInMatch(
        linkedin_url=url,
        name_from_search=name_from_search or "",
        headline=headline,
        location=location,
        snippet=snippet[:500],
        previous_companies=previous_companies,
        match_confidence=confidence,
        source="search_snippet",
    )


def _search_via_ddgs_library(query: str) -> list:
    """
    Fallback: search using the ddgs/duckduckgo_search Python library.

    Uses a different API endpoint than HTML scraping, so it may work
    when the HTML endpoint is rate-limited.
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        logger.debug("ddgs/duckduckgo_search library not available")
        return []

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, region="de-de", max_results=10))

        # Convert to SearchResult-like objects (with .url, .title, .snippet)
        from dataclasses import dataclass as _dc

        @_dc
        class _Result:
            url: str
            title: str
            snippet: str

        return [
            _Result(url=r.get("href", ""), title=r.get("title", ""), snippet=r.get("body", "")) for r in raw_results
        ]
    except Exception as e:
        logger.warning(f"ddgs library search failed: {e}")
        return []


class RateLimitedError(Exception):
    """Raised when all search engines are rate-limited."""

    pass


def _search_ddg_no_retry(query: str) -> list:
    """
    Single-attempt DDG search via curl_cffi with NO internal retries.

    Returns results immediately or raises RateLimitedError on 202.
    This avoids the CurlCffiSearchScraper's built-in 30s/60s/120s
    backoff which makes the caller wait too long.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return []

    from urllib.parse import quote_plus

    from bs4 import BeautifulSoup

    try:
        response = curl_requests.get(
            f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
                "Referer": "https://duckduckgo.com/",
                "DNT": "1",
            },
            impersonate="chrome",
            timeout=15,
        )

        if response.status_code == 202:
            logger.warning("DDG returned 202 (rate limited)")
            raise RateLimitedError("DuckDuckGo returned 202 — rate limited")

        if response.status_code != 200:
            logger.warning(f"DDG returned {response.status_code}")
            return []

        # Parse results
        soup = BeautifulSoup(response.text, "html.parser")
        results = []

        from dataclasses import dataclass as _dc

        @_dc
        class _Result:
            url: str
            title: str
            snippet: str

        for div in soup.find_all("div", class_="result"):
            link = div.find("a", class_="result__a")
            snippet_el = div.find("a", class_="result__snippet")
            if link:
                href = link.get("href", "")
                title = link.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                results.append(_Result(url=href, title=title, snippet=snippet))

        logger.info(f"DDG (no-retry) returned {len(results)} results")
        return results

    except RateLimitedError:
        raise
    except Exception as e:
        logger.warning(f"DDG no-retry search failed: {e}")
        return []


def search_officer_linkedin(
    officer_name: str,
    company_name: str,
    company_city: str = None,
    search_engine=None,
    min_confidence: float = 0.40,
) -> Optional[OfficerLinkedInMatch]:
    """
    Search for an officer's LinkedIn profile and return best match.

    Uses DuckDuckGo via curl_cffi (snippet-first: never hits linkedin.com).
    Falls back to ddgs library if HTML endpoint is rate-limited.

    Args:
        officer_name: Officer's full name from Handelsregister
        company_name: Company name from Handelsregister
        company_city: Company city for location matching
        search_engine: Ignored (kept for API compat). Uses internal no-retry DDG.
        min_confidence: Minimum confidence threshold (default 0.40)

    Returns:
        Best OfficerLinkedInMatch above threshold, or None.

    Raises:
        RateLimitedError: If all search engines are rate-limited.
    """
    # Primary query: name + company
    query = build_search_query(officer_name, company_name)
    logger.info(f"Searching: {query}")

    results = []
    rate_limited = False

    # Try DDG with no-retry (returns fast on 202)
    try:
        results = _search_ddg_no_retry(query)
    except RateLimitedError:
        rate_limited = True

    # If DDG rate-limited, try ddgs library (different API endpoint)
    if rate_limited or not results:
        if rate_limited:
            logger.info("DDG HTML rate-limited, trying ddgs library fallback...")
        results = _search_via_ddgs_library(query)

    # Filter to LinkedIn profile URLs only
    linkedin_results = [r for r in results if "linkedin.com/in/" in r.url]

    # If no LinkedIn results, try fallback query (broader)
    if not linkedin_results:
        fallback_query = build_fallback_query(officer_name, company_city)
        logger.debug(f"Fallback search: {fallback_query}")

        results2 = []
        try:
            results2 = _search_ddg_no_retry(fallback_query)
        except RateLimitedError:
            rate_limited = True

        if not results2:
            results2 = _search_via_ddgs_library(fallback_query)

        linkedin_results = [r for r in results2 if "linkedin.com/in/" in r.url]

    # If still nothing from any source and we got rate limited, signal it
    if not results and not linkedin_results and rate_limited:
        raise RateLimitedError(f"All search engines rate-limited for '{officer_name}'")

    if not linkedin_results:
        logger.debug(f"No LinkedIn results for {officer_name}")
        return None

    # Score each result
    matches = []
    for result in linkedin_results:
        match = parse_search_result(
            title=result.title,
            snippet=result.snippet,
            url=result.url,
            officer_name=officer_name,
            company_name=company_name,
            company_city=company_city,
        )
        matches.append(match)

    # Return highest confidence match above threshold
    matches.sort(key=lambda m: m.match_confidence, reverse=True)

    best = matches[0]
    if best.match_confidence >= min_confidence:
        logger.debug(
            f"Match found: {best.name_from_search} ({best.match_confidence:.2f}) - {best.headline or 'no headline'}"
        )
        return best

    logger.debug(
        f"Best match below threshold: {best.name_from_search} ({best.match_confidence:.2f} < {min_confidence})"
    )
    return None
