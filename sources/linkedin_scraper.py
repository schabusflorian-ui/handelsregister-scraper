"""
LinkedIn profile scraper for extracting public profile data.

Scrapes publicly accessible LinkedIn profile information including
name, headline, location, and summary/about section.
"""

import re
import time
import random
import logging
import json
import requests
import cloudscraper
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]


# Keywords that indicate stealth mode
STEALTH_KEYWORDS = [
    'stealth', 'stealth mode', 'building something',
    'something new', 'exciting news soon', 'coming soon',
    'new venture', 'working on', 'exploring opportunities',
    'next chapter', 'what\'s next', 'in transition',
]

# High-value background companies
HIGH_VALUE_COMPANIES = [
    'google', 'meta', 'facebook', 'amazon', 'apple', 'microsoft',
    'stripe', 'airbnb', 'uber', 'lyft', 'spotify', 'netflix',
    'klarna', 'n26', 'revolut', 'wise', 'transferwise',
    'delivery hero', 'zalando', 'celonis', 'personio', 'flixbus',
    'soundcloud', 'contentful', 'gorillas', 'flink',
    'mckinsey', 'bcg', 'bain', 'ycombinator', 'y combinator',
    'sequoia', 'andreessen', 'a16z', 'index ventures', 'accel',
]

# Founder-related keywords
FOUNDER_KEYWORDS = [
    'founder', 'co-founder', 'cofounder', 'gründer', 'mitgründer',
    'ceo', 'chief executive', 'managing director', 'geschäftsführer',
    'entrepreneur', 'serial entrepreneur', 'angel investor',
]

# Co-founder seeking signals
COFOUNDER_SEEKING_KEYWORDS = [
    'looking for co-founder', 'looking for cofounder', 'seeking co-founder',
    'seeking cofounder', 'need a co-founder', 'need a cofounder',
    'suche mitgründer', 'suche co-founder', 'looking for a technical',
    'looking for a business', 'need technical co-founder', 'need business co-founder',
    'open to co-founding', 'want to co-found', 'looking to co-found',
    'seeking technical partner', 'seeking business partner',
    'building with someone', 'looking for founding team',
]

# Technical role indicators
TECHNICAL_KEYWORDS = [
    'engineer', 'developer', 'programmer', 'software', 'backend', 'frontend',
    'full stack', 'fullstack', 'devops', 'sre', 'data scientist', 'ml engineer',
    'machine learning', 'ai engineer', 'cto', 'tech lead', 'architect',
    'python', 'javascript', 'java', 'golang', 'rust', 'react', 'node',
    'cloud', 'aws', 'infrastructure', 'platform', 'security engineer',
    'mobile developer', 'ios', 'android', 'blockchain', 'web3',
]

# Business role indicators
BUSINESS_KEYWORDS = [
    'business', 'sales', 'marketing', 'growth', 'operations', 'strategy',
    'coo', 'cfo', 'cmo', 'cro', 'head of sales', 'head of marketing',
    'business development', 'bd', 'partnerships', 'account', 'customer success',
    'general manager', 'managing director', 'investment', 'finance',
    'consulting', 'consultant', 'mba', 'analyst', 'venture',
]

# Product role indicators
PRODUCT_KEYWORDS = [
    'product', 'product manager', 'pm', 'cpo', 'product lead',
    'product owner', 'product director', 'head of product',
    'ux', 'user experience', 'user research', 'product design',
]

# Design role indicators
DESIGN_KEYWORDS = [
    'design', 'designer', 'ux', 'ui', 'creative', 'art director',
    'visual', 'brand', 'graphic', 'illustration', 'figma', 'sketch',
]

