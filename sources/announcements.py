"""
Announcements (Bekannmachungen) scraper for handelsregister.de

Fetches official announcements for companies including:
- Neueintragung (New registrations)
- Kapitalerhöhung (Capital increases)
- Geschäftsführer-Änderung (Management changes)
- Satzungsänderung (Statute changes)
- Auflösung (Dissolution)

This extends the basic search scraper to access the "VO" (Veröffentlichungen)
tab for each company, which contains the official Bundesanzeiger publications.

Legal basis: All data is publicly available per §9 HGB.
Rate limit: Respects 60 requests/hour per §303a, b StGB.
"""

import re
import time
from datetime import datetime
from typing import List, Dict, Optional, Iterator, Tuple
from dataclasses import dataclass
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

from sources.bundesapi import BundesAPISource, BundesAPIConfig, SearchResult


@dataclass
class Announcement:
    """A single announcement (Bekanntmachung) from the register."""
    company_name: str
    native_company_number: str
    announcement_type: str  # Neueintragung, Kapitalerhöhung, etc.
    announcement_date: Optional[str]
    publication_date: Optional[str]
    text: str
    capital_old: Optional[float] = None
    capital_new: Optional[float] = None
    officers_mentioned: List[str] = None
    raw_html: Optional[str] = None

    def __post_init__(self):
        if self.officers_mentioned is None:
            self.officers_mentioned = []


# Announcement type patterns (German)
ANNOUNCEMENT_TYPES = {
    'neueintragung': ['neueintragung', 'erstmalige eintragung', 'neue firma'],
    'kapitalerhoehung': ['kapitalerhöhung', 'erhöhung des stammkapitals', 'erhöhung des grundkapitals',
                         'kapital erhöht', 'stammkapital erhöht'],
    'kapitalherabsetzung': ['kapitalherabsetzung', 'herabsetzung des stammkapitals', 'kapital herabgesetzt'],
    'geschaeftsfuehrer': ['geschäftsführer', 'bestellt', 'abberufen', 'prokura', 'vertretungsbefugnis'],
    'satzungsaenderung': ['satzungsänderung', 'änderung der satzung', 'gesellschaftsvertrag geändert'],
    'sitzverlegung': ['sitzverlegung', 'sitz verlegt', 'neuer sitz'],
    'umwandlung': ['umwandlung', 'formwechsel', 'verschmelzung'],
    'aufloesung': ['auflösung', 'liquidation', 'abwicklung', 'löschung'],
    'insolvenz': ['insolvenz', 'insolvenzverfahren', 'eröffnung des insolvenz'],
}


