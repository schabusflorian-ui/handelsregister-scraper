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
from typing import List, Dict, Optional, Iterator, Tuple
from dataclasses import dataclass, field
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
    confidence: float = 0.0  # How confident we are in the extraction


# German startup media RSS feeds
DEFAULT_RSS_FEEDS = [
    # === Core German Startup News ===
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
        'name': 'Sonnenseite',
        'url': 'https://www.sonnenseite.com/feed/',
        'type': 'climate_news',
    },
    {
        'name': 'Munich Startup',
        'url': 'https://www.munich-startup.de/feed/',
        'type': 'startup_news',
    },
    {
        'name': 'Startbase',
        'url': 'https://www.startbase.com/feed/',
        'type': 'startup_news',
    },
    {
        'name': 'Hamburg Startups',
        'url': 'https://www.hamburg-startups.net/feed/',
        'type': 'startup_news',
    },

    # === VC / Deal News ===
    {
        'name': 'VC Magazine',
        'url': 'https://www.vc-magazin.de/feed/',
        'type': 'vc_news',
    },
    {
        'name': 'Finance Forward',
        'url': 'https://financefwd.com/de/feed/',
        'type': 'fintech_news',
    },

    # === Tech & Innovation ===
    {
        'name': 'Heise Online',
        'url': 'https://www.heise.de/rss/heise-atom.xml',
        'type': 'tech_news',
    },
    {
        'name': 'Golem.de',
        'url': 'https://rss.golem.de/rss.php?feed=ATOM1.0',
        'type': 'tech_news',
    },
    {
        'name': 'Computerbase',
        'url': 'https://www.computerbase.de/rss/news.xml',
        'type': 'tech_news',
    },

    # === Climate / Energy / Sustainability ===
    {
        'name': 'Cleanthinking',
        'url': 'https://www.cleanthinking.de/feed/',
        'type': 'climate_news',
    },
    {
        'name': 'Edison Media',
        'url': 'https://edison.media/feed/',
        'type': 'energy_news',
    },
    {
        'name': 'PV Magazine DE',
        'url': 'https://www.pv-magazine.de/feed/',
        'type': 'energy_news',
    },
    {
        'name': 'Electrive',
        'url': 'https://www.electrive.net/feed/',
        'type': 'emobility_news',
    },
    {
        'name': 'H2 View',
        'url': 'https://www.h2-view.com/feed/',
        'type': 'hydrogen_news',
    },

    # === European Startup Ecosystem ===
    {
        'name': 'Tech.eu',
        'url': 'https://tech.eu/feed/',
        'type': 'startup_news',
    },
    {
        'name': 'Sifted',
        'url': 'https://sifted.eu/feed',
        'type': 'startup_news',
    },

    # === AI / Robotics Specific ===
    {
        'name': 'The Decoder',
        'url': 'https://the-decoder.de/feed/',
        'type': 'ai_news',
    },
    {
        'name': 'Autonomes Fahren',
        'url': 'https://www.autonomes-fahren.de/feed/',
        'type': 'robotics_news',
    },

    # === Early Stage / Grants / Spinoffs ===
    {
        'name': 'Startupdetector',
        'url': 'https://startupdetector.de/feed/',
        'type': 'early_stage_news',
    },
    {
        'name': 'Gruenderkueche',
        'url': 'https://www.gruenderkueche.de/feed/',
        'type': 'early_stage_news',
    },
    {
        'name': 'VDI Nachrichten',
        'url': 'https://www.vdi-nachrichten.com/feed/',
        'type': 'research_news',
    },
]