# Common skills to extract
COMMON_SKILLS = [
    # Programming languages
    'python', 'javascript', 'typescript', 'java', 'golang', 'go', 'rust',
    'c++', 'c#', 'ruby', 'php', 'swift', 'kotlin', 'scala', 'r',
    # Frameworks
    'react', 'angular', 'vue', 'node.js', 'django', 'flask', 'rails',
    'spring', 'nextjs', 'express', 'fastapi',
    # Cloud/Infra
    'aws', 'gcp', 'azure', 'kubernetes', 'docker', 'terraform',
    # Data
    'sql', 'postgresql', 'mongodb', 'redis', 'elasticsearch',
    'spark', 'kafka', 'airflow', 'dbt',
    # ML/AI
    'machine learning', 'deep learning', 'tensorflow', 'pytorch',
    'nlp', 'computer vision', 'data science',
    # Business skills
    'fundraising', 'sales', 'marketing', 'growth hacking', 'seo',
    'product management', 'strategy', 'operations', 'finance',
    'business development', 'partnerships', 'negotiation',
]

# DACH region location indicators for filtering (Germany, Austria, Switzerland)
DACH_LOCATIONS = [
    # === GERMANY ===
    # Country
    'germany', 'deutschland', 'german',
    # Major cities
    'berlin', 'munich', 'münchen', 'hamburg', 'frankfurt', 'cologne', 'köln',
    'düsseldorf', 'dusseldorf', 'stuttgart', 'dortmund', 'essen', 'leipzig',
    'bremen', 'dresden', 'hanover', 'hannover', 'nuremberg', 'nürnberg',
    'duisburg', 'bochum', 'wuppertal', 'bielefeld', 'bonn', 'münster',
    'karlsruhe', 'mannheim', 'augsburg', 'wiesbaden', 'aachen', 'heidelberg',
    # Regions/States
    'bavaria', 'bayern', 'baden-württemberg', 'north rhine-westphalia',
    'nordrhein-westfalen', 'hesse', 'hessen', 'saxony', 'sachsen',
    'lower saxony', 'niedersachsen', 'rhineland-palatinate', 'rheinland-pfalz',
    # Tech hubs
    'potsdam', 'freiburg', 'darmstadt', 'regensburg', 'wolfsburg',

    # === AUSTRIA ===
    # Country
    'austria', 'österreich', 'austrian',
    # Major cities
    'vienna', 'wien', 'graz', 'linz', 'salzburg', 'innsbruck', 'klagenfurt',
    'villach', 'wels', 'st. pölten', 'dornbirn', 'wiener neustadt', 'steyr',
    # Regions
    'tyrol', 'tirol', 'styria', 'steiermark', 'carinthia', 'kärnten',
    'upper austria', 'oberösterreich', 'lower austria', 'niederösterreich',
    'vorarlberg', 'burgenland',

    # === SWITZERLAND ===
    # Country
    'switzerland', 'schweiz', 'suisse', 'svizzera', 'swiss',
    # Major cities
    'zurich', 'zürich', 'geneva', 'genève', 'genf', 'basel', 'bern', 'berne',
    'lausanne', 'winterthur', 'lucerne', 'luzern', 'st. gallen', 'lugano',
    'biel', 'thun', 'köniz', 'la chaux-de-fonds', 'fribourg', 'schaffhausen',
    'chur', 'neuchâtel', 'zug',
    # Regions/Cantons
    'canton of zurich', 'kanton zürich', 'canton of bern', 'canton of geneva',
    'canton of vaud', 'ticino', 'valais', 'wallis', 'graubünden', 'aargau',
]

# Alias for backwards compatibility
GERMAN_LOCATIONS = DACH_LOCATIONS


@dataclass
class WorkExperience:
    """A single work experience entry."""
    title: str
    company: str
    duration: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    is_current: bool = False


