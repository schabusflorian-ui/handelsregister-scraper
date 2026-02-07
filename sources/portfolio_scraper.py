"""
VC Portfolio Scraper - Extract early-stage companies from investor portfolios.

Sources:
- Y Combinator (global, many DACH founders)
- EWOR (European, DACH-focused)
- Seedcamp (European)
- Cherry Ventures (Berlin)
- Point Nine (Berlin)
- Earlybird (Berlin/Munich)
- HV Capital (Munich)
- Project A (Berlin)
- Speedinvest (Vienna)
- Lakestar (Zurich)
"""

import logging
import re
import time
import random
import requests
import cloudscraper
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


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
        self.session = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'darwin', 'mobile': False}
        )
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
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
        regions = ['Germany', 'Austria', 'Switzerland', 'Berlin', 'Munich', 'Vienna', 'Zurich']

        for region in regions:
            search_url = f"{url}?regions={region}"
            html = self._fetch(search_url)

            if not html:
                continue

            soup = BeautifulSoup(html, 'html.parser')

            # YC company cards
            for card in soup.find_all('a', class_=re.compile(r'company')):
                try:
                    name = card.find(class_=re.compile(r'name|title'))
                    desc = card.find(class_=re.compile(r'description|tagline'))

                    if name:
                        companies.append(PortfolioCompany(
                            name=name.get_text(strip=True),
                            description=desc.get_text(strip=True) if desc else None,
                            location=region,
                            source='Y Combinator',
                            source_url=urljoin(url, card.get('href', '')),
                        ))
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

        soup = BeautifulSoup(html, 'html.parser')

        # Find company cards/sections
        for card in soup.find_all(['div', 'article'], class_=re.compile(r'portfolio|company|startup')):
            try:
                name_elem = card.find(['h2', 'h3', 'h4', 'a'])
                desc_elem = card.find('p')

                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1:
                        companies.append(PortfolioCompany(
                            name=name,
                            description=desc_elem.get_text(strip=True) if desc_elem else None,
                            source='EWOR',
                            source_url=url,
                        ))
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

        soup = BeautifulSoup(html, 'html.parser')

        # Find portfolio items
        for item in soup.find_all(['a', 'div'], class_=re.compile(r'portfolio|company|grid-item')):
            try:
                name = None
                # Try different selectors
                for selector in ['h2', 'h3', 'h4', '.name', '.title']:
                    elem = item.find(selector) if selector.startswith('.') else item.find(selector)
                    if elem:
                        name = elem.get_text(strip=True)
                        break

                if not name and item.name == 'a':
                    name = item.get_text(strip=True)

                if name and len(name) > 1 and len(name) < 100:
                    companies.append(PortfolioCompany(
                        name=name,
                        source='Cherry Ventures',
                        source_url=url,
                        location='Berlin',
                    ))
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

        soup = BeautifulSoup(html, 'html.parser')

        for item in soup.find_all(['div', 'a'], class_=re.compile(r'portfolio|company')):
            try:
                name_elem = item.find(['h2', 'h3', 'h4', 'span'])
                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1 and len(name) < 100:
                        companies.append(PortfolioCompany(
                            name=name,
                            source='Point Nine',
                            source_url=url,
                            location='Berlin',
                        ))
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

        soup = BeautifulSoup(html, 'html.parser')

        for item in soup.find_all(['div', 'a', 'article'], class_=re.compile(r'portfolio|company|card')):
            try:
                name_elem = item.find(['h2', 'h3', 'h4'])
                desc_elem = item.find('p')

                if name_elem:
                    name = name_elem.get_text(strip=True)
                    if name and len(name) > 1 and len(name) < 100:
                        companies.append(PortfolioCompany(
                            name=name,
                            description=desc_elem.get_text(strip=True) if desc_elem else None,
                            source='Speedinvest',
                            source_url=url,
                            location='Vienna',
                        ))
            except Exception as e:
                logger.debug(f"Error parsing Speedinvest item: {e}")

        seen = set()
        unique = [c for c in companies if c.name not in seen and not seen.add(c.name)]

        logger.info(f"Found {len(unique)} Speedinvest companies")
        return unique


class AllPortfoliosScraper:
    """Scrape all VC portfolios."""

    def __init__(self, delay_between_sources: int = 5):
        self.delay = delay_between_sources
        self.scrapers = [
            YCombinatorScraper(),
            EWORScraper(),
            CherryVenturesScraper(),
            PointNineScraper(),
            SpeedinvestScraper(),
        ]

    def scrape_all(self) -> List[PortfolioCompany]:
        """Scrape all portfolios and return combined list."""
        all_companies = []

        for scraper in self.scrapers:
            try:
                companies = scraper.scrape()
                all_companies.extend(companies)
                time.sleep(self.delay)
            except Exception as e:
                logger.error(f"Error with {scraper.__class__.__name__}: {e}")

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
    cursor.execute('''
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
    ''')

    inserted = 0
    for c in companies:
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO portfolio_companies
                (name, website, description, stage, sector, location, founders, source, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                c.name,
                c.website,
                c.description,
                c.stage,
                c.sector,
                c.location,
                ','.join(c.founders) if c.founders else None,
                c.source,
                c.source_url,
                datetime.now().isoformat(),
            ))
            if cursor.rowcount > 0:
                inserted += 1
        except Exception as e:
            logger.debug(f"Error saving {c.name}: {e}")

    db.conn.commit()
    logger.info(f"Saved {inserted} new portfolio companies to database")
    return inserted


def run_portfolio_scraper(db_path: str = 'handelsregister.db'):
    """Run all portfolio scrapers and save results."""
    from persistence.database import Database

    db = Database(db_path)

    try:
        scraper = AllPortfoliosScraper()
        companies = scraper.scrape_all()

        print(f"\n=== Portfolio Scraper Results ===")
        print(f"Found {len(companies)} unique companies\n")

        # Show by source
        by_source = {}
        for c in companies:
            by_source[c.source] = by_source.get(c.source, 0) + 1

        for source, count in sorted(by_source.items()):
            print(f"  {source}: {count}")

        # Save to database
        inserted = save_portfolio_companies(db, companies)
        print(f"\nSaved {inserted} new companies to database")

        return companies

    finally:
        db.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_portfolio_scraper()