# Patterns that strongly indicate actual funding events (not just advice articles)
# Each is (regex_pattern, weight). An article needs score >= 2 to count.
FUNDING_SIGNALS = [
    # Strong signals - actual funding events
    (r'\berhält\s+\d+\s*(?:Millionen|Mio)', 3),
    (r'\bsammelt\s+\d+\s*(?:Millionen|Mio)', 3),
    (r'\beingesammelt\b', 3),
    (r'\braised?\b.*\d+\s*(?:million|m)\b', 3),
    (r'\bfinanzierungsrunde\b', 3),
    (r'\bkapitalerhöhung\b', 3),
    (r'\bseries\s+[a-d]\b', 3),
    (r'\b(?:pre-?)?seed(?:-?runde| round)\b', 3),
    (r'#DealMonitor\b', 3),

    # Medium signals - likely funding context
    (r'\d+\s*(?:Millionen|Mio\.?)\b.*\b(?:einsammeln|investier|finanzier)', 2),
    (r'\bfunding\b', 2),
    (r'\bwachstumsfinanzierung\b', 2),
    (r'\brisikokapital\b', 2),
    (r'\bneuer?\s+Fonds\b', 2),

    # Climate funding signals
    (r'\bklima(?:fonds|finanzierung|investition)\b', 2),
    (r'\bgreen\s+(?:bond|funding|investment)\b', 2),
    (r'\bimpact\s+(?:invest|fund)', 2),

    # Grant/stipendium/early-stage signals
    (r'\bEXIST[- ](?:Gründerstipendium|Forschungstransfer)\b', 3),
    (r'\bGründerstipendium\b', 3),
    (r'\bGründerpreis\b', 2),
    (r'\bFörder(?:ung|bescheid|mittel|programm)\b', 2),
    (r'\bstipendium\b', 2),
    (r'\bBMBF[- ](?:Förderung|Projekt)\b', 2),
    (r'\bBMWK?i?[- ](?:Förderung|Programm)\b', 2),
    (r'\bHTGF\b', 2),
    (r'\bHigh-Tech Gründerfonds\b', 3),
    (r'\bangel\s+(?:round|runde|invest)', 2),
    (r'\bbusiness\s+angel\b', 1),
    (r'\bpre-?seed\b', 2),
    (r'\baccelerator\b', 1),
    (r'\binkubator\b', 1),
    (r'\bAusgründung\b', 2),
    (r'\bspin-?off\b', 2),
    (r'\buniversitäts?-?(?:startup|gründung|ausgründung)\b', 2),
    (r'\bEIC\s+(?:Accelerator|Pathfinder)\b', 2),

    # Weak signals - need multiple to count
    (r'\bventure\s+capital\b', 1),
    (r'\binvestition\b', 1),
    (r'\bbeteiligung\b', 1),
    (r'\binvestor(?:en)?\b', 1),
]

# Keywords indicating AI/robotics - use word boundaries to avoid false matches
AI_ROBOTICS_PATTERNS = [
    # === AI Core ===
    r'\bkünstliche(?:r|n|s)?\s+intelligenz\b',
    r'\b(?:K|k)(?:I|i)[-\s](?:Startup|Unternehmen|Firma|Tool|Agent|Model|System|Funktion)',
    r'\bartificial\s+intelligence\b',
    r'\bmachine\s+learning\b',
    r'\bmaschinelles\s+lernen\b',
    r'\bdeep\s+learning\b',
    r'\bgenerative\s+(?:ai|ki)\b',
    r'\blarge\s+language\s+model\b',
    r'\bfoundation\s+model\b',
    r'\bdiffusion\s+model\b',
    r'\btext-to-(?:image|video)\b',
    r'\bagentic\s+ai\b',
    r'\bai\s+agent\b',
    r'\bKI-\w+',  # KI-Startup, KI-Firma, KI-Agenten, etc.
    r'\bAI\b',
    r'\.ai\b',

    # === NLP / Language AI ===
    r'\bnlp\b',
    r'\bchatbot\b',
    r'\bllm\b',
    r'\bconversational\s+ai\b',
    r'\bsprachverarbeitung\b',
    r'\bspracherkennung\b',
    r'\bspeech\s+recognition\b',
    r'\btext\s+mining\b',
    r'\bretrieval\s+augmented\s+generation\b',
    r'\brag\b',
    r'\bvector\s+database\b',

    # === Computer Vision ===
    r'\bcomputer\s+vision\b',
    r'\bbildverarbeitung\b',
    r'\bbilderkennung\b',
    r'\bobjekterkennung\b',
    r'\bgesichtserkennung\b',
    r'\blidar\b',
    r'\bmachine\s+vision\b',
    r'\bvideo\s*analytics\b',

    # === Robotics ===
    r'\brobotik\b',
    r'\brobotics\b',
    r'\brobotic\b',
    r'\brobot\b',
    r'\bcobot\b',
    r'\bhumanoide?\b',
    r'\bexoskelett\b',
    r'\bdrone\b',
    r'\bdrohne\b',
    r'\buav\b',
    r'\bserviceroboter\b',
    r'\bindustrieroboter\b',

    # === Autonomous / Process Automation ===
    r'\bautonome\s+(?:systeme|fahrzeuge|fahren)\b',
    r'\bselbstfahrend\b',
    r'\brpa\b',
    r'\bprocess\s+automation\b',
    r'\brobotic\s+process\s+automation\b',
    r'\bindustrial\s+automation\b',
    r'\bindustrie\s+4\.0\b',
    r'\bsmart\s+factory\b',
    r'\bdigital(?:er)?\s+zwilling\b',
    r'\bdigital\s+twin\b',

    # === Data Science / ML ===
    r'\bdata\s+science\b',
    r'\bpredictive\s+(?:analytics|maintenance)\b',
    r'\banomaly\s+detection\b',
    r'\bmlops\b',
    r'\bautoml\b',
    r'\bedge\s+ai\b',
]