@dataclass
class LinkedInProfile:
    """Extracted LinkedIn profile data."""
    url: str
    name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    summary: Optional[str] = None
    current_company: Optional[str] = None
    previous_companies: List[str] = field(default_factory=list)

    # Enhanced profile data
    skills: List[str] = field(default_factory=list)
    experience: List[WorkExperience] = field(default_factory=list)
    education: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)

    # Co-founder matching signals
    looking_for_cofounder: bool = False
    cofounder_signals: List[str] = field(default_factory=list)

    # Role/function classification
    is_technical: bool = False
    is_business: bool = False
    is_product: bool = False
    is_design: bool = False
    primary_function: Optional[str] = None  # technical, business, product, design, other

    # Detection metadata
    stealth_signals: List[str] = field(default_factory=list)
    high_value_background: List[str] = field(default_factory=list)
    founder_signals: List[str] = field(default_factory=list)
    confidence_score: float = 0.0

    scraped_at: datetime = field(default_factory=datetime.now)
    raw_html: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            'linkedin_url': self.url,
            'name': self.name,
            'headline': self.headline,
            'location': self.location,
            'summary': self.summary,
            'current_company': self.current_company,
            'previous_companies': json.dumps(self.previous_companies) if self.previous_companies else None,
            'skills': json.dumps(self.skills) if self.skills else None,
            'experience': json.dumps([{
                'title': e.title, 'company': e.company, 'duration': e.duration,
                'location': e.location, 'is_current': e.is_current
            } for e in self.experience]) if self.experience else None,
            'education': json.dumps(self.education) if self.education else None,
            'looking_for_cofounder': self.looking_for_cofounder,
            'cofounder_signals': json.dumps(self.cofounder_signals) if self.cofounder_signals else None,
            'primary_function': self.primary_function,
            'stealth_signals': json.dumps(self.stealth_signals) if self.stealth_signals else None,
            'confidence_score': self.confidence_score,
        }


