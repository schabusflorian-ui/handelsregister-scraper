"""
VC Portfolio Scraper - Extract early-stage companies from investor portfolios.

50+ DACH-focused early-stage funds:
- German VCs (Berlin, Munich, Hamburg)
- Austrian VCs (Vienna)
- Swiss VCs (Zurich, Geneva)
- Pan-European funds with DACH focus
"""

import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# 50+ Early-stage DACH-focused funds with portfolio URLs
DACH_FUNDS = [
    # === GERMAN VCs - Berlin ===
    {"name": "Cherry Ventures", "url": "https://www.cherry.vc/portfolio", "location": "Berlin", "stage": "Seed"},
    {"name": "Point Nine", "url": "https://www.pointnine.com/portfolio", "location": "Berlin", "stage": "Seed"},
    {"name": "Project A", "url": "https://www.project-a.com/portfolio", "location": "Berlin", "stage": "Seed/A"},
    {
        "name": "HV Capital",
        "url": "https://www.hvcapital.com/portfolio",
        "location": "Berlin/Munich",
        "stage": "Seed/A",
    },
    {"name": "Earlybird", "url": "https://earlybird.com/portfolio", "location": "Berlin", "stage": "Seed/A"},
    {"name": "Cavalry Ventures", "url": "https://cavalry.vc/portfolio", "location": "Berlin", "stage": "Pre-Seed/Seed"},
    {"name": "La Famiglia", "url": "https://www.lafamiglia.vc/portfolio", "location": "Berlin", "stage": "Seed"},
    {
        "name": "signals Venture Capital",
        "url": "https://signals.vc/portfolio",
        "location": "Berlin",
        "stage": "Pre-Seed/Seed",
    },
    {"name": "IBB Ventures", "url": "https://www.ibbventures.de/portfolio", "location": "Berlin", "stage": "Seed"},
    {"name": "Atlantic Labs", "url": "https://atlanticlabs.de/portfolio", "location": "Berlin", "stage": "Pre-Seed"},
    {"name": "Fly Ventures", "url": "https://fly.vc/portfolio", "location": "Berlin", "stage": "Pre-Seed/Seed"},
    {"name": "FinLeap", "url": "https://www.finleap.com/companies", "location": "Berlin", "stage": "Seed"},
    {"name": "Capnamic", "url": "https://capnamic.com/portfolio", "location": "Berlin/Cologne", "stage": "Seed/A"},
    {
        "name": "Vorwerk Ventures",
        "url": "https://vorwerkventures.com/portfolio",
        "location": "Berlin",
        "stage": "Seed/A",
    },
    {"name": "Redstone", "url": "https://www.redstone.vc/portfolio", "location": "Berlin", "stage": "Seed/A"},
    {"name": "Target Global", "url": "https://www.targetglobal.vc/portfolio", "location": "Berlin", "stage": "Seed/A"},
    {"name": "Heartcore Capital", "url": "https://heartcore.com/portfolio", "location": "Berlin", "stage": "Seed"},
    {
        "name": "Visionaries Club",
        "url": "https://www.visionariesclub.com/portfolio",
        "location": "Berlin",
        "stage": "Seed/A",
    },
    # === GERMAN VCs - Munich ===
    {"name": "UVC Partners", "url": "https://www.uvcpartners.com/portfolio", "location": "Munich", "stage": "Seed/A"},
    {"name": "Acton Capital", "url": "https://actoncapital.com/portfolio", "location": "Munich", "stage": "Seed/A"},
    {"name": "10x Founders", "url": "https://10xfounders.com/portfolio", "location": "Munich", "stage": "Pre-Seed"},
    {"name": "Bayern Kapital", "url": "https://www.bayernkapital.de/portfolio", "location": "Munich", "stage": "Seed"},
    {
        "name": "Plug and Play Munich",
        "url": "https://www.plugandplaytechcenter.com/munich",
        "location": "Munich",
        "stage": "Pre-Seed",
    },
    {
        "name": "TechFounders",
        "url": "https://www.techfounders.com/portfolio",
        "location": "Munich",
        "stage": "Pre-Seed",
    },
    {"name": "High-Tech Gründerfonds", "url": "https://www.htgf.de/en/portfolio", "location": "Bonn", "stage": "Seed"},
    {"name": "42CAP", "url": "https://www.42cap.com/portfolio", "location": "Munich", "stage": "Pre-Seed/Seed"},
    {
        "name": "SevenVentures",
        "url": "https://www.sevenventures.com/portfolio",
        "location": "Munich",
        "stage": "Growth",
    },
    # === GERMAN VCs - Hamburg ===
    {"name": "Hanse Ventures", "url": "https://hanseventures.com/portfolio", "location": "Hamburg", "stage": "Seed"},
    {"name": "Venture Stars", "url": "https://venturestars.com/portfolio", "location": "Hamburg", "stage": "Pre-Seed"},
    {"name": "Neotas Ventures", "url": "https://www.neotas.ventures/portfolio", "location": "Hamburg", "stage": "Seed"},
    # === AUSTRIAN VCs ===
    {
        "name": "Speedinvest",
        "url": "https://www.speedinvest.com/portfolio",
        "location": "Vienna",
        "stage": "Pre-Seed/Seed",
    },
    {
        "name": "AWS Gründerfonds",
        "url": "https://www.gruenderfonds.at/portfolio",
        "location": "Vienna",
        "stage": "Seed",
    },
    {"name": "Calm/Storm", "url": "https://calmstorm.vc/portfolio", "location": "Vienna", "stage": "Seed"},
    {"name": "Push Ventures", "url": "https://www.push.vc/portfolio", "location": "Vienna", "stage": "Pre-Seed"},
    {"name": "Pioneers Ventures", "url": "https://pioneers.io/ventures", "location": "Vienna", "stage": "Seed"},
    {
        "name": "Venionaire Capital",
        "url": "https://www.venionaire.com/portfolio",
        "location": "Vienna",
        "stage": "Seed",
    },
    {"name": "IST Cube", "url": "https://www.ist-cube.com/portfolio", "location": "Vienna", "stage": "Pre-Seed"},
    {"name": "Apex Ventures", "url": "https://www.apex.ventures/portfolio", "location": "Vienna", "stage": "Seed"},
    # === SWISS VCs ===
    {"name": "Lakestar", "url": "https://www.lakestar.com/portfolio", "location": "Zurich", "stage": "Seed/A"},
    {"name": "btov Partners", "url": "https://www.btov.vc/portfolio", "location": "Zurich/Berlin", "stage": "Seed/A"},
    {"name": "Wingman Ventures", "url": "https://www.wingman.vc/portfolio", "location": "Zurich", "stage": "Seed"},
    {"name": "Redalpine", "url": "https://www.redalpine.com/portfolio", "location": "Zurich", "stage": "Seed/A"},
    {"name": "Verve Ventures", "url": "https://www.verve.vc/portfolio", "location": "Zurich", "stage": "Seed"},
    {
        "name": "Polytech Ventures",
        "url": "https://www.polytechventures.com/portfolio",
        "location": "Zurich",
        "stage": "Seed",
    },
    {
        "name": "Serpentine Ventures",
        "url": "https://www.serpentine.vc/portfolio",
        "location": "Zurich",
        "stage": "Pre-Seed/Seed",
    },
    {"name": "Founderful", "url": "https://www.founderful.com/portfolio", "location": "Zurich", "stage": "Pre-Seed"},
    {"name": "VI Partners", "url": "https://www.vipartners.ch/portfolio", "location": "Zurich", "stage": "Seed/A"},
    {"name": "investiere", "url": "https://www.investiere.ch/startups", "location": "Zurich", "stage": "Seed"},
    # === ACCELERATORS ===
    {"name": "Y Combinator", "url": "https://www.ycombinator.com/companies", "location": "Global", "stage": "Pre-Seed"},
    {"name": "EWOR", "url": "https://ewor.io/portfolio", "location": "Munich/Vienna", "stage": "Pre-Seed"},
    {
        "name": "Techstars Berlin",
        "url": "https://www.techstars.com/portfolio?location=berlin",
        "location": "Berlin",
        "stage": "Pre-Seed",
    },
    {
        "name": "Entrepreneur First Berlin",
        "url": "https://www.joinef.com/companies",
        "location": "Berlin",
        "stage": "Pre-Seed",
    },
    {"name": "APX", "url": "https://apx.vc/portfolio", "location": "Berlin", "stage": "Pre-Seed"},
    {
        "name": "German Accelerator",
        "url": "https://www.germanaccelerator.com/portfolio",
        "location": "Berlin/Munich",
        "stage": "Seed",
    },
    {
        "name": "Startup Wise Guys",
        "url": "https://startupwiseguys.com/portfolio",
        "location": "Berlin",
        "stage": "Pre-Seed",
    },
    {"name": "Antler DACH", "url": "https://www.antler.co/portfolio", "location": "Berlin", "stage": "Pre-Seed"},
    # === PAN-EUROPEAN with DACH focus ===
    {
        "name": "Creandum",
        "url": "https://www.creandum.com/portfolio",
        "location": "Stockholm/Berlin",
        "stage": "Seed/A",
    },
    {
        "name": "Northzone",
        "url": "https://www.northzone.com/portfolio",
        "location": "London/Stockholm",
        "stage": "Seed/A",
    },
    {
        "name": "Index Ventures",
        "url": "https://www.indexventures.com/portfolio",
        "location": "London/SF",
        "stage": "Seed/A",
    },
    {
        "name": "Balderton Capital",
        "url": "https://www.balderton.com/portfolio",
        "location": "London",
        "stage": "Seed/A",
    },
    {"name": "Atomico", "url": "https://www.atomico.com/portfolio", "location": "London", "stage": "Seed/A"},
    {"name": "Felix Capital", "url": "https://www.felixcap.com/portfolio", "location": "London", "stage": "Seed"},
    {"name": "Notion Capital", "url": "https://notion.vc/portfolio", "location": "London", "stage": "Seed/A"},
]