# Keywords indicating climate tech / cleantech
CLIMATE_PATTERNS = [
    r'\bcleantech\b',
    r'\bgreentech\b',
    r'\bclimate\s*tech\b',
    r'\bklimatechnologie\b',
    r'\berneuerbare\s+energie\b',
    r'\brenewable\s+energy\b',
    r'\bphotovoltaik\b',
    r'\bsolar(?:energie|energy|panel|modul)\b',
    r'\bwindenergie\b',
    r'\bwind\s+(?:energy|turbine|kraft)\b',
    r'\bwasserstoff\b',
    r'\bhydrogen\b',
    r'\bgrüne(?:r|n|s)?\s+wasserstoff\b',
    r'\bgreen\s+hydrogen\b',
    r'\bbrennstoffzelle\b',
    r'\bfuel\s+cell\b',
    r'\belektromobilität\b',
    r'\belectric\s+vehicle\b',
    r'\bladeinfrastruktur\b',
    r'\benergiespeicher\b',
    r'\benergy\s+storage\b',
    r'\bbatterietechnologie\b',
    r'\bsolid\s+state\s+battery\b',
    r'\bfestkörperbatterie\b',
    r'\bcarbon\s+capture\b',
    r'\bco2-abscheidung\b',
    r'\bdekarbonisierung\b',
    r'\bdecarbonization\b',
    r'\bsmart\s+grid\b',
    r'\bwärmepumpe\b',
    r'\bheat\s+pump\b',
    r'\bgeothermie\b',
    r'\bagritech\b',
    r'\bvertical\s+farming\b',
    r'\bprecision\s+farming\b',
]

# Combined patterns for backward compatibility
AI_ROBOTICS_CLIMATE_PATTERNS = AI_ROBOTICS_PATTERNS + CLIMATE_PATTERNS