class LinkedInProfileScraper:
    """
    Scrapes public LinkedIn profile pages.

    Note: LinkedIn aggressively blocks scrapers. This scraper works with
    public profiles but may get blocked with heavy use.
    """

    def __init__(
        self,
        delay_range: tuple = (5, 15),
        proxy: Optional[str] = None,
        use_cloudscraper: bool = True,
    ):
        self.delay_range = delay_range
        self.proxy = proxy
        # Use cloudscraper for better bot bypass
        if use_cloudscraper:
            self.session = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
            )
        else:
            self.session = requests.Session()

    def _get_headers(self) -> Dict[str, str]:
        """Get randomized headers."""
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        }

    def _delay(self):
        """Random delay between requests."""
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def _fetch_profile(self, url: str) -> Optional[str]:
        """
        Fetch a LinkedIn profile page.

        Args:
            url: LinkedIn profile URL

        Returns:
            HTML content or None if error
        """
        proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None

        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                proxies=proxies,
                timeout=30,
                allow_redirects=True,
            )

            if response.status_code == 999:
                logger.warning(f"LinkedIn blocked request (999): {url}")
                return None

            if response.status_code == 429:
                logger.warning(f"Rate limited (429): {url}")
                return None

            if response.status_code != 200:
                logger.warning(f"HTTP {response.status_code}: {url}")
                return None

            # Check for auth wall
            if 'authwall' in response.url or 'login' in response.url:
                logger.debug(f"Auth wall hit: {url}")
                # Still try to parse - some data might be in the HTML
                pass

            return response.text

        except requests.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            return None

    def _parse_profile(self, html: str, url: str) -> LinkedInProfile:
        """
        Parse LinkedIn profile HTML.

        LinkedIn's HTML structure changes frequently, so this uses
        multiple strategies to extract data.
        """
        profile = LinkedInProfile(url=url, raw_html=html)
        soup = BeautifulSoup(html, 'html.parser')

        # Strategy 1: Look for JSON-LD structured data
        profile = self._extract_from_json_ld(soup, profile)

        # Strategy 2: Parse HTML elements
        profile = self._extract_from_html(soup, profile)

        # Strategy 3: Parse from meta tags
        profile = self._extract_from_meta(soup, profile)

        # Strategy 4: Extract work experience
        profile = self._extract_experience(soup, profile)

        # Detect signals and calculate confidence
        profile = self._detect_signals(profile)

        return profile

    def _extract_from_json_ld(self, soup: BeautifulSoup, profile: LinkedInProfile) -> LinkedInProfile:
        """Extract data from JSON-LD script tags."""
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)

                if isinstance(data, dict):
                    if data.get('@type') == 'Person':
                        profile.name = profile.name or data.get('name')
                        if 'address' in data:
                            addr = data['address']
                            if isinstance(addr, dict):
                                profile.location = addr.get('addressLocality') or addr.get('addressCountry')
                            elif isinstance(addr, str):
                                profile.location = addr

            except (json.JSONDecodeError, TypeError):
                continue

        return profile

    def _extract_from_html(self, soup: BeautifulSoup, profile: LinkedInProfile) -> LinkedInProfile:
        """Extract data from HTML elements."""

        # Name - various selectors LinkedIn has used
        name_selectors = [
            'h1.top-card-layout__title',
            'h1.text-heading-xlarge',
            '.pv-top-card--list li:first-child',
            '.top-card__title',
            'h1',
        ]
        for selector in name_selectors:
            elem = soup.select_one(selector)
            if elem and elem.get_text(strip=True):
                name = elem.get_text(strip=True)
                # Filter out non-name content
                if len(name) < 100 and not any(x in name.lower() for x in ['linkedin', 'sign in', 'join']):
                    profile.name = profile.name or name
                    break

        # Headline
        headline_selectors = [
            '.top-card-layout__headline',
            'h2.top-card-layout__headline',
            '.text-body-medium',
            '.top-card__subline',
        ]
        for selector in headline_selectors:
            elem = soup.select_one(selector)
            if elem:
                headline = elem.get_text(strip=True)
                if len(headline) > 5 and len(headline) < 500:
                    profile.headline = profile.headline or headline
                    break

        # Location
        location_selectors = [
            '.top-card-layout__first-subline',
            '.top-card__subline-item',
            '.profile-info-subheader',
        ]
        for selector in location_selectors:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text(strip=True)
                # Look for location patterns
                if any(x in text.lower() for x in ['germany', 'berlin', 'munich', 'frankfurt', 'hamburg', 'deutschland']):
                    profile.location = profile.location or text
                    break

        # Summary/About
        summary_selectors = [
            '.core-section-container__content p',
            '.pv-about__summary-text',
            '#about ~ .display-flex p',
            'section.summary p',
        ]
        for selector in summary_selectors:
            elems = soup.select(selector)
            for elem in elems:
                text = elem.get_text(strip=True)
                if len(text) > 50:
                    profile.summary = profile.summary or text[:2000]
                    break

        return profile

    def _extract_from_meta(self, soup: BeautifulSoup, profile: LinkedInProfile) -> LinkedInProfile:
        """Extract data from meta tags."""

        # Title often contains name and headline
        title = soup.find('title')
        if title:
            title_text = title.get_text()
            # Pattern: "Name - Title | LinkedIn"
            match = re.match(r'^([^-|]+)\s*[-|]\s*([^|]+)', title_text)
            if match:
                profile.name = profile.name or match.group(1).strip()
                profile.headline = profile.headline or match.group(2).strip()

        # OG tags
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            content = og_title['content']
            if ' - ' in content:
                parts = content.split(' - ', 1)
                profile.name = profile.name or parts[0].strip()
                if len(parts) > 1:
                    profile.headline = profile.headline or parts[1].strip()

        og_description = soup.find('meta', property='og:description')
        if og_description and og_description.get('content'):
            profile.summary = profile.summary or og_description['content'][:2000]

        return profile

    def _detect_signals(self, profile: LinkedInProfile) -> LinkedInProfile:
        """Detect stealth signals and calculate confidence score."""

        text_to_check = ' '.join(filter(None, [
            profile.headline,
            profile.summary,
            profile.name,
        ])).lower()

        # Check for stealth keywords
        for keyword in STEALTH_KEYWORDS:
            if keyword in text_to_check:
                profile.stealth_signals.append(keyword)

        # Check for high-value background
        for company in HIGH_VALUE_COMPANIES:
            if company in text_to_check:
                profile.high_value_background.append(company)

        # Check for founder signals
        for keyword in FOUNDER_KEYWORDS:
            if keyword in text_to_check:
                profile.founder_signals.append(keyword)

        # Check for co-founder seeking signals
        for keyword in COFOUNDER_SEEKING_KEYWORDS:
            if keyword in text_to_check:
                profile.cofounder_signals.append(keyword)
                profile.looking_for_cofounder = True

        # Classify role/function
        profile = self._classify_role(profile, text_to_check)

        # Extract skills from text
        profile = self._extract_skills(profile, text_to_check)

        # Calculate confidence score
        score = 0.0

        # Stealth signals are strong indicators
        if profile.stealth_signals:
            score += min(0.4, len(profile.stealth_signals) * 0.15)

        # Founder keywords
        if profile.founder_signals:
            score += min(0.3, len(profile.founder_signals) * 0.1)

        # High-value background
        if profile.high_value_background:
            score += min(0.2, len(profile.high_value_background) * 0.1)

        # Location in Germany
        if profile.location and any(x in profile.location.lower() for x in ['germany', 'deutschland', 'berlin', 'munich', 'münchen', 'hamburg', 'frankfurt']):
            score += 0.1

        profile.confidence_score = min(1.0, score)

        return profile

    def _classify_role(self, profile: LinkedInProfile, text: str) -> LinkedInProfile:
        """Classify the person's primary role/function."""
        tech_score = sum(1 for k in TECHNICAL_KEYWORDS if k in text)
        biz_score = sum(1 for k in BUSINESS_KEYWORDS if k in text)
        product_score = sum(1 for k in PRODUCT_KEYWORDS if k in text)
        design_score = sum(1 for k in DESIGN_KEYWORDS if k in text)

        profile.is_technical = tech_score >= 2
        profile.is_business = biz_score >= 2
        profile.is_product = product_score >= 2
        profile.is_design = design_score >= 2

        # Determine primary function
        scores = {
            'technical': tech_score,
            'business': biz_score,
            'product': product_score,
            'design': design_score,
        }
        if max(scores.values()) >= 2:
            profile.primary_function = max(scores, key=scores.get)
        else:
            profile.primary_function = 'other'

        return profile

    def _extract_skills(self, profile: LinkedInProfile, text: str) -> LinkedInProfile:
        """Extract skills mentioned in profile text."""
        found_skills = []
        for skill in COMMON_SKILLS:
            # Use word boundary matching for short skills
            if len(skill) <= 3:
                if re.search(rf'\b{re.escape(skill)}\b', text, re.I):
                    found_skills.append(skill)
            else:
                if skill in text:
                    found_skills.append(skill)

        profile.skills = list(set(found_skills))
        return profile

    def _extract_experience(self, soup: BeautifulSoup, profile: LinkedInProfile) -> LinkedInProfile:
        """Extract work experience entries from profile."""
        experiences = []

        # Look for experience section
        exp_section = soup.find('section', {'id': 'experience'}) or \
                      soup.find('section', class_=re.compile(r'experience', re.I))

        if exp_section:
            for item in exp_section.find_all('li', class_=re.compile(r'position|experience', re.I)):
                try:
                    title_elem = item.find(['h3', 'span'], class_=re.compile(r'title', re.I))
                    company_elem = item.find(['h4', 'span'], class_=re.compile(r'company|subtitle', re.I))
                    duration_elem = item.find('span', class_=re.compile(r'date|duration', re.I))

                    if title_elem and company_elem:
                        exp = WorkExperience(
                            title=title_elem.get_text(strip=True),
                            company=company_elem.get_text(strip=True),
                            duration=duration_elem.get_text(strip=True) if duration_elem else None,
                            is_current='present' in (duration_elem.get_text(strip=True).lower() if duration_elem else '')
                        )
                        experiences.append(exp)
                except Exception:
                    continue

        profile.experience = experiences
        return profile

    def scrape_profile(self, url: str) -> Optional[LinkedInProfile]:
        """
        Scrape a single LinkedIn profile.

        Args:
            url: LinkedIn profile URL

        Returns:
            LinkedInProfile or None if failed
        """
        logger.debug(f"Scraping: {url}")

        html = self._fetch_profile(url)
        if not html:
            return None

        profile = self._parse_profile(html, url)

        logger.info(f"Scraped: {profile.name or 'Unknown'} - {profile.headline or 'No headline'}[:50]")

        return profile

    def scrape_profiles(self, urls: List[str]) -> List[LinkedInProfile]:
        """
        Scrape multiple LinkedIn profiles.

        Args:
            urls: List of LinkedIn profile URLs

        Returns:
            List of successfully scraped profiles
        """
        profiles = []

        for i, url in enumerate(urls):
            logger.info(f"Profile {i+1}/{len(urls)}: {url}")

            profile = self.scrape_profile(url)
            if profile:
                profiles.append(profile)

            if i < len(urls) - 1:
                self._delay()

        logger.info(f"Successfully scraped {len(profiles)}/{len(urls)} profiles")

        return profiles


