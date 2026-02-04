"""
News Monitor - Track German startup news via RSS feeds.

Monitors RSS feeds from German startup media outlets to:
1. Discover funding announcements
2. Find new AI/robotics startups
3. Track investor activity

Free data source - no API limits.
"""

import re
import logging
import hashlib
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Iterator
from dataclasses import dataclass
import xml.etree.ElementTree as ET

try:
    import urllib.request
    import urllib.error
except ImportError:
    pass

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """A news article from RSS feed."""
    title: str
    url: str
    published_date: Optional[str]
    source: str
    description: Optional[str]
    content_hash: str  # For deduplication


@dataclass
class FundingMention:
    """A funding-related mention extracted from news."""
    company_name: str
    investors: List[str]
    amount: Optional[float]
    currency: Optional[str]
    round_type: Optional[str]
    article_url: str
    article_title: str
    source: str
    extracted_at: str


# German startup media RSS feeds
DEFAULT_RSS_FEEDS = [
    {
        'name': 'Gruenderszene',
        'url': 'https://www.gruenderszene.de/feed',
        'type': 'startup_news',
    },
    {
        'name': 't3n',
        'url': 'https://t3n.de/rss.xml',
        'type': 'tech_news',
    },
    {
        'name': 'deutsche-startups',
        'url': 'https://www.deutsche-startups.de/feed/',
        'type': 'startup_news',
    },
    {
        'name': 'Handelsblatt Tech',
        'url': 'https://www.handelsblatt.com/contentexport/feed/tech',
        'type': 'business_news',
    },
]

# Keywords indicating funding news
FUNDING_KEYWORDS = [
    # German
    'finanzierung', 'finanzierungsrunde', 'investment', 'investition',
    'millionen', 'kapitalerhöhung', 'series a', 'series b', 'series c',
    'seed', 'pre-seed', 'wachstumsfinanzierung', 'venture capital',
    'risikokapital', 'investor', 'investoren', 'beteiligung',
    # English (often used in German articles)
    'funding', 'raised', 'round', 'backed', 'million', 'capital',
]

# Keywords indicating AI/robotics
AI_ROBOTICS_KEYWORDS = [
    'künstliche intelligenz', 'ki', 'artificial intelligence', 'ai',
    'machine learning', 'deep learning', 'robotik', 'robotics',
    'automation', 'automatisierung', 'neural', 'nlp', 'computer vision',
    'autonomous', 'autonom', 'chatbot', 'llm', 'generative ai',
]