# Patterns for early-stage / grant / university spinoff detection
EARLY_STAGE_PATTERNS = [
    r'\bEXIST[- ]?(?:Gründerstipendium|Forschungstransfer|Gründungskultur)\b',
    r'\bGründerstipendium\b',
    r'\bGründerpreis\b',
    r'\bstartup[- ]?stipendium\b',
    r'\bFörder(?:ung|bescheid|mittel|programm)\b',
    r'\bBMBF\b',
    r'\bBMWK?\b',
    r'\bHTGF\b',
    r'\bHigh-Tech\s+Gründerfonds\b',
    r'\bpre-?seed\b',
    r'\bangel[- ](?:round|runde|invest|funding)\b',
    r'\bbusiness\s+angels?\b',
    r'\baccelerator(?:-?programm)?\b',
    r'\binkubator\b',
    r'\bincubator\b',
    r'\bAusgründung\b',
    r'\bspin-?off\b',
    r'\buni(?:versitäts?)?[- ]?(?:startup|gründung|ausgründung|spinoff)\b',
    r'\bForschungstransfer\b',
    r'\bTechnologietransfer\b',
    r'\bEIC\s+(?:Accelerator|Pathfinder)\b',
    r'\bHorizon\s+(?:Europe|2020)\b',
    r'\bINVEST[- ]Zuschuss\b',
    r'\bERP[- ]Gründerkredit\b',
    r'\bZIM[- ]Förderung\b',
    r'\bGründerwettbewerb\b',
    r'\bfrühphasen(?:finanzierung|investor|kapital)\b',
    r'\bearly[- ]?stage\b',
    r'\bfounders?\s+(?:program|programm|grant|stipend)\b',
    r'\bCyber\s+Valley\b',
    r'\bUnternehmerTUM\b',
    r'\bFraunhofer\s+Venture\b',
    r'\bMax\s+Planck\s+Innovation\b',
]

