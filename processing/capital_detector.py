"""
Capital raise detection from multiple sources.

Since direct Bundesanzeiger scraping is legally problematic (CAPTCHA + ToS),
this module uses alternative strategies:

1. Capital diff detection - Track capital changes over time
2. Publication mining - Parse publications from enrichment APIs
3. News monitoring - Optional RSS feed monitoring
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass
class CapitalEvent:
    """Detected capital event."""

    event_type: str  # 'increase', 'decrease', 'initial'
    event_date: Optional[str]
    previous_amount: Optional[float]
    new_amount: Optional[float]
    change_amount: Optional[float]
    currency: str
    publication_text: Optional[str]
    source: str  # 'diff', 'publication', 'news'
    confidence_score: float  # 0.0 to 1.0


class CapitalRaiseDetector:
    """
    Detect capital raises from various sources.

    Uses pattern matching and text analysis to identify
    capital changes from publications and tracking.
    """

    # Keywords indicating capital raises (German)
    CAPITAL_RAISE_KEYWORDS = [
        "kapitalerhöhung",
        "erhöhung des stammkapitals",
        "erhöhung des grundkapitals",
        "capital increase",
        "stammkapital erhöht",
        "grundkapital erhöht",
        "neue gesellschafter",
        "sacheinlage",
        "bareinlage",
        "aufstockung",
        "kapitalaufstockung",
    ]

    # Keywords indicating capital decrease
    CAPITAL_DECREASE_KEYWORDS = [
        "kapitalherabsetzung",
        "herabsetzung des stammkapitals",
        "capital decrease",
        "stammkapital herabgesetzt",
    ]

    # Patterns to extract amounts (German number format: 1.234.567,89)
    AMOUNT_PATTERNS = [
        # "EUR 100.000,00" or "100.000 EUR"
        r"(?:EUR|€)\s*([\d.]+(?:,\d{2})?)",
        r"([\d.]+(?:,\d{2})?)\s*(?:EUR|€|Euro)",
        # "Stammkapital: 25.000,00 Euro"
        r"(?:stamm|grund)kapital[:\s]*([\d.]+(?:,\d{2})?)",
        # "auf EUR 500.000,00 erhöht"
        r"auf\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)\s*(?:EUR|€|Euro)?\s*erhöht",
        # "von EUR 25.000 auf EUR 100.000"
        r"von\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)\s*auf\s*(?:EUR|€)?\s*([\d.]+(?:,\d{2})?)",
    ]

    def __init__(self):
        # Pre-compile patterns
        self._raise_patterns = [
            re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in self.CAPITAL_RAISE_KEYWORDS
        ]
        self._decrease_patterns = [
            re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE) for kw in self.CAPITAL_DECREASE_KEYWORDS
        ]
        self._amount_patterns = [re.compile(p, re.IGNORECASE) for p in self.AMOUNT_PATTERNS]

    def detect_from_publications(
        self, publications: List[Dict], current_capital: Optional[float] = None
    ) -> List[CapitalEvent]:
        """
        Detect capital events from publication texts.

        Args:
            publications: List of publication dicts with 'text', 'date', 'type'
            current_capital: Current known capital amount for comparison

        Returns:
            List of detected CapitalEvent objects
        """
        events = []

        for pub in publications:
            text = pub.get("text", "")
            if not text:
                continue

            event = self._analyze_publication(
                text=text,
                pub_date=pub.get("date"),
                pub_type=pub.get("type"),
                current_capital=current_capital,
            )

            if event:
                events.append(event)

        return events

    def detect_from_capital_diff(
        self,
        previous_capital: Optional[float],
        current_capital: Optional[float],
        detection_date: Optional[str] = None,
    ) -> Optional[CapitalEvent]:
        """
        Detect capital change by comparing values.

        Args:
            previous_capital: Previously recorded capital amount
            current_capital: Newly observed capital amount
            detection_date: Date of detection

        Returns:
            CapitalEvent if change detected, None otherwise
        """
        if previous_capital is None or current_capital is None:
            return None

        if previous_capital == current_capital:
            return None

        change = current_capital - previous_capital

        # Only flag significant changes (>10% or >10k)
        if previous_capital > 0:
            pct_change = abs(change) / previous_capital
            if pct_change < 0.1 and abs(change) < 10000:
                return None

        event_type = "increase" if change > 0 else "decrease"

        return CapitalEvent(
            event_type=event_type,
            event_date=detection_date or datetime.now().isoformat(),
            previous_amount=previous_capital,
            new_amount=current_capital,
            change_amount=abs(change),
            currency="EUR",
            publication_text=f"Capital changed from {previous_capital:,.2f} to {current_capital:,.2f} EUR",
            source="diff",
            confidence_score=0.7,  # Medium confidence for diff detection
        )

    def _analyze_publication(
        self,
        text: str,
        pub_date: Optional[str],
        pub_type: Optional[str],
        current_capital: Optional[float],
    ) -> Optional[CapitalEvent]:
        """Analyze a single publication for capital events."""
        text_lower = text.lower()

        # Check for capital raise keywords
        raise_matches = sum(1 for p in self._raise_patterns if p.search(text))
        decrease_matches = sum(1 for p in self._decrease_patterns if p.search(text))

        if raise_matches == 0 and decrease_matches == 0:
            return None

        # Determine event type
        event_type = "increase" if raise_matches > decrease_matches else "decrease"

        # Extract amounts
        amounts = self._extract_amounts(text)

        # Determine amounts
        previous_amount = None
        new_amount = None
        change_amount = None

        if len(amounts) >= 2:
            # Assume smaller is previous, larger is new (for increases)
            sorted_amounts = sorted(amounts)
            if event_type == "increase":
                previous_amount = sorted_amounts[0]
                new_amount = sorted_amounts[-1]
            else:
                previous_amount = sorted_amounts[-1]
                new_amount = sorted_amounts[0]
            change_amount = abs(new_amount - previous_amount)
        elif len(amounts) == 1:
            new_amount = amounts[0]
            if current_capital and current_capital != new_amount:
                previous_amount = current_capital
                change_amount = abs(new_amount - current_capital)

        # Calculate confidence
        confidence = self._calculate_confidence(
            keyword_matches=max(raise_matches, decrease_matches),
            has_amounts=bool(amounts),
            has_change=change_amount is not None,
        )

        return CapitalEvent(
            event_type=event_type,
            event_date=pub_date,
            previous_amount=previous_amount,
            new_amount=new_amount,
            change_amount=change_amount,
            currency="EUR",
            publication_text=text[:500] if len(text) > 500 else text,
            source="publication",
            confidence_score=confidence,
        )

    def _extract_amounts(self, text: str) -> List[float]:
        """Extract monetary amounts from text."""
        amounts = []

        for pattern in self._amount_patterns:
            matches = pattern.findall(text)
            for match in matches:
                # Handle tuple from patterns with multiple groups
                if isinstance(match, tuple):
                    for m in match:
                        if m:
                            amount = self._parse_german_number(m)
                            if amount and amount > 0:
                                amounts.append(amount)
                else:
                    amount = self._parse_german_number(match)
                    if amount and amount > 0:
                        amounts.append(amount)

        # Remove duplicates while preserving order
        seen = set()
        unique = []
        for a in amounts:
            if a not in seen:
                seen.add(a)
                unique.append(a)

        return unique

    def _parse_german_number(self, num_str: str) -> Optional[float]:
        """
        Parse German number format to float.

        German: 1.234.567,89 -> 1234567.89
        """
        if not num_str:
            return None

        try:
            # Remove thousand separators (.) and convert decimal comma to dot
            cleaned = num_str.replace(".", "").replace(",", ".")
            return float(cleaned)
        except (ValueError, TypeError):
            return None

    def _calculate_confidence(
        self,
        keyword_matches: int,
        has_amounts: bool,
        has_change: bool,
    ) -> float:
        """Calculate confidence score for detection."""
        score = 0.0

        # Base score from keywords (max 0.4)
        score += min(keyword_matches * 0.15, 0.4)

        # Bonus for extracted amounts
        if has_amounts:
            score += 0.3

        # Bonus for detected change
        if has_change:
            score += 0.3

        return min(score, 1.0)


class NewsCapitalMonitor:
    """
    Monitor tech news RSS feeds for funding announcements.

    This is an optional supplement to publication-based detection.
    """

    # German tech/startup news RSS feeds
    NEWS_FEEDS = [
        "https://www.gruenderszene.de/feed",
        "https://t3n.de/rss.xml",
        "https://www.deutsche-startups.de/feed/",
    ]

    # Keywords indicating funding news
    FUNDING_KEYWORDS = [
        "finanzierung",
        "funding",
        "investment",
        "millionen",
        "million",
        "seed",
        "series a",
        "series b",
        "series c",
        "kapital",
        "venture",
        "fundraising",
        "einsammeln",
    ]

    def __init__(self, feeds: Optional[List[str]] = None):
        self.feeds = feeds or self.NEWS_FEEDS.copy()

    def scan_feeds(self) -> List[Dict]:
        """
        Scan RSS feeds for funding news.

        Returns:
            List of articles that may contain funding announcements
        """
        try:
            import feedparser
        except ImportError:
            print("feedparser not installed. Run: pip install feedparser")
            return []

        articles = []

        for feed_url in self.feeds:
            try:
                feed = feedparser.parse(feed_url)

                for entry in feed.entries[:20]:  # Check recent entries
                    title = entry.get("title", "")
                    summary = entry.get("summary", "")
                    text = f"{title} {summary}".lower()

                    # Check for funding keywords
                    if any(kw in text for kw in self.FUNDING_KEYWORDS):
                        articles.append(
                            {
                                "title": title,
                                "link": entry.get("link"),
                                "date": entry.get("published"),
                                "summary": summary,
                                "source": feed_url,
                            }
                        )

            except Exception as e:
                print(f"Error parsing feed {feed_url}: {e}")
                continue

        return articles

    def match_to_companies(
        self,
        articles: List[Dict],
        company_names: List[str],
    ) -> List[Tuple[str, Dict]]:
        """
        Match news articles to known company names.

        Args:
            articles: List of article dicts
            company_names: List of company names to match

        Returns:
            List of (company_name, article) tuples
        """
        matches = []

        # Create simple matching patterns
        name_patterns = []
        for name in company_names:
            # Extract core name (without legal form)
            core_name = re.sub(r"\s*(GmbH|AG|UG|KG|e\.V\.).*$", "", name, flags=re.IGNORECASE)
            if len(core_name) >= 3:
                pattern = re.compile(rf"\b{re.escape(core_name)}\b", re.IGNORECASE)
                name_patterns.append((name, pattern))

        for article in articles:
            text = f"{article.get('title', '')} {article.get('summary', '')}"

            for name, pattern in name_patterns:
                if pattern.search(text):
                    matches.append((name, article))
                    break  # One match per article

        return matches