class AnnouncementScraper:
    """
    Scraper for company announcements from handelsregister.de

    Workflow:
    1. Search for companies (using existing BundesAPISource)
    2. For each company, click into the VO (Veröffentlichungen) tab
    3. Parse the announcements HTML
    4. Extract structured data (type, date, capital amounts, etc.)
    """

    def __init__(self, config: Optional[BundesAPIConfig] = None):
        self.config = config or BundesAPIConfig()
        self.source = BundesAPISource(config)
        self._last_response = None

    def _classify_announcement(self, text: str) -> str:
        """Classify announcement type based on text content."""
        text_lower = text.lower()

        for ann_type, keywords in ANNOUNCEMENT_TYPES.items():
            if any(kw in text_lower for kw in keywords):
                return ann_type

        return 'sonstige'  # Other/unknown

    def _extract_capital_amounts(self, text: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Extract old and new capital amounts from announcement text.

        German number format: 1.234.567,89 EUR
        """
        # Pattern: "von EUR 25.000,00 auf EUR 100.000,00"
        pattern_change = r'von\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)\s*(?:EUR|€)?\s*auf\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)'
        match = re.search(pattern_change, text, re.IGNORECASE)
        if match:
            old_str, new_str = match.groups()
            return self._parse_german_number(old_str), self._parse_german_number(new_str)

        # Pattern: "Stammkapital: EUR 100.000,00" (just new amount)
        pattern_single = r'(?:stamm|grund)kapital[:\s]*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)'
        match = re.search(pattern_single, text, re.IGNORECASE)
        if match:
            return None, self._parse_german_number(match.group(1))

        return None, None

    def _parse_german_number(self, num_str: str) -> Optional[float]:
        """Parse German number format (1.234,56) to float."""
        if not num_str:
            return None
        try:
            # Remove thousand separators (.) and convert decimal comma to dot
            cleaned = num_str.replace('.', '').replace(',', '.')
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    def _extract_officers(self, text: str) -> List[str]:
        """Extract officer names mentioned in announcement."""
        officers = []

        # Common patterns for officer names
        # "Geschäftsführer: Max Mustermann, geb. 01.01.1980"
        # "Bestellt als Geschäftsführer: Dr. Hans Schmidt"
        patterns = [
            r'(?:geschäftsführer|vorstand|prokurist)[:\s]+([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)+)',
            r'(?:bestellt|abberufen)[^:]*:\s*([A-ZÄÖÜ][a-zäöüß]+(?:\s+[A-ZÄÖÜ][a-zäöüß]+)+)',
        ]

        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            officers.extend(matches)

        return list(set(officers))  # Remove duplicates

    def _parse_announcement_block(self, block_html: str, company_name: str,
                                   native_company_number: str) -> Optional[Announcement]:
        """Parse a single announcement block from the VO page."""
        soup = BeautifulSoup(block_html, 'lxml')

        # Extract text content
        text = soup.get_text(separator=' ', strip=True)
        if not text or len(text) < 20:
            return None

        # Extract date (typically in format DD.MM.YYYY)
        date_match = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
        announcement_date = date_match.group(1) if date_match else None

        # Classify announcement type
        ann_type = self._classify_announcement(text)

        # Extract capital amounts if applicable
        capital_old, capital_new = self._extract_capital_amounts(text)

        # Extract officers if mentioned
        officers = self._extract_officers(text)

        return Announcement(
            company_name=company_name,
            native_company_number=native_company_number,
            announcement_type=ann_type,
            announcement_date=announcement_date,
            publication_date=datetime.now().strftime('%Y-%m-%d'),
            text=text[:2000],  # Limit text length
            capital_old=capital_old,
            capital_new=capital_new,
            officers_mentioned=officers,
            raw_html=block_html[:5000] if len(block_html) > 5000 else block_html,
        )

    def fetch_announcements_for_company(
        self,
        search_result: SearchResult,
        max_announcements: int = 10,
    ) -> List[Announcement]:
        """
        Fetch announcements for a single company.

        This requires clicking into the company detail page and
        accessing the VO (Veröffentlichungen) tab.

        Args:
            search_result: SearchResult from a previous search
            max_announcements: Maximum announcements to return

        Returns:
            List of Announcement objects
        """
        # Note: This is a placeholder for the actual implementation
        # The full implementation would:
        # 1. Click on the VO button for this company row
        # 2. Parse the resulting HTML
        # 3. Extract individual announcements

        # For now, we'll return an empty list and log that this needs implementation
        print(f"fetch_announcements_for_company not yet implemented for: {search_result.name}")
        return []

    def search_with_announcements(
        self,
        keywords: List[str],
        keyword_mode: str = 'all',
        states: Optional[List[str]] = None,
        max_results: int = 10,
        fetch_announcements: bool = True,
    ) -> Iterator[Tuple[SearchResult, List[Announcement]]]:
        """
        Search for companies and optionally fetch their announcements.

        This is a higher-level method that combines search + announcement fetching.

        Args:
            keywords: Search keywords
            keyword_mode: 'all', 'min', or 'exact'
            states: State codes to filter
            max_results: Maximum companies to return
            fetch_announcements: Whether to fetch announcements (uses extra requests)

        Yields:
            Tuples of (SearchResult, List[Announcement])
        """
        for result in self.source.search(
            keywords=keywords,
            keyword_mode=keyword_mode,
            states=states,
            max_results=max_results,
        ):
            announcements = []

            if fetch_announcements:
                # This will use an additional request per company
                if not self.source.rate_limiter.acquire(timeout=60):
                    print("Rate limit reached, stopping announcement fetching")
                    fetch_announcements = False
                else:
                    announcements = self.fetch_announcements_for_company(result)

            yield result, announcements


def detect_announcement_type(text: str) -> str:
    """
    Detect the type of announcement from text.

    Returns one of:
    - neueintragung
    - kapitalerhoehung
    - kapitalherabsetzung
    - geschaeftsfuehrer
    - satzungsaenderung
    - sitzverlegung
    - umwandlung
    - aufloesung
    - insolvenz
    - sonstige
    """
    text_lower = text.lower()

    for ann_type, keywords in ANNOUNCEMENT_TYPES.items():
        if any(kw in text_lower for kw in keywords):
            return ann_type

    return 'sonstige'


def extract_capital_from_text(text: str) -> Dict[str, Optional[float]]:
    """
    Extract capital information from announcement text.

    Returns dict with 'old' and 'new' capital amounts.
    """
    result = {'old': None, 'new': None}

    # Pattern: "von EUR 25.000,00 auf EUR 100.000,00"
    pattern_change = r'von\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)\s*(?:EUR|€)?\s*auf\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)'
    match = re.search(pattern_change, text, re.IGNORECASE)
    if match:
        old_str, new_str = match.groups()
        result['old'] = _parse_german_number(old_str)
        result['new'] = _parse_german_number(new_str)
        return result

    # Pattern: "Stammkapital: EUR 100.000,00"
    pattern_single = r'(?:stamm|grund)kapital[:\s]*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)'
    match = re.search(pattern_single, text, re.IGNORECASE)
    if match:
        result['new'] = _parse_german_number(match.group(1))

    return result


def _parse_german_number(num_str: str) -> Optional[float]:
    """Parse German number format to float."""
    if not num_str:
        return None
    try:
        cleaned = num_str.replace('.', '').replace(',', '.')
        return float(cleaned)
    except (ValueError, TypeError):
        return None
