"""
Website Scraper - Extract structured company data from websites.

Fetches key pages (homepage, about, team, careers) and extracts:
- Company description/purpose
- Technology keywords
- Team members
- Social media links
- Funding/investor mentions
- Job openings count

Can operate in two modes:
1. Heuristic extraction (fast, free)
2. LLM extraction (accurate, requires API key)
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
REQUEST_TIMEOUT = 15


@dataclass
class ScrapedWebsiteData:
    """Structured data extracted from a company website."""
    # Core company info
    description: Optional[str] = None  # What the company does (1-3 sentences)
    tagline: Optional[str] = None  # Short slogan/tagline

    # Technology signals
    tech_keywords: List[str] = field(default_factory=list)
    tech_stack: List[str] = field(default_factory=list)  # Programming languages, frameworks

    # Team
    team_members: List[Dict[str, str]] = field(default_factory=list)  # [{name, role, linkedin}]
    team_size_indicator: Optional[str] = None  # "10-50", "startup", etc.

    # Funding/investors
    investors_mentioned: List[str] = field(default_factory=list)
    funding_mentions: List[str] = field(default_factory=list)  # "Series A", "$5M raised"

    # Social links
    linkedin_url: Optional[str] = None
    twitter_url: Optional[str] = None
    github_url: Optional[str] = None

    # Job market signals
    job_count: int = 0
    job_titles: List[str] = field(default_factory=list)

    # Raw content for LLM processing
    homepage_text: Optional[str] = None
    about_text: Optional[str] = None

    # Metadata
    pages_fetched: List[str] = field(default_factory=list)
    scrape_quality: float = 0.0  # 0-1, how much data we got


# Known tech keywords to look for
TECH_KEYWORDS = {
    # AI/ML
    'artificial intelligence', 'machine learning', 'deep learning', 'neural network',
    'nlp', 'natural language', 'computer vision', 'generative ai', 'llm', 'gpt',
    'transformer', 'reinforcement learning', 'predictive', 'ai-powered',
    # Robotics
    'robotics', 'robotic', 'automation', 'autonomous', 'robot', 'cobot',
    'industrial automation', 'rpa', 'drone', 'uav',
    # Software
    'saas', 'cloud', 'api', 'platform', 'software', 'app', 'mobile',
    # Data
    'big data', 'analytics', 'data science', 'data-driven',
    # Hardware
    'iot', 'sensor', 'embedded', 'hardware', 'edge computing',
    # Industry-specific
    'fintech', 'healthtech', 'medtech', 'cleantech', 'climate tech', 'proptech',
    'insurtech', 'legaltech', 'agritech', 'foodtech', 'biotech', 'edtech',
}

# Tech stack keywords
TECH_STACK_KEYWORDS = {
    'python', 'javascript', 'typescript', 'react', 'node.js', 'golang', 'rust',
    'kubernetes', 'docker', 'aws', 'azure', 'gcp', 'tensorflow', 'pytorch',
    'postgresql', 'mongodb', 'redis', 'elasticsearch',
}

# Known investor names to detect
KNOWN_INVESTORS = {
    # Major VCs
    'sequoia', 'a16z', 'andreessen horowitz', 'accel', 'benchmark', 'greylock',
    'index ventures', 'insight partners', 'general catalyst', 'lightspeed',
    'bessemer', 'founders fund', 'khosla', 'nea', 'battery ventures',
    # European VCs
    'atomico', 'balderton', 'northzone', 'eqt ventures', 'lakestar', 'hv capital',
    'cherry ventures', 'project a', 'earlybird', 'point nine', 'speedinvest',
    'cavalry ventures', 'fly ventures', 'la famiglia', 'visionaries club',
    # German VCs
    'htgf', 'high-tech gründerfonds', 'coparion', 'btov', 'capnamic', 'tengelmann',
    'signals', 'mig capital', 'burda principal', 'vc fonds technologie',
    # Corporate VCs
    'google ventures', 'intel capital', 'salesforce ventures', 'microsoft ventures',
    'samsung next', 'siemens', 'bosch', 'porsche ventures', 'bmw i ventures',
}

# Social media URL patterns
SOCIAL_PATTERNS = {
    'linkedin': re.compile(r'https?://(?:www\.)?linkedin\.com/company/[a-zA-Z0-9\-_]+/?', re.I),
    'twitter': re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[a-zA-Z0-9_]+/?', re.I),
    'github': re.compile(r'https?://(?:www\.)?github\.com/[a-zA-Z0-9\-_]+/?', re.I),
}


def _fetch_page(url: str) -> Optional[Tuple[str, BeautifulSoup]]:
    """Fetch a page and return (text_content, soup)."""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={'User-Agent': USER_AGENT},
        )
        if resp.status_code >= 400:
            return None

        soup = BeautifulSoup(resp.text[:512_000], 'lxml')

        # Remove non-content elements
        for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            tag.decompose()

        text = soup.get_text(separator=' ', strip=True)
        # Normalize whitespace
        text = re.sub(r'\s+', ' ', text)

        return text, soup
    except requests.RequestException as e:
        logger.debug("Failed to fetch %s: %s", url, e)
        return None


def _find_page_urls(soup: BeautifulSoup, base_url: str) -> Dict[str, str]:
    """Find URLs for key pages (about, team, careers, etc.)."""
    pages = {}

    # Patterns to match
    patterns = {
        'about': ['about', 'über uns', 'ueber-uns', 'unternehmen', 'company', 'who-we-are'],
        'team': ['team', 'people', 'founders', 'leadership', 'management', 'about-us'],
        'careers': ['careers', 'jobs', 'karriere', 'stellenangebote', 'work-with-us', 'join'],
        'press': ['press', 'news', 'presse', 'media', 'newsroom', 'blog'],
        'contact': ['contact', 'kontakt', 'imprint', 'impressum'],
    }

    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        text = (a.get_text() or '').lower().strip()

        for page_type, keywords in patterns.items():
            if page_type in pages:
                continue
            for kw in keywords:
                if kw in href or kw in text:
                    full_url = urljoin(base_url, a['href'])
                    # Only accept same-domain links
                    if urlparse(full_url).netloc == urlparse(base_url).netloc:
                        pages[page_type] = full_url
                        break

    return pages


def _extract_description(text: str, soup: BeautifulSoup) -> Tuple[Optional[str], Optional[str]]:
    """Extract company description and tagline."""
    tagline = None
    description = None

    # Try meta description first
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        description = meta['content'].strip()[:500]

    # Try og:description
    if not description:
        og = soup.find('meta', attrs={'property': 'og:description'})
        if og and og.get('content'):
            description = og['content'].strip()[:500]

    # Look for tagline in h1/h2
    for tag in ['h1', 'h2']:
        heading = soup.find(tag)
        if heading:
            h_text = heading.get_text(strip=True)
            if 10 < len(h_text) < 150:
                tagline = h_text
                break

    # If no meta description, try to extract from first paragraph
    if not description:
        for p in soup.find_all('p'):
            p_text = p.get_text(strip=True)
            if len(p_text) > 50:
                description = p_text[:500]
                break

    return description, tagline


def _extract_tech_keywords(text: str) -> Tuple[List[str], List[str]]:
    """Extract technology keywords and tech stack from text."""
    text_lower = text.lower()

    found_keywords = []
    for kw in TECH_KEYWORDS:
        if kw in text_lower:
            found_keywords.append(kw)

    found_stack = []
    for tech in TECH_STACK_KEYWORDS:
        # Use word boundary matching
        if re.search(rf'\b{re.escape(tech)}\b', text_lower):
            found_stack.append(tech)

    return found_keywords, found_stack


def _extract_social_links(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract social media links."""
    links = {}

    for a in soup.find_all('a', href=True):
        href = a['href']
        for platform, pattern in SOCIAL_PATTERNS.items():
            if platform not in links and pattern.match(href):
                links[platform] = href

    return links