def is_dach_location(location: Optional[str], headline: Optional[str] = None, summary: Optional[str] = None) -> bool:
    """
    Check if profile appears to be based in DACH region (Germany, Austria, Switzerland).

    Args:
        location: Profile location field
        headline: Profile headline (may contain location)
        summary: Profile summary (may contain location hints)

    Returns:
        True if profile appears to be DACH-based
    """
    # Combine all text to check
    texts = [location, headline, summary]
    combined = ' '.join(t.lower() for t in texts if t)

    # Check for DACH location indicators
    for loc in DACH_LOCATIONS:
        if loc in combined:
            return True

    return False


# Alias for backwards compatibility
is_german_location = is_dach_location


class StealthFounderDetector:
    """
    Analyzes LinkedIn profiles to detect potential stealth founders.
    Filters by DACH region (Germany, Austria, Switzerland) by default.
    """

    def __init__(self, min_confidence: float = 0.3, require_german_location: bool = True):
        self.min_confidence = min_confidence
        self.require_german_location = require_german_location  # Actually DACH region

    def is_german(self, profile: LinkedInProfile) -> bool:
        """Check if profile is based in DACH region (Germany, Austria, Switzerland)."""
        return is_dach_location(profile.location, profile.headline, profile.summary)

    def is_stealth_founder(self, profile: LinkedInProfile) -> bool:
        """Check if profile matches stealth founder criteria."""
        # Check confidence threshold
        if profile.confidence_score < self.min_confidence:
            return False

        # Check German location if required
        if self.require_german_location and not self.is_german(profile):
            return False

        return True

    def filter_stealth_founders(self, profiles: List[LinkedInProfile]) -> List[LinkedInProfile]:
        """Filter profiles to only include likely stealth founders."""
        return [p for p in profiles if self.is_stealth_founder(p)]

    def filter_german_only(self, profiles: List[LinkedInProfile]) -> List[LinkedInProfile]:
        """Filter to only Germany-based profiles."""
        return [p for p in profiles if self.is_german(p)]

    def rank_by_confidence(self, profiles: List[LinkedInProfile]) -> List[LinkedInProfile]:
        """Sort profiles by confidence score (highest first)."""
        return sorted(profiles, key=lambda p: p.confidence_score, reverse=True)