@dataclass
class PortfolioCompany:
    """A company from a VC portfolio."""

    name: str
    website: Optional[str] = None
    description: Optional[str] = None
    stage: Optional[str] = None  # seed, series-a, etc.
    sector: Optional[str] = None
    location: Optional[str] = None
    founders: List[str] = field(default_factory=list)
    source: str = ""  # Which VC portfolio
    source_url: str = ""
    found_at: datetime = field(default_factory=datetime.now)


class PortfolioScraper:
    """Base class for portfolio scrapers."""

    def __init__(self, delay_range: tuple = (2, 5)):
        self.delay_range = delay_range
        self.session = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "darwin", "mobile": False})
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _delay(self):
        time.sleep(random.uniform(*self.delay_range))

    def _fetch(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                return response.text
            logger.warning(f"Failed to fetch {url}: {response.status_code}")
            return None
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
            return None

    def scrape(self) -> List[PortfolioCompany]:
        raise NotImplementedError


class YCombinatorScraper(PortfolioScraper):
    """Scrape Y Combinator's company directory."""

    def scrape(self) -> List[PortfolioCompany]:
        """
        YC has a public API for their company directory.
        We filter for DACH-region companies.
        """
        companies = []
        url = "https://www.ycombinator.com/companies"

        logger.info("Scraping Y Combinator portfolio...")

        # YC uses Algolia - we can search for DACH regions
        regions = ["Germany", "Austria", "Switzerland", "Berlin", "Munich", "Vienna", "Zurich"]

        for region in regions:
            search_url = f"{url}?regions={region}"
            html = self._fetch(search_url)

            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")

            # YC company cards
            for card in soup.find_all("a", class_=re.compile(r"company")):
                try:
                    name = card.find(class_=re.compile(r"name|title"))
                    desc = card.find(class_=re.compile(r"description|tagline"))

                    if name:
                        companies.append(
                            PortfolioCompany(
                                name=name.get_text(strip=True),
                                description=desc.get_text(strip=True) if desc else None,
                                location=region,
                                source="Y Combinator",
                                source_url=urljoin(url, card.get("href", "")),
                            )
                        )
                except Exception as e:
                    logger.debug(f"Error parsing YC card: {e}")

            self._delay()

        logger.info(f"Found {len(companies)} YC companies in DACH region")
        return companies


class EWORScraper(PortfolioScraper):
    """Scrape EWOR portfolio."""

    def scrape(self) -> List[PortfolioCompany]:
        companies = []
        url = "https://ewor.io/portfolio"

        logger.info("Scraping EWOR portfolio...")

        html = self._fetch(url)
        if not html:
            return companies

        soup = BeautifulSoup(html, "html.parser")

        # Find company cards/sections
        for card in soup.find_all(["div", "article"], class_=re.compile(r"portfolio|company|startup")):
            try:
                name_elem = card.find(["h2", "h3", "h4", "a"])
                desc_elem = card.find("p")

                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1:
                        companies.append(
                            PortfolioCompany(
                                name=name,
                                description=desc_elem.get_text(strip=True) if desc_elem else None,
                                source="EWOR",
                                source_url=url,
                            )
                        )
            except Exception as e:
                logger.debug(f"Error parsing EWOR card: {e}")

        logger.info(f"Found {len(companies)} EWOR companies")
        return companies


class CherryVenturesScraper(PortfolioScraper):
    """Scrape Cherry Ventures (Berlin) portfolio."""

    def scrape(self) -> List[PortfolioCompany]:
        companies = []
        url = "https://www.cherry.vc/portfolio"

        logger.info("Scraping Cherry Ventures portfolio...")

        html = self._fetch(url)
        if not html:
            return companies

        soup = BeautifulSoup(html, "html.parser")

        # Find portfolio items
        for item in soup.find_all(["a", "div"], class_=re.compile(r"portfolio|company|grid-item")):
            try:
                name = None
                # Try different selectors
                for selector in ["h2", "h3", "h4", ".name", ".title"]:
                    elem = item.find(selector) if selector.startswith(".") else item.find(selector)
                    if elem:
                        name = elem.get_text(strip=True)
                        break

                if not name and item.name == "a":
                    name = item.get_text(strip=True)

                if name and len(name) > 1 and len(name) < 100:
                    companies.append(
                        PortfolioCompany(
                            name=name,
                            source="Cherry Ventures",
                            source_url=url,
                            location="Berlin",
                        )
                    )
            except Exception as e:
                logger.debug(f"Error parsing Cherry item: {e}")

        # Dedupe by name
        seen = set()
        unique = []
        for c in companies:
            if c.name not in seen:
                seen.add(c.name)
                unique.append(c)

        logger.info(f"Found {len(unique)} Cherry Ventures companies")
        return unique


class PointNineScraper(PortfolioScraper):
    """Scrape Point Nine Capital (Berlin) portfolio."""

    def scrape(self) -> List[PortfolioCompany]:
        companies = []
        url = "https://www.pointnine.com/portfolio"

        logger.info("Scraping Point Nine portfolio...")

        html = self._fetch(url)
        if not html:
            return companies

        soup = BeautifulSoup(html, "html.parser")

        for item in soup.find_all(["div", "a"], class_=re.compile(r"portfolio|company")):
            try:
                name_elem = item.find(["h2", "h3", "h4", "span"])
                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1 and len(name) < 100:
                        companies.append(
                            PortfolioCompany(
                                name=name,
                                source="Point Nine",
                                source_url=url,
                                location="Berlin",
                            )
                        )
            except Exception as e:
                logger.debug(f"Error parsing Point Nine item: {e}")

        # Dedupe
        seen = set()
        unique = [c for c in companies if c.name not in seen and not seen.add(c.name)]

        logger.info(f"Found {len(unique)} Point Nine companies")
        return unique


class SpeedinvestScraper(PortfolioScraper):
    """Scrape Speedinvest (Vienna) portfolio."""

    def scrape(self) -> List[PortfolioCompany]:
        companies = []
        url = "https://www.speedinvest.com/portfolio"

        logger.info("Scraping Speedinvest portfolio...")

        html = self._fetch(url)
        if not html:
            return companies

        soup = BeautifulSoup(html, "html.parser")

        for item in soup.find_all(["div", "a", "article"], class_=re.compile(r"portfolio|company|card")):
            try:
                name_elem = item.find(["h2", "h3", "h4"])
                desc_elem = item.find("p")

                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1 and len(name) < 100:
                        companies.append(
                            PortfolioCompany(
                                name=name,
                                description=desc_elem.get_text(strip=True) if desc_elem else None,
                                source="Speedinvest",
                                source_url=url,
                                location="Vienna",
                            )
                        )
            except Exception as e:
                logger.debug(f"Error parsing Speedinvest item: {e}")

        seen = set()
        unique = [c for c in companies if c.name not in seen and not seen.add(c.name)]

        logger.info(f"Found {len(unique)} Speedinvest companies")
        return unique


class GenericPortfolioScraper(PortfolioScraper):
    """
    Generic scraper that works with most VC portfolio pages.
    Uses heuristics to find company names in common HTML patterns.
    """

    def __init__(self, fund_info: Dict, delay_range: tuple = (2, 5)):
        super().__init__(delay_range)
        self.fund_name = fund_info["name"]
        self.fund_url = fund_info["url"]
        self.fund_location = fund_info.get("location", "")
        self.fund_stage = fund_info.get("stage", "")

    def scrape(self) -> List[PortfolioCompany]:
        companies = []

        logger.info(f"Scraping {self.fund_name}...")

        html = self._fetch(self.fund_url)
        if not html:
            return companies

        soup = BeautifulSoup(html, "html.parser")

        # Strategy 1: Look for portfolio/company sections
        for selector in [
            {"class": re.compile(r"portfolio|company|startup|card|grid-item|logo", re.I)},
            {"class": re.compile(r"item|member|partner", re.I)},
        ]:
            items = soup.find_all(["div", "a", "article", "li"], selector)
            for item in items:
                company = self._extract_company(item)
                if company:
                    companies.append(company)

        # Strategy 2: Look for headings with links
        for heading in soup.find_all(["h2", "h3", "h4"]):
            link = heading.find("a") or heading.find_parent("a")
            if link:
                name = heading.get_text(strip=True)
                if self._is_valid_company_name(name):
                    companies.append(
                        PortfolioCompany(
                            name=name,
                            website=link.get("href") if link.get("href", "").startswith("http") else None,
                            source=self.fund_name,
                            source_url=self.fund_url,
                            location=self.fund_location,
                            stage=self.fund_stage,
                        )
                    )

        # Strategy 3: Look for image alts (logos)
        for img in soup.find_all("img", alt=True):
            alt = img.get("alt", "")
            if self._is_valid_company_name(alt) and "logo" in str(img.get("class", [])).lower():
                companies.append(
                    PortfolioCompany(
                        name=alt,
                        source=self.fund_name,
                        source_url=self.fund_url,
                        location=self.fund_location,
                        stage=self.fund_stage,
                    )
                )

        # Dedupe by name
        seen = set()
        unique = []
        for c in companies:
            key = c.name.lower().strip()
            if key not in seen and len(key) > 1:
                seen.add(key)
                unique.append(c)

        logger.info(f"  Found {len(unique)} companies from {self.fund_name}")
        return unique

    def _extract_company(self, item) -> Optional[PortfolioCompany]:
        """Extract company info from a portfolio item element."""
        name = None
        description = None
        website = None

        # Try to find name
        for tag in ["h2", "h3", "h4", "h5", "strong", "b"]:
            elem = item.find(tag)
            if elem:
                name = elem.get_text(strip=True)
                break

        # Fallback: link text or image alt
        if not name:
            link = item.find("a")
            if link:
                name = link.get_text(strip=True)
                href = link.get("href", "")
                if href.startswith("http"):
                    website = href

        if not name:
            img = item.find("img", alt=True)
            if img:
                name = img.get("alt", "")

        # Validate name
        if not self._is_valid_company_name(name):
            return None

        # Try to find description
        p = item.find("p")
        if p:
            description = p.get_text(strip=True)[:500]

        return PortfolioCompany(
            name=name,
            description=description,
            website=website,
            source=self.fund_name,
            source_url=self.fund_url,
            location=self.fund_location,
            stage=self.fund_stage,
        )

    def _is_valid_company_name(self, name: str) -> bool:
        """Check if a string looks like a valid company name."""
        if not name:
            return False

        name = name.strip()

        # Length checks
        if len(name) < 2 or len(name) > 100:
            return False

        # Skip common non-company strings
        skip_words = [
            "portfolio",
            "companies",
            "our companies",
            "investments",
            "learn more",
            "read more",
            "view all",
            "see all",
            "about",
            "contact",
            "home",
            "menu",
            "close",
            "privacy",
            "terms",
            "cookie",
            "newsletter",
            "linkedin",
            "twitter",
            "facebook",
            "instagram",
        ]
        if name.lower() in skip_words:
            return False

        # Skip if starts with common nav words
        if name.lower().startswith(("back to", "go to", "click", "view")):
            return False

        return True


class AllPortfoliosScraper:
    """Scrape all 50+ VC portfolios from DACH_FUNDS list."""

    def __init__(self, delay_between_sources: int = 3, max_funds: int = None):
        self.delay = delay_between_sources
        self.max_funds = max_funds  # Limit for testing

    def scrape_all(self) -> List[PortfolioCompany]:
        """Scrape all portfolios and return combined list."""
        all_companies = []
        funds_to_scrape = DACH_FUNDS[: self.max_funds] if self.max_funds else DACH_FUNDS

        logger.info(f"Scraping {len(funds_to_scrape)} VC portfolios...")

        for i, fund in enumerate(funds_to_scrape):
            try:
                scraper = GenericPortfolioScraper(fund)
                companies = scraper.scrape()
                all_companies.extend(companies)

                # Progress update every 10 funds
                if (i + 1) % 10 == 0:
                    logger.info(
                        f"  Progress: {i + 1}/{len(funds_to_scrape)} funds, {len(all_companies)} companies found"
                    )

                time.sleep(self.delay)
            except Exception as e:
                logger.warning(f"Error with {fund['name']}: {e}")

        # Dedupe across all sources
        seen = set()
        unique = []
        for c in all_companies:
            key = c.name.lower().strip()
            if key not in seen:
                seen.add(key)
                unique.append(c)

        logger.info(f"Total unique companies from all portfolios: {len(unique)}")
        return unique


def save_portfolio_companies(db, companies: List[PortfolioCompany]):
    """Save portfolio companies to database."""
    cursor = db.conn.cursor()

    # Ensure table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            website TEXT,
            description TEXT,
            stage TEXT,
            sector TEXT,
            location TEXT,
            founders TEXT,
            source TEXT,
            source_url TEXT,
            handelsregister_match_id INTEGER REFERENCES companies(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, source)
        )
    """)

    inserted = 0
    for c in companies:
        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO portfolio_companies
                (name, website, description, stage, sector, location, founders, source, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    c.name,
                    c.website,
                    c.description,
                    c.stage,
                    c.sector,
                    c.location,
                    ",".join(c.founders) if c.founders else None,
                    c.source,
                    c.source_url,
                    datetime.now().isoformat(),
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
        except Exception as e:
            logger.debug(f"Error saving {c.name}: {e}")

    db.conn.commit()
    logger.info(f"Saved {inserted} new portfolio companies to database")
    return inserted


def run_portfolio_scraper(db_path: str = "handelsregister.db", max_funds: int = None):
    """Run all portfolio scrapers and save results."""
    from persistence.database import Database

    print("\n=== DACH VC Portfolio Scraper ===")
    print(f"Total funds in database: {len(DACH_FUNDS)}")
    print(f"Funds to scrape: {max_funds or 'all'}\n")

    db = Database(db_path)

    try:
        scraper = AllPortfoliosScraper(max_funds=max_funds)
        companies = scraper.scrape_all()

        print("\n=== Results ===")
        print(f"Found {len(companies)} unique companies\n")

        # Show by source (top 20)
        by_source = {}
        for c in companies:
            by_source[c.source] = by_source.get(c.source, 0) + 1

        print("Companies by fund:")
        for source, count in sorted(by_source.items(), key=lambda x: -x[1])[:20]:
            print(f"  {source}: {count}")

        if len(by_source) > 20:
            print(f"  ... and {len(by_source) - 20} more funds")

        # Save to database
        inserted = save_portfolio_companies(db, companies)
        print(f"\nSaved {inserted} new companies to database")

        return companies

    finally:
        db.close()


def list_funds():
    """List all funds in the database."""
    print(f"\n=== {len(DACH_FUNDS)} DACH Early-Stage Funds ===\n")

    by_location = {}
    for fund in DACH_FUNDS:
        loc = fund["location"].split("/")[0]  # Primary location
        if loc not in by_location:
            by_location[loc] = []
        by_location[loc].append(fund)

    for location, funds in sorted(by_location.items()):
        print(f"\n{location} ({len(funds)} funds):")
        for f in funds:
            print(f"  - {f['name']} ({f['stage']})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape VC portfolio companies")
    parser.add_argument("--list", action="store_true", help="List all funds")
    parser.add_argument("--max", type=int, help="Max funds to scrape (for testing)")
    parser.add_argument("--db", default="handelsregister.db", help="Database path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.list:
        list_funds()
    else:
        run_portfolio_scraper(db_path=args.db, max_funds=args.max)