def _extract_investors(text: str) -> List[str]:
    """Extract mentioned investors from text."""
    text_lower = text.lower()
    found = []

    for investor in KNOWN_INVESTORS:
        if investor in text_lower:
            # Capitalize properly
            found.append(investor.title())

    return found


def _extract_funding_mentions(text: str) -> List[str]:
    """Extract funding-related mentions."""
    mentions = []

    # Patterns for funding rounds
    patterns = [
        r'(?:raised|secured|closed)\s+(?:€|EUR|\$|USD)?\s*(\d+(?:\.\d+)?)\s*(?:million|mio|m\b)',
        r'series\s+[a-d](?:\s+funding|\s+round)?',
        r'seed\s+(?:funding|round|investment)',
        r'pre-seed',
        r'(?:€|EUR|\$|USD)\s*(\d+(?:\.\d+)?)\s*(?:million|mio|m)\s+(?:funding|investment|raised)',
        r'backed\s+by\s+[\w\s,&]+',
        r'(?:angel|vc|venture)\s+(?:funding|investment|backed)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            # Get the full match context
            for m in re.finditer(pattern, text, re.IGNORECASE):
                context = text[max(0, m.start()-20):m.end()+20].strip()
                if context and context not in mentions:
                    mentions.append(context)

    return mentions[:5]  # Limit to 5


def _extract_team(soup: BeautifulSoup, text: str) -> Tuple[List[Dict], Optional[str]]:
    """Extract team members and size indicators."""
    members = []
    size_indicator = None

    # Look for team size mentions
    size_patterns = [
        r'(\d+)\+?\s*(?:employees|team members|mitarbeiter)',
        r'team\s+of\s+(\d+)',
        r'(?:we are|we\'re)\s+(\d+)\s+(?:people|employees)',
    ]

    for pattern in size_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            size_indicator = f"{m.group(1)}+ employees"
            break

    # Look for structured team data (common patterns)
    # This is basic - could be enhanced with more patterns
    for div in soup.find_all(['div', 'article', 'section'], class_=re.compile(r'team|member|founder|people', re.I)):
        name_elem = div.find(['h3', 'h4', 'strong', 'span'], class_=re.compile(r'name', re.I))
        role_elem = div.find(['p', 'span'], class_=re.compile(r'role|title|position', re.I))

        if name_elem:
            member = {'name': name_elem.get_text(strip=True)}
            if role_elem:
                member['role'] = role_elem.get_text(strip=True)

            # Look for LinkedIn link
            linkedin = div.find('a', href=re.compile(r'linkedin\.com', re.I))
            if linkedin:
                member['linkedin'] = linkedin['href']

            if member['name'] and len(member['name']) > 2:
                members.append(member)

    return members[:20], size_indicator  # Limit to 20 members


def _extract_jobs(soup: BeautifulSoup, text: str) -> Tuple[int, List[str]]:
    """Extract job posting count and titles."""
    titles = []

    # Look for job listing structures
    job_patterns = [
        re.compile(r'job|position|opening|stelle|karriere', re.I),
    ]

    for pattern in job_patterns:
        for elem in soup.find_all(['a', 'h3', 'h4', 'li'], string=pattern):
            title = elem.get_text(strip=True)
            if 5 < len(title) < 100:
                titles.append(title)

    # Also look for job count mentions
    count_match = re.search(r'(\d+)\s*(?:open\s+)?(?:positions?|jobs?|openings?|stellen?)', text, re.I)
    job_count = int(count_match.group(1)) if count_match else len(titles)

    return job_count, titles[:10]


class WebsiteScraper:
    """
    Scrape company websites for structured data.

    Usage:
        scraper = WebsiteScraper()
        data = scraper.scrape('https://example.com')
    """

    def __init__(self, fetch_subpages: bool = True, max_pages: int = 5):
        """
        Args:
            fetch_subpages: Whether to fetch about/team/careers pages
            max_pages: Maximum number of pages to fetch per company
        """
        self.fetch_subpages = fetch_subpages
        self.max_pages = max_pages

    def scrape(self, url: str) -> ScrapedWebsiteData:
        """
        Scrape a company website and extract structured data.

        Args:
            url: Homepage URL

        Returns:
            ScrapedWebsiteData with extracted information
        """
        data = ScrapedWebsiteData()
        all_text = ""

        # Fetch homepage
        result = _fetch_page(url)
        if not result:
            logger.warning("Failed to fetch homepage: %s", url)
            return data

        homepage_text, homepage_soup = result
        data.pages_fetched.append(url)
        data.homepage_text = homepage_text[:10000]  # Store first 10K chars
        all_text += homepage_text + " "

        # Extract from homepage
        data.description, data.tagline = _extract_description(homepage_text, homepage_soup)
        social = _extract_social_links(homepage_soup)
        data.linkedin_url = social.get('linkedin')
        data.twitter_url = social.get('twitter')
        data.github_url = social.get('github')

        # Find and fetch subpages
        if self.fetch_subpages:
            subpages = _find_page_urls(homepage_soup, url)

            for page_type, page_url in list(subpages.items())[:self.max_pages - 1]:
                result = _fetch_page(page_url)
                if result:
                    page_text, page_soup = result
                    data.pages_fetched.append(page_url)
                    all_text += page_text + " "

                    # Store about page text
                    if page_type == 'about':
                        data.about_text = page_text[:10000]
                        # Try to get better description from about page
                        if not data.description or len(data.description) < 100:
                            desc, _ = _extract_description(page_text, page_soup)
                            if desc and len(desc) > len(data.description or ''):
                                data.description = desc

                    # Extract team from team page
                    if page_type == 'team':
                        members, size = _extract_team(page_soup, page_text)
                        if members:
                            data.team_members = members
                        if size:
                            data.team_size_indicator = size

                    # Extract jobs from careers page
                    if page_type == 'careers':
                        count, titles = _extract_jobs(page_soup, page_text)
                        data.job_count = count
                        data.job_titles = titles

                    # Update social links if found
                    social = _extract_social_links(page_soup)
                    data.linkedin_url = data.linkedin_url or social.get('linkedin')
                    data.twitter_url = data.twitter_url or social.get('twitter')
                    data.github_url = data.github_url or social.get('github')

        # Extract from all collected text
        data.tech_keywords, data.tech_stack = _extract_tech_keywords(all_text)
        data.investors_mentioned = _extract_investors(all_text)
        data.funding_mentions = _extract_funding_mentions(all_text)

        # If no team extracted yet, try from all content
        if not data.team_members:
            data.team_members, data.team_size_indicator = _extract_team(homepage_soup, all_text)

        # Calculate scrape quality score
        quality = 0.0
        if data.description:
            quality += 0.3
        if data.tech_keywords:
            quality += 0.2
        if data.team_members:
            quality += 0.2
        if data.linkedin_url:
            quality += 0.1
        if data.investors_mentioned or data.funding_mentions:
            quality += 0.2
        data.scrape_quality = min(quality, 1.0)

        logger.info(
            "Scraped %s: %d pages, quality=%.2f, keywords=%d, team=%d, investors=%d",
            url, len(data.pages_fetched), data.scrape_quality,
            len(data.tech_keywords), len(data.team_members), len(data.investors_mentioned)
        )

        return data


def scrape_for_enrichment(url: str) -> Dict:
    """
    Convenience function to scrape a website and return dict for DB enrichment.

    Returns dict with fields matching the companies table.
    """
    scraper = WebsiteScraper()
    data = scraper.scrape(url)

    enrichment = {}

    # Purpose/description
    if data.description:
        enrichment['purpose'] = data.description

    # Tech keywords - merge with existing
    if data.tech_keywords:
        enrichment['detected_tech_keywords'] = data.tech_keywords

    # Social links
    if data.linkedin_url:
        enrichment['linkedin_url'] = data.linkedin_url
    if data.twitter_url:
        enrichment['twitter_url'] = data.twitter_url

    # Metadata
    enrichment['_scrape_quality'] = data.scrape_quality
    enrichment['_pages_fetched'] = len(data.pages_fetched)
    enrichment['_team_size'] = data.team_size_indicator
    enrichment['_job_count'] = data.job_count
    enrichment['_investors_mentioned'] = data.investors_mentioned
    enrichment['_funding_mentions'] = data.funding_mentions
    enrichment['_raw_homepage'] = data.homepage_text
    enrichment['_raw_about'] = data.about_text

    return enrichment


def generate_llm_prompt(data: ScrapedWebsiteData, company_name: str) -> str:
    """
    Generate a prompt for LLM extraction of structured company data.

    Use this with Claude/GPT to extract detailed information that
    heuristics miss.
    """
    # Combine available text, prioritizing about page
    text = ""
    if data.about_text:
        text += f"ABOUT PAGE:\n{data.about_text[:4000]}\n\n"
    if data.homepage_text:
        text += f"HOMEPAGE:\n{data.homepage_text[:4000]}\n\n"

    prompt = f"""Extract structured company information from the following website content for "{company_name}".

Return a JSON object with these fields (use null if not found):
{{
  "description": "1-2 sentence description of what the company does",
  "industry": "primary industry (e.g., 'AI/ML', 'Fintech', 'Robotics', 'SaaS')",
  "product_type": "what they sell (e.g., 'B2B SaaS', 'API platform', 'Hardware')",
  "target_customers": "who they sell to (e.g., 'Enterprise', 'SMBs', 'Consumers')",
  "technology_focus": ["list", "of", "key", "technologies"],
  "founding_year": 2020,
  "team_size": "estimated team size or range",
  "funding_stage": "Seed/Series A/B/etc if mentioned",
  "investors": ["list", "of", "investor", "names"],
  "headquarters_city": "city name",
  "is_b2b": true,
  "is_ai_company": true,
  "key_differentiator": "what makes them unique"
}}

WEBSITE CONTENT:
{text}

Return only the JSON object, no other text."""

    return prompt


def extract_with_llm(
    data: ScrapedWebsiteData,
    company_name: str,
    llm_client,  # Anthropic or OpenAI client
    model: str = "claude-3-haiku-20240307",
) -> Optional[Dict]:
    """
    Use an LLM to extract structured data from scraped website content.

    Args:
        data: ScrapedWebsiteData from WebsiteScraper
        company_name: Name of the company
        llm_client: Anthropic or OpenAI client instance
        model: Model to use for extraction

    Returns:
        Parsed JSON dict or None if extraction failed
    """
    import json

    prompt = generate_llm_prompt(data, company_name)

    try:
        # Try Anthropic client first
        if hasattr(llm_client, 'messages'):
            response = llm_client.messages.create(
                model=model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            text = response.content[0].text
        # OpenAI client
        elif hasattr(llm_client, 'chat'):
            response = llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
            )
            text = response.choices[0].message.content
        else:
            logger.error("Unknown LLM client type")
            return None

        # Parse JSON response
        # Handle potential markdown code blocks
        if '```json' in text:
            text = text.split('```json')[1].split('```')[0]
        elif '```' in text:
            text = text.split('```')[1].split('```')[0]

        return json.loads(text.strip())

    except Exception as e:
        logger.error("LLM extraction failed: %s", e)
        return None