def scrape_and_detect(
    urls: List[str],
    min_confidence: float = 0.3,
    delay_range: tuple = (5, 15),
) -> List[LinkedInProfile]:
    """
    Convenience function to scrape profiles and filter stealth founders.

    Args:
        urls: List of LinkedIn profile URLs
        min_confidence: Minimum confidence score to include
        delay_range: Delay between requests

    Returns:
        List of LinkedInProfile objects for likely stealth founders
    """
    scraper = LinkedInProfileScraper(delay_range=delay_range)
    detector = StealthFounderDetector(min_confidence=min_confidence)

    profiles = scraper.scrape_profiles(urls)
    stealth_founders = detector.filter_stealth_founders(profiles)
    ranked = detector.rank_by_confidence(stealth_founders)

    return ranked


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # Test with a sample URL (replace with real URL for testing)
    test_urls = [
        'https://www.linkedin.com/in/example-profile',
    ]

    results = scrape_and_detect(test_urls, min_confidence=0.0)

    for p in results:
        print(f"\n{p.name}")
        print(f"  Headline: {p.headline}")
        print(f"  Location: {p.location}")
        print(f"  Stealth signals: {p.stealth_signals}")
        print(f"  Background: {p.high_value_background}")
        print(f"  Confidence: {p.confidence_score:.2f}")