# Words that should NOT be extracted as company names
GERMAN_STOPWORDS = {
    'ich', 'du', 'er', 'sie', 'es', 'wir', 'ihr', 'sie',
    'der', 'die', 'das', 'den', 'dem', 'des', 'ein', 'eine',
    'vom', 'von', 'zum', 'zur', 'mit', 'bei', 'nach', 'aus',
    'vor', 'über', 'unter', 'zwischen', 'hinter', 'neben',
    'warum', 'wie', 'was', 'wer', 'wo', 'wann', 'welche',
    'diese', 'dieser', 'dieses', 'jeder', 'jede', 'jedes',
    'anfang', 'ende', 'plötzlich', 'wegen', 'ohne', 'hier',
    'dort', 'neue', 'neuer', 'neues', 'zwei', 'drei', 'vier',
    'auktion', 'fortpflanzung', 'narzissmus', 'rechenzentrum',
    'aufnahme', 'unzufrieden',
}


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
        user_agent: str = 'HandelsregisterScraper/1.0',
    ):
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
        title = self._get_text(item, 'title')
        link = self._get_text(item, 'link')
        pub_date = self._get_text(item, 'pubDate')
        description = self._get_text(item, 'description')

        # Atom feeds use <link href="..."/> attributes instead of text content
        if not link:
            for ns_prefix in ['', '{http://www.w3.org/2005/Atom}']:
                link_elem = item.find(ns_prefix + 'link')
                if link_elem is not None:
                    href = link_elem.get('href', '')
                    if href:
                        link = href
                        break

        if not pub_date:
            pub_date = self._get_text(item, 'published') or self._get_text(item, 'updated')

        if not description:
            description = self._get_text(item, 'summary') or self._get_text(item, 'content')

        if not title or not link:
            return None

        # Strip HTML from description
        if description:
            description = re.sub(r'<[^>]+>', ' ', description)
            description = re.sub(r'\s+', ' ', description).strip()

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
        """
        Check if article is about an actual funding event.

        Uses weighted signals instead of simple keyword matching
        to reduce false positives from advice/opinion articles.
        """
        text = f"{article.title} {article.description or ''}".lower()

        score = 0
        for pattern, weight in FUNDING_SIGNALS:
            if re.search(pattern, text, re.IGNORECASE):
                score += weight

        return score >= 2

    def is_ai_robotics_related(self, article: NewsArticle) -> bool:
        """Check if article is about AI/robotics/climate tech."""
        text = f"{article.title} {article.description or ''}"

        return any(re.search(p, text, re.IGNORECASE) for p in AI_ROBOTICS_CLIMATE_PATTERNS)

    def is_early_stage_signal(self, article: NewsArticle) -> bool:
        """
        Check if article mentions early-stage signals (grants, stipends, spinoffs).

        Detects:
        - EXIST Gründerstipendium recipients
        - BMBF/BMWK grant recipients
        - University spinoffs (Ausgründung)
        - Accelerator/incubator program entries
        - Angel/pre-seed rounds
        - HTGF investments
        """
        text = f"{article.title} {article.description or ''}"

        return any(re.search(p, text, re.IGNORECASE) for p in EARLY_STAGE_PATTERNS)

    def extract_funding_info(self, article: NewsArticle) -> Optional[FundingMention]:
        """
        Extract structured funding information from article.

        Tries source-specific parsers first (DealMonitor format),
        then falls back to generic extraction.
        """
        # Try DealMonitor format first (deutsche-startups)
        if '#DealMonitor' in (article.title or ''):
            return self._parse_dealmonitor(article)

        # Try StartupTicker format
        if '#StartupTicker' in (article.title or ''):
            return self._parse_startup_ticker(article)

        # Generic extraction
        return self._extract_generic(article)

    def _parse_dealmonitor(self, article: NewsArticle) -> Optional[FundingMention]:
        """
        Parse deutsche-startups #DealMonitor format.

        Example: "#DealMonitor - Enua erhält 25 Millionen – Additive Drives..."
        """
        text = f"{article.title} {article.description or ''}"

        # DealMonitor titles list multiple deals separated by – or +++
        # Extract the first/main deal
        deals = re.split(r'\s*[–—]\s*|\s*\+\+\+\s*', text)

        # Name fragment allowing lowercase starts (one.five, co-reactive, etc.)
        _n = r'[A-Za-z][A-Za-z0-9\.\-]*(?:\s+[A-Za-z][A-Za-z0-9\.\-]+)*'

        # Find the first segment with a funding amount
        for deal in deals:
            deal = deal.strip()
            if not deal:
                continue

            # Pattern: "CompanyName erhält/sammelt X Millionen"
            match = re.search(
                rf'({_n})\s+'
                r'(?:erhält|sammelt|bekommt|sichert\s+sich|schließt)\s+'
                r'(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)',
                deal
            )
            if match:
                company = match.group(1).strip()
                if self._is_valid_company_name(company):
                    amount = float(match.group(2).replace(',', '.')) * 1_000_000
                    return FundingMention(
                        company_name=company,
                        investors=[],
                        amount=amount,
                        currency='EUR',
                        round_type=self._extract_round_type(deal),
                        article_url=article.url,
                        article_title=article.title,
                        source=article.source,
                        extracted_at=datetime.utcnow().isoformat(),
                        confidence=0.9,
                    )

            # Pattern: "CompanyName sammelt X Millionen ein"
            match = re.search(
                rf'({_n})\s+'
                r'sammelt\s+(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)\s+ein',
                deal
            )
            if match:
                company = match.group(1).strip()
                if self._is_valid_company_name(company):
                    amount = float(match.group(2).replace(',', '.')) * 1_000_000
                    return FundingMention(
                        company_name=company,
                        investors=[],
                        amount=amount,
                        currency='EUR',
                        round_type=self._extract_round_type(deal),
                        article_url=article.url,
                        article_title=article.title,
                        source=article.source,
                        extracted_at=datetime.utcnow().isoformat(),
                        confidence=0.9,
                    )

        return None

    def _parse_startup_ticker(self, article: NewsArticle) -> Optional[FundingMention]:
        """
        Parse deutsche-startups #StartupTicker format.

        These list multiple startups but don't always have funding amounts.
        Extract company names mentioned.
        """
        text = f"{article.title} {article.description or ''}"

        # StartupTicker mentions multiple companies with +++
        segments = re.split(r'\s*\+\+\+\s*', text)

        # Return the first company name found
        for segment in segments:
            segment = segment.strip()
            # Skip the header
            if '#StartupTicker' in segment:
                continue
            # Extract company-like name (allow lowercase starts for modern names)
            match = re.search(r'([A-Za-z][A-Za-z0-9\.\-]+(?:\s+[A-Za-z][A-Za-z0-9\.\-]+)*)', segment)
            if match:
                name = match.group(1).strip()
                if self._is_valid_company_name(name) and len(name) >= 3:
                    return FundingMention(
                        company_name=name,
                        investors=[],
                        amount=None,
                        currency=None,
                        round_type=None,
                        article_url=article.url,
                        article_title=article.title,
                        source=article.source,
                        extracted_at=datetime.utcnow().isoformat(),
                        confidence=0.5,
                    )

        return None

    def _extract_generic(self, article: NewsArticle) -> Optional[FundingMention]:
        """
        Generic funding extraction for non-structured articles.

        Looks for patterns like:
        - "X erhält Y Millionen"
        - "X sammelt Y Millionen ein"
        - "X raises $Y million"
        - "Neuer Fonds: X Millionen"
        """
        text = f"{article.title} {article.description or ''}"

        # Step 1: Extract amount
        amount, currency = self._extract_amount(text)

        # Step 2: Extract company name using funding-context patterns
        company_name = self._extract_company_name(text)

        # Step 3: Extract investors
        investors = self._extract_investors(text)

        # Step 4: Extract round type
        round_type = self._extract_round_type(text)

        # Only return if we have meaningful data
        # Require at least a company name OR an amount
        if not company_name and not amount:
            return None

        # Calculate confidence based on what we extracted
        confidence = 0.3
        if company_name and amount:
            confidence = 0.8
        elif company_name and round_type:
            confidence = 0.7
        elif amount:
            confidence = 0.5

        return FundingMention(
            company_name=company_name or '',
            investors=investors,
            amount=amount,
            currency=currency,
            round_type=round_type,
            article_url=article.url,
            article_title=article.title,
            source=article.source,
            extracted_at=datetime.utcnow().isoformat(),
            confidence=confidence,
        )

    def _extract_amount(self, text: str) -> Tuple[Optional[float], Optional[str]]:
        """Extract funding amount from text."""
        # Special case: "die erste Million" = 1M EUR
        if re.search(r'(?:die\s+)?erste\s+Million\b', text, re.IGNORECASE):
            return 1_000_000.0, 'EUR'

        # EUR amounts - with or without explicit currency
        eur_patterns = [
            # "25 Millionen Euro" / "25 Mio. Euro" / "25 Mio. EUR"
            (r'(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)\s*(?:Euro|EUR|€)', 1_000_000),
            # "€25 Millionen" / "€ 25 Mio"
            (r'(?:€|EUR)\s*(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)', 1_000_000),
            # "25 Milliarden Euro"
            (r'(\d+(?:[,\.]\d+)?)\s*(?:Milliarden|Mrd\.?)\s*(?:Euro|EUR|€)', 1_000_000_000),
            # "X erhält/sammelt 25 Millionen" (implicit EUR in German startup context)
            (r'(?:erhält|sammelt|bekommt|eingesammelt|einsammeln|sichert sich)\s+(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)', 1_000_000),
            # "90 Millionen in" (investment context, implicit EUR)
            (r'(\d+(?:[,\.]\d+)?)\s*(?:Millionen|Mio\.?)\s+(?:in\s+|investier)', 1_000_000),
        ]

        for pattern, multiplier in eur_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace(',', '.')
                amount = float(amount_str) * multiplier
                return amount, 'EUR'

        # USD amounts
        usd_patterns = [
            r'\$\s*(\d+(?:[,\.]\d+)?)\s*(?:million|m)\b',
            r'(\d+(?:[,\.]\d+)?)\s*(?:million|m)\s*(?:dollar|usd|\$)',
        ]

        for pattern in usd_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount_str = match.group(1).replace(',', '.')
                amount = float(amount_str) * 1_000_000
                return amount, 'USD'

        return None, None

    def _extract_company_name(self, text: str) -> Optional[str]:
        """
        Extract company name from funding article text.

        Uses verb-context patterns specific to German funding headlines.
        Handles both uppercase and lowercase startup names (e.g. one.five, co-reactive).
        """
        # Name fragment: 1-4 words, allows lowercase starts, dots, hyphens
        # Limited to max 4 words to avoid grabbing sentence fragments
        _w = r'[A-Za-z][A-Za-z0-9\.\-]*'
        _n = rf'{_w}(?:\s+{_w}){{0,3}}'

        # Patterns ordered by specificity (most specific first)
        patterns = [
            # "CompanyName: X Mio" / "CompanyName: Seed-Runde" (colon headlines)
            rf'^({_n})\s*:\s+(?:\d|Seed|Series|Pre|Angel|Grant)',
            # "CompanyName erhält/sammelt/bekommt X Millionen"
            rf'({_n})\s+(?:erhält|sammelt|bekommt|sichert\s+sich|schließt)',
            # "hat CompanyName ... eingesammelt" / "hat CompanyName ... geschlossen"
            rf'hat\s+({_n})\s+.*?(?:eingesammelt|geschlossen|erhalten|bekommen)',
            # "CompanyName hat ... eingesammelt"
            rf'({_n})\s+(?:hat|haben)\s+.*eingesammelt',
            # "X raises/secures/closes"
            rf'({_n})\s+(?:raises|secures|closes)',
            # "Startup X" / "Fintech X" / "KI-Startup X"
            rf'(?:Startup|Start-up|Fintech|Healthtech|Insurtech|SaaS|KI-Startup|AI-Startup|Cleantech-Startup|HealthTech-Startup)\s+({_n})',
            # "Gründer von X" / "Gründer-Team von X"
            rf'(?:Gründer(?:-Team)?|Founder)\s+(?:von|of)\s+({_n})',
            # "Series/Seed/Runde für CompanyName" (funding context only)
            rf'(?:Series\s+\w|Seed|Finanzierung|Runde)\s+für\s+({_n})',
            # "bei X" in funding context, e.g. "investiert bei CompanyName"
            rf'investier\w*\s+(?:bei|in)\s+({_n})',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                name = match.group(1).strip()
                # Validate: not a stopword, not too short
                if self._is_valid_company_name(name):
                    return name

        return None

    def _is_valid_company_name(self, name: str) -> bool:
        """Check if extracted name is plausibly a company name."""
        if not name or len(name) < 2:
            return False

        # Reject placeholder names
        if name.lower() in ('unknown', 'unbekannt'):
            return False

        # Reject German stopwords and common non-company words
        if name.lower() in GERMAN_STOPWORDS:
            return False

        # Reject common non-company words (German nouns, adjectives, geographic)
        blacklist = {
            'der', 'die', 'das', 'ein', 'kein', 'sein', 'ihr',
            'maschmeyers', 'ehemalige',
            'europäische', 'deutsche', 'berliner', 'münchner',
            'frühphasen', 'bremer', 'hamburger', 'kölner',
            # Countries and regions that appear in headlines
            'deutschland', 'germany', 'europa', 'europe',
            'kroatien', 'frankreich', 'österreich', 'schweiz',
            'italien', 'spanien', 'polen', 'china', 'indien',
            'bayern', 'sachsen', 'hessen', 'brandenburg',
            # Generic words that slip through
            'incubation', 'investment', 'finanzierung', 'förderung',
            'millionen', 'milliarden', 'prozent', 'umsatz',
            'wissenschaftler', 'solche',
        }
        if name.lower() in blacklist:
            return False

        # Single character
        if len(name) <= 1:
            return False

        # Reject names that are clearly sentence fragments (contain articles/prepositions)
        words = name.lower().split()
        fragment_words = {'der', 'die', 'das', 'den', 'dem', 'des',
                         'ein', 'eine', 'einer', 'einem', 'einen',
                         'und', 'oder', 'aber', 'sein', 'ihr', 'alle',
                         'von', 'vom', 'zum', 'zur', 'mit', 'für',
                         'wird', 'werden', 'wurde', 'hat', 'haben',
                         'damit', 'seine', 'seiner', 'seinen',
                         'the', 'a', 'an', 'and', 'for', 'with',
                         'based', 'french', 'german', 'dutch',
                         'lithuanian', 'austrian', 'swiss',
                         'startup', 'investor', 'company',
                         'platform', 'analysis', 'tech', 'climate',
                         'skaliert', 'running'}
        if len(words) > 1 and any(w in fragment_words for w in words):
            return False

        # Reject if too many words (likely a sentence, not a name)
        if len(words) > 3:
            return False

        return True

    def _extract_investors(self, text: str) -> List[str]:
        """Extract investor names from text."""
        investors = []

        patterns = [
            # "angeführt von InvestorName" / "lead by InvestorName"
            r'(?:angeführt|geführt|geleitet)\s+von\s+([A-Z][A-Za-z0-9\s,&\.\-]+?)(?:\.|,\s+(?:sowie|und|and|mit)|$)',
            # "led by InvestorName"
            r'(?:led?|backed)\s+by\s+([A-Z][A-Za-z0-9\s,&\.\-]+?)(?:\.|,\s+(?:and|with)|$)',
            # "von InvestorName Capital/Ventures/Partners"
            r'von\s+([A-Z][A-Za-z0-9]+(?:\s+(?:Capital|Ventures|Partners|Invest|Fund))+)',
            # "Investor InvestorName"
            r'(?:Investor|Lead-Investor|Hauptinvestor)\s+([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+)*)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                investor_text = match.group(1).strip()
                # Split by separators
                for inv in re.split(r'\s*(?:,|und|and|sowie|&)\s*', investor_text):
                    inv = inv.strip()
                    # Must look like an investor name (capitalized, not too short)
                    if inv and len(inv) > 3 and inv[0].isupper():
                        # Don't add obvious non-investor words
                        if inv.lower() not in GERMAN_STOPWORDS:
                            investors.append(inv)
                break

        return investors

    def _extract_round_type(self, text: str) -> Optional[str]:
        """Extract funding round type from text."""
        round_patterns = [
            # Grants & stipends (earliest stage)
            (r'\bEXIST[- ]?(?:Gründerstipendium|Forschungstransfer)\b', 'grant'),
            (r'\bGründerstipendium\b', 'grant'),
            (r'\bFörder(?:ung|bescheid|mittel|programm)\b', 'grant'),
            (r'\bstipendium\b', 'grant'),
            (r'\bBMBF\b', 'grant'),
            # Angel
            (r'\bangel\s+(?:round|runde|invest)', 'angel'),
            (r'\bbusiness\s+angels?\b', 'angel'),
            # Pre-seed & seed
            (r'\bpre-?seed\b', 'pre_seed'),
            (r'\bseed(?:-?runde|\s+round|\s+finanzierung)?\b', 'seed'),
            # Series rounds
            (r'\bseries\s*a\b', 'series_a'),
            (r'\bseries\s*b\b', 'series_b'),
            (r'\bseries\s*c\b', 'series_c'),
            (r'\bseries\s*d\b', 'series_d'),
            # Growth
            (r'\bwachstums?finanzierung\b', 'growth'),
            (r'\bgrowth\s*(?:round|runde)\b', 'growth'),
        ]

        for pattern, round_name in round_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return round_name

        return None

    def scan_for_funding(self, min_confidence: float = 0.5) -> List[FundingMention]:
        """
        Scan all feeds for funding news.

        Args:
            min_confidence: Minimum confidence for extracted mentions

        Returns:
            List of extracted funding mentions
        """
        articles = self.fetch_all_articles()

        funding_mentions = []

        for article in articles:
            if self.is_funding_related(article):
                mention = self.extract_funding_info(article)
                if mention and mention.confidence >= min_confidence:
                    funding_mentions.append(mention)
                    logger.info(
                        "Found funding: %s - %s %s (confidence: %.0f%%)",
                        mention.company_name,
                        mention.amount,
                        mention.currency,
                        mention.confidence * 100,
                    )

        return funding_mentions

    def scan_for_early_stage(self) -> List[NewsArticle]:
        """
        Scan all feeds for early-stage signals.

        Detects grants, stipends, university spinoffs, accelerator entries,
        angel rounds, and pre-seed funding.

        Returns:
            List of articles with early-stage signals
        """
        articles = self.fetch_all_articles()

        return [a for a in articles if self.is_early_stage_signal(a)]

    def scan_for_ai_startups(self) -> List[NewsArticle]:
        """
        Scan all feeds for AI/robotics startup news.

        Returns:
            List of relevant articles
        """
        articles = self.fetch_all_articles()

        return [a for a in articles if self.is_ai_robotics_related(a)]