class NewsMonitor:
    """
    Monitor RSS feeds for startup funding news.

    Provides free, real-time detection of:
    - Funding announcements
    - New AI/robotics companies
    - Investor activity
    """

    def __init__(
        self,
        feeds: Optional[List[Dict]] = None,
        user_agent: str = 'HandelsregisterScraper/1.0 (https://github.com)',
    ):
        """
        Initialize news monitor.

        Args:
            feeds: List of feed configs (name, url, type)
            user_agent: User agent for requests
        """
        self.feeds = feeds or DEFAULT_RSS_FEEDS
        self.user_agent = user_agent

    def fetch_feed(self, feed_url: str) -> Optional[str]:
        """Fetch RSS feed content."""
        try:
            request = urllib.request.Request(
                feed_url,
                headers={'User-Agent': self.user_agent}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode('utf-8')
        except Exception as e:
            logger.warning("Failed to fetch feed %s: %s", feed_url, e)
            return None

    def parse_feed(self, xml_content: str, source_name: str) -> List[NewsArticle]:
        """Parse RSS/Atom feed XML into articles."""
        articles = []

        try:
            root = ET.fromstring(xml_content)

            # Try RSS format first
            items = root.findall('.//item')

            # Try Atom format if no RSS items
            if not items:
                # Atom uses different namespace
                ns = {'atom': 'http://www.w3.org/2005/Atom'}
                items = root.findall('.//atom:entry', ns)
                if not items:
                    items = root.findall('.//{http://www.w3.org/2005/Atom}entry')

            for item in items:
                article = self._parse_item(item, source_name)
                if article:
                    articles.append(article)

        except ET.ParseError as e:
            logger.error("Failed to parse feed XML: %s", e)

        return articles

    def _parse_item(self, item: ET.Element, source_name: str) -> Optional[NewsArticle]:
        """Parse a single RSS/Atom item."""
        # Try RSS format
        title = self._get_text(item, 'title')
        link = self._get_text(item, 'link')
        pub_date = self._get_text(item, 'pubDate')
        description = self._get_text(item, 'description')

        # Try Atom format if RSS fields empty
        if not link:
            link_elem = item.find('link')
            if link_elem is not None:
                link = link_elem.get('href', '')

        # Atom uses 'published' or 'updated'
        if not pub_date:
            pub_date = self._get_text(item, 'published') or self._get_text(item, 'updated')

        # Atom uses 'summary' or 'content'
        if not description:
            description = self._get_text(item, 'summary') or self._get_text(item, 'content')

        if not title or not link:
            return None

        # Create content hash for deduplication
        content_hash = hashlib.md5(
            (title + link).encode('utf-8')
        ).hexdigest()

        return NewsArticle(
            title=title,
            url=link,
            published_date=pub_date,
            source=source_name,
            description=description,
            content_hash=content_hash,
        )

    def _get_text(self, element: ET.Element, tag: str) -> Optional[str]:
        """Get text content of a child element."""
        child = element.find(tag)
        if child is not None and child.text:
            return child.text.strip()

        # Try with namespace
        for ns_prefix in ['', '{http://www.w3.org/2005/Atom}', '{http://purl.org/rss/1.0/}']:
            child = element.find(ns_prefix + tag)
            if child is not None and child.text:
                return child.text.strip()

        return None

    def fetch_all_articles(self, max_per_feed: int = 50) -> List[NewsArticle]:
        """Fetch articles from all configured feeds."""
        all_articles = []

        for feed in self.feeds:
            logger.info("Fetching feed: %s", feed['name'])

            content = self.fetch_feed(feed['url'])
            if not content:
                continue

            articles = self.parse_feed(content, feed['name'])
            all_articles.extend(articles[:max_per_feed])

            logger.info("Got %d articles from %s", len(articles), feed['name'])

        return all_articles

    def is_funding_related(self, article: NewsArticle) -> bool:
        """Check if article is about funding."""
        text = f"{article.title} {article.description or ''}".lower()

        return any(keyword in text for keyword in FUNDING_KEYWORDS)

    def is_ai_robotics_related(self, article: NewsArticle) -> bool:
        """Check if article is about AI/robotics."""
        text = f"{article.title} {article.description or ''}".lower()

        return any(keyword in text for keyword in AI_ROBOTICS_KEYWORDS)

    def extract_funding_info(self, article: NewsArticle) -> Optional[FundingMention]:
        """
        Extract funding information from article.

        Uses regex patterns to extract:
        - Company name
        - Funding amount
        - Investors
        - Round type
        """
        text = f"{article.title} {article.description or ''}"

        # Extract amount (German format: "5 Millionen Euro" or "5M EUR")
        amount = None
        currency = None

        amount_patterns = [
            r'(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?|Million|M)\s*(?:Euro|EUR|€)',
            r'€\s*(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?|Million|M)',
            r'(\d+(?:[,\.]\d+)?)\s*(?:Milliarden|Mrd\.?|Billion|B)\s*(?:Euro|EUR|€)',
        ]

        for pattern in amount_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace(',', '.')
                amount = float(amount_str)
                currency = 'EUR'

                # Convert to actual amount
                if 'milliard' in match.group(0).lower() or 'billion' in match.group(0).lower():
                    amount *= 1_000_000_000
                else:
                    amount *= 1_000_000
                break

        # Try USD
        if not amount:
            usd_patterns = [
                r'\$\s*(\d+(?:[,\.]\d+)?)\s*(?:million|m)\b',
                r'(\d+(?:[,\.]\d+)?)\s*(?:million|m)\s*(?:dollar|usd|\$)',
            ]
            for pattern in usd_patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    amount_str = match.group(1).replace(',', '.')
                    amount = float(amount_str) * 1_000_000
                    currency = 'USD'
                    break

        # Extract round type
        round_type = None
        round_patterns = [
            (r'\b(pre-?seed)\b', 'pre_seed'),
            (r'\b(seed)\s*(?:runde|round|finanzierung)?\b', 'seed'),
            (r'\b(series\s*a)\b', 'series_a'),
            (r'\b(series\s*b)\b', 'series_b'),
            (r'\b(series\s*c)\b', 'series_c'),
            (r'\b(series\s*d)\b', 'series_d'),
            (r'\b(wachstums?finanzierung|growth)\b', 'growth'),
        ]

        for pattern, round_name in round_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                round_type = round_name
                break

        # Extract company name (heuristic: capitalized words before "erhält", "sammelt", "raises")
        company_name = None
        company_patterns = [
            r'([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)\s+(?:erhält|sammelt|raises|secures|schließt)',
            r'(?:startup|fintech|healthtech|saas)\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)',
        ]

        for pattern in company_patterns:
            match = re.search(pattern, text)
            if match:
                company_name = match.group(1).strip()
                break

        if not company_name:
            # Fall back to first capitalized phrase in title
            match = re.search(r'^([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)', article.title)
            if match:
                company_name = match.group(1).strip()

        # Extract investors (after "von", "from", "durch", "lead by")
        investors = []
        investor_patterns = [
            r'(?:von|from|durch|angeführt von|lead by|led by)\s+([A-Z][A-Za-z0-9\s,&]+?)(?:\.|,\s*(?:sowie|und|and)|$)',
        ]

        for pattern in investor_patterns:
            match = re.search(pattern, text)
            if match:
                investor_text = match.group(1)
                # Split by common separators
                for inv in re.split(r'\s*(?:,|und|and|sowie|&)\s*', investor_text):
                    inv = inv.strip()
                    if inv and len(inv) > 2:
                        investors.append(inv)
                break

        if not (amount or company_name):
            return None

        return FundingMention(
            company_name=company_name or 'Unknown',
            investors=investors,
            amount=amount,
            currency=currency,
            round_type=round_type,
            article_url=article.url,
            article_title=article.title,
            source=article.source,
            extracted_at=datetime.utcnow().isoformat(),
        )

    def scan_for_funding(self) -> List[FundingMention]:
        """
        Scan all feeds for funding news.

        Returns:
            List of extracted funding mentions
        """
        articles = self.fetch_all_articles()

        funding_mentions = []

        for article in articles:
            if self.is_funding_related(article):
                mention = self.extract_funding_info(article)
                if mention:
                    funding_mentions.append(mention)
                    logger.info(
                        "Found funding: %s - %s %s",
                        mention.company_name,
                        mention.amount,
                        mention.currency
                    )

        return funding_mentions

    def scan_for_ai_startups(self) -> List[NewsArticle]:
        """
        Scan all feeds for AI/robotics startup news.

        Returns:
            List of relevant articles
        """
        articles = self.fetch_all_articles()

        return [a for a in articles if self.is_ai_robotics_related(a)]
