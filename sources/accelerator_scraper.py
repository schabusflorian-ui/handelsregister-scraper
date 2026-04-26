"""
Startup-idea scrapers for accelerators and thesis funds.

Purpose:
    Collect one row per *company* — with its description, sector tags,
    website, and batch/cohort — to feed a downstream idea-discovery and
    clustering pipeline (embed `one_liner` + `tags` + website copy).
    This is NOT a founder database; founders are stashed in `raw_json`
    only where already public.

Sources implemented:
    - Y Combinator          (sitemap + per-company Inertia JSON blob)
    - General Catalyst      (sitemap + per-company Webflow detail page)
    - Lux Capital           (sitemap + per-company Webflow detail page)
    - Playground Global     (single portfolio page, 48 inline cards)

robots.txt compliance:
    Each scraper only fetches URLs that are explicitly allowed by the
    origin's robots.txt at design time. YC `/companies?*` filtered views
    and GC `/list` are avoided — we use sitemaps + canonical detail URLs
    instead.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Iterable, List, Optional
from urllib.parse import urljoin

OnRecord = Optional[Callable[["CompanyIdea"], None]]

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model — one row per company
# ---------------------------------------------------------------------------

@dataclass
class CompanyIdea:
    """A startup captured from an accelerator / fund portfolio page."""

    program: str                              # "Y Combinator", "General Catalyst", ...
    company: str = ""                         # canonical name
    one_liner: Optional[str] = None           # short pitch / meta description
    long_description: Optional[str] = None    # full paragraph where available
    tags: List[str] = field(default_factory=list)   # sector / industry labels
    company_website: Optional[str] = None
    batch: Optional[str] = None               # "W24", "2021", "Cohort 3" ...
    country: Optional[str] = None
    year_founded: Optional[int] = None
    team_size: Optional[int] = None
    status: Optional[str] = None              # "active" | "acquired" | "public" | "dead"
    source_url: str = ""
    raw: dict = field(default_factory=dict)   # founders, press links, etc.
    scraped_at: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class AcceleratorScraper:
    program_name: str = ""
    crawl_delay: float = 2.0

    def __init__(self, delay_range: tuple = (2, 5), max_records: Optional[int] = None):
        self.delay_range = delay_range
        self.max_records = max_records
        self.session = requests.Session()
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            # NOTE: no explicit Accept header — YC's sitemap route 404s when
            # Accept prefers text/html because content negotiation picks the
            # missing HTML variant. requests defaults to Accept: */* which
            # both XML sitemaps and HTML detail pages serve happily.
        }

    def _delay(self):
        time.sleep(max(self.crawl_delay, random.uniform(*self.delay_range)))

    def _fetch(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, headers=self.headers, timeout=30)
            if r.status_code == 200:
                return r.text
            logger.warning("Fetch %s -> %s", url, r.status_code)
        except Exception as e:  # noqa: BLE001
            logger.warning("Fetch %s errored: %s", url, e)
        return None

    @staticmethod
    def _clean(t: Optional[str]) -> Optional[str]:
        if not t:
            return None
        t = re.sub(r"\s+", " ", t).strip()
        return t or None

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:  # noqa: D401
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Y Combinator  — Inertia-embedded JSON on each /companies/<slug>
# ---------------------------------------------------------------------------

class YCombinatorScraper(AcceleratorScraper):
    program_name = "Y Combinator"
    crawl_delay = 2.0
    BASE = "https://www.ycombinator.com"
    SITEMAP = "https://www.ycombinator.com/companies/sitemap"

    _SLUG_RE = re.compile(r"<loc>https://www\.ycombinator\.com/companies/([a-z0-9][a-z0-9-]+)</loc>")
    _NON_SLUG = {"sitemap", "founders-you-may-know", "women-founders",
                 "black-founders", "hispanic-latino-founders"}

    def _discover_slugs(self) -> List[str]:
        xml = self._fetch(self.SITEMAP)
        if not xml:
            return []
        slugs = []
        for m in self._SLUG_RE.finditer(xml):
            s = m.group(1)
            if s in self._NON_SLUG or "/" in s:
                continue
            slugs.append(s)
        return list(dict.fromkeys(slugs))

    def _parse_company_page(self, slug: str, html_text: str) -> Optional[CompanyIdea]:
        m = re.search(r'data-page="([^"]+)"', html_text)
        if not m:
            return None
        try:
            data = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            return None
        company = data.get("props", {}).get("company") or {}
        if not company.get("name"):
            return None

        status = None
        ycdc = (company.get("ycdc_status") or "").lower()
        if "acquired" in ycdc: status = "acquired"
        elif "public" in ycdc: status = "public"
        elif "dead" in ycdc or "inactive" in ycdc: status = "dead"
        elif ycdc: status = "active"

        founders_raw = company.get("founders") or []
        founders_brief = [
            {
                "name": f.get("full_name") or f.get("name"),
                "title": f.get("title"),
                "linkedin_url": f.get("linkedin_url"),
            }
            for f in founders_raw
        ]

        return CompanyIdea(
            program=self.program_name,
            company=company["name"],
            one_liner=self._clean(company.get("one_liner")),
            long_description=self._clean(company.get("long_description")),
            tags=list(company.get("tags") or []),
            company_website=company.get("website"),
            batch=company.get("batch") or company.get("batch_name"),
            country=company.get("country"),
            year_founded=company.get("year_founded") or None,
            team_size=company.get("team_size") or None,
            status=status,
            source_url=f"{self.BASE}/companies/{slug}",
            raw={
                "slug": slug,
                "city": company.get("city"),
                "founders": founders_brief,
                "linkedin_url": company.get("linkedin_url"),
                "twitter_url": company.get("twitter_url"),
            },
        )

    def scrape(self, slugs: Optional[List[str]] = None, on_record: OnRecord = None) -> List[CompanyIdea]:
        if slugs is None:
            slugs = self._discover_slugs()
        if self.max_records:
            slugs = slugs[: self.max_records]
        logger.info("YC: %d slugs", len(slugs))

        out: List[CompanyIdea] = []
        for i, slug in enumerate(slugs):
            txt = self._fetch(f"{self.BASE}/companies/{slug}")
            if txt:
                rec = self._parse_company_page(slug, txt)
                if rec:
                    out.append(rec)
                    if on_record:
                        on_record(rec)
            if (i + 1) % 50 == 0:
                logger.info("YC: %d/%d fetched, %d records", i + 1, len(slugs), len(out))
            self._delay()
        return out


# ---------------------------------------------------------------------------
# General Catalyst — Webflow CMS pages
# ---------------------------------------------------------------------------

class GeneralCatalystScraper(AcceleratorScraper):
    program_name = "General Catalyst"
    crawl_delay = 2.0
    BASE = "https://www.generalcatalyst.com"
    SITEMAP = "https://www.generalcatalyst.com/sitemap.xml"
    # robots.txt: /list and /internal/ disallowed — we use /companies/<slug> only.

    _SLUG_RE = re.compile(r"<loc>https://www\.generalcatalyst\.com/companies/([a-z0-9][a-z0-9-]+)</loc>")

    def _discover_slugs(self) -> List[str]:
        xml = self._fetch(self.SITEMAP)
        return list(dict.fromkeys(m.group(1) for m in self._SLUG_RE.finditer(xml or "")))

    def _parse_company_page(self, slug: str, html_text: str) -> Optional[CompanyIdea]:
        soup = BeautifulSoup(html_text, "html.parser")
        h1 = soup.select_one("h1.c-page-header__heading")
        if not h1:
            return None
        name = self._clean(h1.get_text())

        # The description is the first non-nav <p> under the h1's ancestor section.
        one_liner = None
        section = h1
        for _ in range(4):
            section = section.parent or section
        for p in section.find_all("p", recursive=True):
            classes = p.get("class") or []
            if "c_navbar_menu_text" in classes or "c_footer__text" in classes:
                continue
            t = self._clean(p.get_text())
            if t and 15 < len(t) < 600:
                one_liner = t
                break

        # Sector tags live under .c-cms-nest-company__sectors. GC inlines them
        # as one concatenated string ("Artificial IntelligenceEnterprise") plus
        # split children, so prefer child elements when present.
        tags: List[str] = []
        sectors_el = soup.select_one("div.c-cms-nest-company__sectors")
        if sectors_el:
            # Only take leaf nodes so we don't get the concatenated parent text.
            for child in sectors_el.find_all(True):
                if child.find(True):  # has children => not a leaf
                    continue
                t = self._clean(child.get_text())
                if t and 2 < len(t) < 60 and t not in tags:
                    tags.append(t)
            # Fallback: if we got nothing (unusual markup), split the concatenated
            # string on camel-case boundaries.
            if not tags:
                raw = self._clean(sectors_el.get_text()) or ""
                tags = [self._clean(t) for t in
                        re.findall(r"[A-Z][a-zA-Z &-]+?(?=[A-Z][a-z]|$)", raw)
                        if self._clean(t)]

        # Company website — external "Website" link (labelled)
        website = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                continue
            if "generalcatalyst.com" in href or "greenhouse.io" in href or "jobs" in href:
                continue
            label = self._clean(a.get_text()) or ""
            if label.lower() in {"website", "visit", "visit site", "visit website"}:
                website = href
                break

        return CompanyIdea(
            program=self.program_name,
            company=name,
            one_liner=one_liner,
            tags=tags,
            company_website=website,
            source_url=f"{self.BASE}/companies/{slug}",
            raw={"slug": slug},
        )

    def scrape(self, slugs: Optional[List[str]] = None, on_record: OnRecord = None) -> List[CompanyIdea]:
        if slugs is None:
            slugs = self._discover_slugs()
        if self.max_records:
            slugs = slugs[: self.max_records]
        logger.info("GC: %d slugs", len(slugs))

        out: List[CompanyIdea] = []
        for i, slug in enumerate(slugs):
            txt = self._fetch(f"{self.BASE}/companies/{slug}")
            if txt:
                rec = self._parse_company_page(slug, txt)
                if rec:
                    out.append(rec)
                    if on_record:
                        on_record(rec)
            if (i + 1) % 50 == 0:
                logger.info("GC: %d/%d fetched, %d records", i + 1, len(slugs), len(out))
            self._delay()
        return out


# ---------------------------------------------------------------------------
# Lux Capital — Webflow pages, description in <meta name="description">
# ---------------------------------------------------------------------------

class LuxCapitalScraper(AcceleratorScraper):
    program_name = "Lux Capital"
    crawl_delay = 2.0
    BASE = "https://www.luxcapital.com"
    SITEMAP = "https://www.luxcapital.com/sitemap.xml"

    _SLUG_RE = re.compile(r"<loc>https://www\.luxcapital\.com/companies/([a-z0-9][a-z0-9-]+)</loc>")

    # On each detail page the hero block precedes an h2 "Similar companies" —
    # we scope parsing to everything before that h2.
    _SIMILAR_HEADER_RE = re.compile(r"similar\s+companies", re.I)

    def _discover_slugs(self) -> List[str]:
        xml = self._fetch(self.SITEMAP)
        return list(dict.fromkeys(m.group(1) for m in self._SLUG_RE.finditer(xml or "")))

    def _hero_scope(self, soup: BeautifulSoup) -> BeautifulSoup:
        # Truncate soup at the "Similar companies" heading if present.
        h2 = soup.find(lambda tag: tag.name in ("h2", "h3") and
                       self._SIMILAR_HEADER_RE.search(tag.get_text() or ""))
        if not h2:
            return soup
        # Extract all siblings/descendants after h2 to detach them
        for el in list(h2.find_all_next()):
            if isinstance(el, Tag):
                el.decompose()
        h2.decompose()
        return soup

    def _parse_company_page(self, slug: str, html_text: str) -> Optional[CompanyIdea]:
        soup = BeautifulSoup(html_text, "html.parser")

        # Description from meta first (Lux puts the one-liner there).
        meta = soup.find("meta", attrs={"name": "description"})
        one_liner = self._clean(meta.get("content") if meta else None)

        soup = self._hero_scope(soup)
        h1 = soup.select_one("h1.company-detail_title")
        if not h1:
            return None
        name = self._clean(h1.get_text())

        # Fallback description: .text-size-medium paragraph in hero
        if not one_liner:
            p = soup.select_one("p.text-size-medium")
            if p:
                one_liner = self._clean(p.get_text())

        # Tags: Lux renders sector labels as bare <p> without classes, in
        # order BEFORE the "Lux investment:" line. Founders appear AFTER that
        # line, so we cut at the first date-bearing paragraph.
        tags: List[str] = []
        year_founded = None
        status = None
        cut = False
        for p in soup.find_all("p"):
            t = self._clean(p.get_text())
            if not t or t == one_liner:
                continue
            if len(t) > 80:
                continue
            low = t.lower()
            if low.startswith("lux investment"):
                m = re.search(r"(\d{4})", t)
                if m:
                    year_founded = year_founded or int(m.group(1))
                cut = True
                continue
            if "publicly listed" in low:
                status = "public"; cut = True; continue
            if "acquired" in low:
                status = "acquired"; cut = True; continue
            if cut:
                # Remaining paragraphs are founders / misc — stop harvesting tags.
                continue
            if re.match(r"^[A-Z][a-zA-Z &/-]+(\s[A-Z][a-zA-Z &/-]+){1,3}$", t):
                if t not in tags:
                    tags.append(t)

        # Company website — external link that isn't social / Lux
        website = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                continue
            if any(s in href for s in ("luxcapital.com", "twitter.com", "x.com",
                                       "linkedin.com", "facebook.com", "youtube.com",
                                       "instagram.com", "apple.com", "google.com")):
                continue
            label = self._clean(a.get_text()) or ""
            if label.lower() in {"website", "visit", "visit site"} or not label:
                website = href
                break

        return CompanyIdea(
            program=self.program_name,
            company=name,
            one_liner=one_liner,
            tags=tags,
            company_website=website,
            status=status,
            source_url=f"{self.BASE}/companies/{slug}",
            raw={"slug": slug, "investment_year_or_founding": year_founded},
        )

    def scrape(self, slugs: Optional[List[str]] = None, on_record: OnRecord = None) -> List[CompanyIdea]:
        if slugs is None:
            slugs = self._discover_slugs()
        if self.max_records:
            slugs = slugs[: self.max_records]
        logger.info("Lux: %d slugs", len(slugs))

        out: List[CompanyIdea] = []
        for i, slug in enumerate(slugs):
            txt = self._fetch(f"{self.BASE}/companies/{slug}")
            if txt:
                rec = self._parse_company_page(slug, txt)
                if rec:
                    out.append(rec)
                    if on_record:
                        on_record(rec)
            if (i + 1) % 50 == 0:
                logger.info("Lux: %d/%d fetched, %d records", i + 1, len(slugs), len(out))
            self._delay()
        return out


# ---------------------------------------------------------------------------
# Playground Global — all data on a single /portfolio page
# ---------------------------------------------------------------------------

class PlaygroundGlobalScraper(AcceleratorScraper):
    program_name = "Playground Global"
    crawl_delay = 2.0
    URL = "https://www.playground.vc/portfolio"

    # Name extraction: Playground doesn't render the company name as text in
    # the card — it's baked into the logo SVG filename as the last token,
    # e.g. ".../<hash>_Agility%20Robotics.svg" -> "Agility Robotics".
    _LOGO_NAME_RE = re.compile(r"_([^/_]+?)\.(?:svg|png|jpg|webp)$", re.I)

    def _card_name(self, card: Tag) -> Optional[str]:
        img = card.find("img", class_="portfolio-logo")
        if img:
            alt = self._clean(img.get("alt") or "")
            if alt and alt.lower() not in {"portfolio logo", "logo"}:
                return alt
            src = img.get("src") or ""
            m = self._LOGO_NAME_RE.search(src)
            if m:
                from urllib.parse import unquote
                return self._clean(unquote(m.group(1)).replace("-", " "))
        for h in ("h3", "h4", "h5"):
            el = card.find(h)
            if el:
                return self._clean(el.get_text())
        return None

    def _card_description(self, card: Tag) -> Optional[str]:
        el = card.select_one(".portfolio-card-text") or card.select_one(".pc-info-wrapper")
        if not el:
            return None
        t = self._clean(el.get_text(" ", strip=True))
        # Strip the trailing "VisitVisit" / "Arrow RightArrow Right" artifacts
        if t:
            t = re.sub(r"\b(Visit(Visit)?|Arrow Right(Arrow Right)?)\b", "", t).strip()
            # Tags often trail as " | AI | Robotics" after the description
            if "|" in t:
                t = t.split("|", 1)[0].strip()
        return t or None

    def _card_tags(self, card: Tag) -> List[str]:
        # Tags live in a nested Webflow dyn-list: .portfolio-filter-target.w-dyn-list
        # with each tag as a .w-dyn-item leaf.
        tag_wrap = card.select_one("div.portfolio-filter-target.w-dyn-list")
        tags: List[str] = []
        if tag_wrap:
            for item in tag_wrap.select(".w-dyn-item"):
                t = self._clean(item.get_text())
                if t and t not in tags:
                    tags.append(t)
        return tags

    def _card_website(self, card: Tag) -> Optional[str]:
        for a in card.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "playground.vc" not in href:
                return href
        return None

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        txt = self._fetch(self.URL)
        if not txt:
            return []
        soup = BeautifulSoup(txt, "html.parser")
        cards = soup.select(".portfolio-item.w-dyn-item")  # only the outermost
        logger.info("Playground: %d outer cards", len(cards))

        seen = set()
        out: List[CompanyIdea] = []
        for c in cards:
            name = self._card_name(c)
            desc = self._card_description(c)
            if not name and not desc:
                continue
            if not name and desc:
                first = desc.split(".")[0]
                if 3 < len(first) < 60:
                    name = first
            key = (name or "").lower()
            if key in seen:
                continue
            if name:
                seen.add(key)
            # Playground renders everything on one URL — append a stable fragment
            # so each row has a unique `source_url` for the DB's UNIQUE constraint.
            website = self._card_website(c)
            anchor = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") \
                     or re.sub(r"[^a-z0-9]+", "-", (website or "").lower()).strip("-")
            rec = CompanyIdea(
                program=self.program_name,
                company=name or "",
                one_liner=desc,
                tags=self._card_tags(c),
                company_website=website,
                source_url=f"{self.URL}#{anchor}" if anchor else self.URL,
                raw={},
            )
            out.append(rec)
            if on_record:
                on_record(rec)
            if self.max_records and len(out) >= self.max_records:
                break
        return out


# ---------------------------------------------------------------------------
# a16z Speedrun — public JSON API (Django REST backend behind speedrun.a16z.com)
# ---------------------------------------------------------------------------

class A16zSpeedrunScraper(AcceleratorScraper):
    """
    Speedrun is a16z's games-and-adjacent games/AI accelerator. The Next.js
    frontend at speedrun.a16z.com calls a public Django REST backend at
    speedrun-be.a16z.com with no auth — we hit that directly.

    One row per company; each company's `industries[]` becomes our `tags`,
    `preamble` becomes a short one-liner, `description` the long pitch.
    """

    program_name = "a16z Speedrun"
    crawl_delay = 1.0  # API, not HTML scraping — be courteous but fast
    API = "https://speedrun-be.a16z.com/api/companies/companies/"
    # The frontend exposes these six cohorts; kept as a fixed list because
    # there's no listing endpoint and we want deterministic coverage.
    COHORTS = ["SR001", "SR002", "SR003", "SR004", "SR005", "SR006"]

    def _parse_row(self, row: dict, cohort: str) -> CompanyIdea:
        industries = [self._clean(i) for i in (row.get("industries") or [])
                      if self._clean(i) and self._clean(i).upper() != "ALL"]
        return CompanyIdea(
            program=self.program_name,
            company=row.get("name") or "",
            one_liner=self._clean(row.get("preamble")),
            long_description=self._clean(row.get("description")),
            tags=industries,
            company_website=row.get("website_url") or None,
            batch=cohort,
            country=row.get("country") or None,
            year_founded=row.get("founded_year") or None,
            team_size=row.get("team_size") or None,
            source_url=f"https://speedrun.a16z.com/companies/{row.get('slug')}",
            raw={
                "id": row.get("id"),
                "slug": row.get("slug"),
                "city": row.get("city"),
                "state": row.get("state"),
                "region": row.get("region"),
                "linkedin_url": row.get("linkedin_url"),
                "x_url": row.get("x_url"),
                "github_url": row.get("github_url"),
                "demo_day_video_url": row.get("demo_day_video_url"),
                "founders": row.get("founder_set") or [],
            },
        )

    def _fetch_cohort(self, cohort: str, on_record: OnRecord = None) -> List[CompanyIdea]:
        out: List[CompanyIdea] = []
        url = f"{self.API}?cohort={cohort}&limit=50&offset=0&ordering=name"
        while url:
            try:
                r = self.session.get(url, headers=self.headers, timeout=30)
            except Exception as e:  # noqa: BLE001
                logger.warning("Speedrun %s: %s", url, e)
                break
            if r.status_code != 200:
                logger.warning("Speedrun %s -> %s", url, r.status_code)
                break
            data = r.json()
            for row in data.get("results") or []:
                rec = self._parse_row(row, cohort)
                out.append(rec)
                if on_record:
                    on_record(rec)
            url = data.get("next")
            self._delay()
        return out

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        out: List[CompanyIdea] = []
        for cohort in self.COHORTS:
            logger.info("Speedrun: fetching cohort %s", cohort)
            out.extend(self._fetch_cohort(cohort, on_record=on_record))
            if self.max_records and len(out) >= self.max_records:
                out = out[: self.max_records]
                break
        logger.info("Speedrun: %d records across %d cohorts", len(out), len(self.COHORTS))
        return out


# ---------------------------------------------------------------------------
# Sequoia Arc — cohort announcement posts
# ---------------------------------------------------------------------------

class SequoiaArcScraper(AcceleratorScraper):
    """
    Sequoia Arc is Sequoia Capital's pre-seed/seed accelerator.

    Important scope note: Arc cohort membership is NOT tagged on
    sequoiacap.com's /our-companies/ portfolio — Arc alumni are folded into
    the main list. The only authoritative source for who was in which Arc
    cohort is Sequoia's cohort-announcement blog posts. Only two such
    posts publish the full lineup in a parseable (h2 + paragraph) form:

      - "Vision, Grit, Growth: Introducing the Next Arc Founders" (US 2021)
      - "The Outlier Founders of Arc Europe" (Europe 2022)

    That gives ~17 named alumni. Other Arc cohort announcements exist but
    are narrative-only and don't enumerate companies. If comprehensive Arc
    coverage is needed later, the right move is to read the cohort
    announcements alongside press releases per-company — not a scraping job.
    """

    program_name = "Sequoia Arc"
    crawl_delay = 2.0

    # (post slug, cohort label) — extend here as new parseable announcements ship
    COHORT_POSTS = [
        ("vision-grit-growth-introducing-the-next-arc-founders",
         "Arc Americas 2022"),
        ("outlier-founders-of-arc-europe",
         "Arc Europe 2022"),
    ]
    BASE = "https://sequoiacap.com/article"

    _SKIP_HEADINGS = {"share", "related articles", "more articles",
                      "related topics", "featured articles", "in the press"}

    def _parse_post(self, slug: str, cohort: str, html_text: str) -> List[CompanyIdea]:
        soup = BeautifulSoup(html_text, "html.parser")
        art = soup.find("article") or soup.find("main") or soup
        out: List[CompanyIdea] = []
        for h in art.find_all(["h2", "h3"]):
            name_raw = self._clean(h.get_text())
            if not name_raw:
                continue
            if name_raw.lower() in self._SKIP_HEADINGS:
                continue
            # "Flagship(fka Vitrine)" -> keep "Flagship" as canonical; note prior name
            prior = None
            m = re.match(r"^(.+?)\s*\(fka\s+([^)]+)\)\s*$", name_raw, re.I)
            if m:
                name, prior = m.group(1).strip(), m.group(2).strip()
            else:
                name = name_raw
            if not (2 < len(name) < 60):
                continue

            # Gather paragraphs until the next h2/h3, pick up external links
            paras: List[str] = []
            links: List[str] = []
            for sib in h.find_next_siblings():
                if getattr(sib, "name", None) in ("h2", "h3"):
                    break
                if getattr(sib, "name", None) == "p":
                    t = self._clean(sib.get_text())
                    if t:
                        paras.append(t)
                if hasattr(sib, "find_all"):
                    for a in sib.find_all("a", href=True):
                        href = a["href"]
                        if (href.startswith("http") and
                                "sequoiacap.com" not in href and
                                "twitter.com" not in href and
                                "linkedin.com" not in href and
                                href not in links):
                            links.append(href)
            if not paras:
                continue
            long_desc = " ".join(paras)
            one_liner = paras[0].split(". ", 1)[0][:300]
            if not one_liner.endswith("."):
                one_liner += "."
            website = links[0] if links else None
            out.append(CompanyIdea(
                program=self.program_name,
                company=name,
                one_liner=one_liner,
                long_description=long_desc,
                tags=[],
                company_website=website,
                batch=cohort,
                country="US" if "Americas" in cohort else (
                    "EU" if "Europe" in cohort else None),
                source_url=f"{self.BASE}/{slug}/#{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
                raw={"post_slug": slug, "prior_name": prior,
                     "external_links": links},
            ))
        return out

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        out: List[CompanyIdea] = []
        for slug, cohort in self.COHORT_POSTS:
            logger.info("Sequoia Arc: %s (%s)", slug, cohort)
            html_text = self._fetch(f"{self.BASE}/{slug}/")
            if not html_text:
                continue
            recs = self._parse_post(slug, cohort, html_text)
            for r in recs:
                out.append(r)
                if on_record:
                    on_record(r)
            self._delay()
        logger.info("Sequoia Arc: %d records across %d posts",
                    len(out), len(self.COHORT_POSTS))
        return out


# ---------------------------------------------------------------------------
# DDL + persistence
# ---------------------------------------------------------------------------

COMPANY_IDEA_DDL = """
CREATE TABLE IF NOT EXISTS company_ideas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    program             TEXT    NOT NULL,
    company             TEXT    NOT NULL,
    one_liner           TEXT,
    long_description    TEXT,
    tags_json           TEXT,
    tags_normalized     TEXT,
    company_website     TEXT,
    normalized_website  TEXT,
    normalized_company  TEXT,
    batch               TEXT,
    country             TEXT,
    year_founded        INTEGER,
    team_size           INTEGER,
    status              TEXT,
    source_url          TEXT,
    raw_json            TEXT,

    -- populated later by matching / clustering jobs
    company_id          INTEGER REFERENCES companies(id),
    embedding_version   TEXT,

    scraped_at          TEXT    NOT NULL,
    created_at          TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(program, source_url)
);
CREATE INDEX IF NOT EXISTS idx_ideas_program  ON company_ideas(program);
CREATE INDEX IF NOT EXISTS idx_ideas_batch    ON company_ideas(program, batch);
CREATE INDEX IF NOT EXISTS idx_ideas_website  ON company_ideas(company_website);
CREATE INDEX IF NOT EXISTS idx_ideas_nweb     ON company_ideas(normalized_website);
CREATE INDEX IF NOT EXISTS idx_ideas_ncompany ON company_ideas(normalized_company);
CREATE INDEX IF NOT EXISTS idx_ideas_country  ON company_ideas(country);
CREATE INDEX IF NOT EXISTS idx_ideas_status   ON company_ideas(status);
"""


# --- normalization helpers --------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"[\s,]+(inc|incorporated|llc|ltd|limited|gmbh|ug|ag|sa|sarl|bv|oy|"
    r"corp|corporation|co|plc|ab|kg|kft|spa|srl|pvt|pty)\.?$",
    re.I,
)


def normalize_website(url: Optional[str]) -> Optional[str]:
    """Return a canonical host form of the URL suitable for dedup / JOIN.

    "https://www.Mistral.AI/" -> "mistral.ai"
    "http://airbnb.com"       -> "airbnb.com"
    Returns None if URL is empty or un-parseable.
    """
    if not url:
        return None
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    try:
        from urllib.parse import urlparse
        host = urlparse(u).netloc
    except Exception:  # noqa: BLE001
        return None
    if not host:
        return None
    host = host.split(":")[0]  # drop port
    if host.startswith("www."):
        host = host[4:]
    return host or None


def normalize_company_name(name: Optional[str]) -> Optional[str]:
    """Lowercase, strip legal suffixes and punctuation for fuzzy dedup."""
    if not name:
        return None
    s = name.strip().lower()
    # Drop trailing legal suffix (possibly repeated: "Foo Inc Ltd")
    for _ in range(2):
        s = _COMPANY_SUFFIXES.sub("", s).strip()
    # Collapse whitespace, strip stray punctuation
    s = re.sub(r"[^\w\s&+/-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def normalize_tag(tag: Optional[str]) -> Optional[str]:
    """Lowercase + trim; join words with single space. Preserves shape for
    vocab cleanup later — we don't try to canonicalise synonyms here."""
    if not tag:
        return None
    s = re.sub(r"\s+", " ", tag).strip().lower()
    return s or None


def save_company_ideas(db, records: List[CompanyIdea]) -> int:
    cur = db.conn.cursor()
    for stmt in COMPANY_IDEA_DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    inserted = 0
    for r in records:
        norm_tags = [t for t in (normalize_tag(x) for x in r.tags) if t]
        try:
            cur.execute(
                """
                INSERT OR IGNORE INTO company_ideas
                (program, company, one_liner, long_description,
                 tags_json, tags_normalized,
                 company_website, normalized_website, normalized_company,
                 batch, country, year_founded, team_size, status,
                 source_url, raw_json, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (r.program, r.company, r.one_liner, r.long_description,
                 json.dumps(r.tags) if r.tags else None,
                 json.dumps(norm_tags) if norm_tags else None,
                 r.company_website,
                 normalize_website(r.company_website),
                 normalize_company_name(r.company),
                 r.batch, r.country, r.year_founded,
                 r.team_size, r.status, r.source_url,
                 json.dumps(r.raw) if r.raw else None,
                 r.scraped_at.isoformat() if isinstance(r.scraped_at, datetime)
                 else str(r.scraped_at)),
            )
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("save %s: %s", r.company, e)
    db.conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# IndiePage — directory of indie founder portfolios (huge solo-founder
# corpus). The /discover page renders a __NEXT_DATA__ blob with up to
# 2,268 startups per fetch; the total claimed is ~5K but the API caps
# the initial render. We accept the cap — 2,268 is already the biggest
# single source in this scraper after YC.
# ---------------------------------------------------------------------------

class IndiePageScraper(AcceleratorScraper):
    program_name = "IndiePage"
    crawl_delay = 2.0
    DISCOVER_URL = "https://indiepa.ge/discover"

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        html_text = self._fetch(self.DISCOVER_URL)
        if not html_text:
            return []
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html_text, re.S,
        )
        if not m:
            logger.warning("IndiePage: no __NEXT_DATA__ script tag")
            return []
        try:
            data = json.loads(m.group(1))
        except Exception as e:  # noqa: BLE001
            logger.warning("IndiePage: bad JSON: %s", e)
            return []
        startups = (data.get("props", {})
                    .get("pageProps", {})
                    .get("startups", []) or [])
        total = (data.get("props", {})
                 .get("pageProps", {})
                 .get("startupsTotal"))
        logger.info("IndiePage: %d startups in payload (total claim=%s)",
                    len(startups), total)

        out: List[CompanyIdea] = []
        for s in startups:
            name = (s.get("name") or "").strip()
            url = (s.get("url") or "").strip() or None
            bio = (s.get("bio") or "").strip() or None
            slug = s.get("_id")
            if not name and not bio:
                continue
            user = s.get("user") or {}
            founder = (user.get("name") or "").strip() if isinstance(user, dict) else ""
            rec = CompanyIdea(
                program=self.program_name,
                company=name,
                one_liner=bio[:300] if bio else None,
                long_description=bio if bio and len(bio) > 300 else None,
                tags=[],
                company_website=url,
                source_url=f"https://indiepa.ge/startup/{slug}" if slug else self.DISCOVER_URL + f"#{name.lower()}",
                raw={
                    "indiepage_id":  s.get("_id"),
                    "votes":          s.get("votesCounter"),
                    "founder":        founder or None,
                    "founder_handle": user.get("username") if isinstance(user, dict) else None,
                },
            )
            out.append(rec)
            if on_record:
                on_record(rec)
        if self.max_records:
            out = out[: self.max_records]
        return out


# ---------------------------------------------------------------------------
# Tiny.com — Andrew Wilkinson's holding-co portfolio. ~75 acquired SaaS /
# digital businesses. Only company name (sometimes) + outbound URL — light
# data, but fills a unique "indie rollup" angle.
# ---------------------------------------------------------------------------

class TinyComScraper(AcceleratorScraper):
    program_name = "Tiny.com"
    crawl_delay = 2.0
    URL = "https://www.tiny.com/companies"

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        html_text = self._fetch(self.URL)
        if not html_text:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        cards = soup.select("a.company-grid-item")
        logger.info("Tiny.com: %d portfolio cards", len(cards))

        out: List[CompanyIdea] = []
        for c in cards:
            href = c.get("href") or ""
            if not href.startswith("http"):
                continue
            from urllib.parse import urlparse
            host = urlparse(href).hostname or ""
            host = host.replace("www.", "")
            # Derive a name from the host (metalab.com -> "Metalab")
            base = host.split(".")[0] if host else ""
            name = base.capitalize() if base else "(unknown)"
            # Try to get richer text from card overlay if present
            overlay = c.select_one(".company-item-overlay")
            blurb = self._clean(overlay.get_text(" ", strip=True)) if overlay else None
            img = c.find("img")
            if img and img.get("alt"):
                alt = self._clean(img.get("alt")) or ""
                # Tiny suffixes alts with " Image" / " Logo" — strip those
                # before treating the alt as a company name.
                alt = re.sub(r"\s+(image|logo)$", "", alt, flags=re.I).strip()
                if alt and len(alt) < 80 and alt.lower() not in {"logo", "company logo"}:
                    name = alt
            rec = CompanyIdea(
                program=self.program_name,
                company=name,
                one_liner=blurb,
                tags=[],
                company_website=href,
                source_url=f"{self.URL}#{base}",
                raw={"tiny_host": host},
            )
            out.append(rec)
            if on_record:
                on_record(rec)
        if self.max_records:
            out = out[: self.max_records]
        return out


# ---------------------------------------------------------------------------
# Anthropic / Claude customers — 39 customer stories on claude.com/customers.
# Each has a company name, vertical, and a problem/outcome write-up.
# ---------------------------------------------------------------------------

class ClaudeCustomersScraper(AcceleratorScraper):
    program_name = "Claude Customers"
    crawl_delay = 2.0
    URL = "https://claude.com/customers"

    def scrape(self, on_record: OnRecord = None) -> List[CompanyIdea]:
        html_text = self._fetch(self.URL)
        if not html_text:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        articles = soup.find_all("article")
        logger.info("Claude customers: %d article cards", len(articles))

        out: List[CompanyIdea] = []
        for art in articles:
            # The visible headline is in a <p> tag inside the card
            # ("Notion is building a workspace for teams and agents").
            # The h3 only contains the company name short ("Notion").
            # We prefer the <p> headline; fall back to h3.
            headline = None
            for p in art.find_all("p"):
                t = self._clean(p.get_text())
                if not t:
                    continue
                low = t.lower()
                if low in {"customer story", "read story", "play video"}:
                    continue
                if 15 <= len(t) <= 200:
                    headline = t
                    break
            if not headline:
                h = art.find(["h2", "h3"])
                if h:
                    headline = self._clean(h.get_text())
            if not headline:
                continue
            # The company name is typically the first word(s) before a verb.
            # Use a heuristic: first " is " or " uses " or " builds " split.
            company = headline
            for split in (" is building", " is using", " uses ", " builds ",
                          " accelerates", " automates", " powers", " transforms"):
                if split in headline.lower():
                    company = headline.split(split, 1)[0].strip()
                    break
            company = company[:80]
            # Detail-page link
            detail_link = None
            for a in art.find_all("a", href=True):
                href = a["href"]
                if "/customers/" in href and href != "/customers":
                    detail_link = href if href.startswith("http") else f"https://claude.com{href}"
                    break
            rec = CompanyIdea(
                program=self.program_name,
                company=company,
                one_liner=headline,
                tags=["ai-native", "claude-customer"],
                company_website=None,  # not on the listing; would need detail fetch
                source_url=detail_link or f"{self.URL}#{re.sub(r'[^a-z0-9]+', '-', company.lower())}",
                raw={"headline": headline},
            )
            out.append(rec)
            if on_record:
                on_record(rec)
        if self.max_records:
            out = out[: self.max_records]
        return out


SCRAPERS = {
    "yc":         YCombinatorScraper,
    "gc":         GeneralCatalystScraper,
    "lux":        LuxCapitalScraper,
    "playground": PlaygroundGlobalScraper,
    "speedrun":   A16zSpeedrunScraper,
    "sequoia_arc": SequoiaArcScraper,
    "indiepage":  IndiePageScraper,
    "tiny":       TinyComScraper,
    "claude":     ClaudeCustomersScraper,
}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    p = argparse.ArgumentParser()
    p.add_argument("--program", required=True, choices=list(SCRAPERS))
    p.add_argument("--max", type=int, default=None,
                   help="Cap records (default: full portfolio)")
    p.add_argument("--slugs", nargs="*",
                   help="Explicit slugs for YC / GC / Lux (skip sitemap)")
    p.add_argument("--jsonl", type=str, default=None,
                   help="Append every record to this JSONL file as it's scraped")
    args = p.parse_args()

    s = SCRAPERS[args.program](max_records=args.max)

    jsonl_fh = open(args.jsonl, "a", buffering=1) if args.jsonl else None

    def _write(rec: CompanyIdea):
        d = asdict(rec)
        d["scraped_at"] = rec.scraped_at.isoformat()
        if jsonl_fh:
            jsonl_fh.write(json.dumps(d, default=str, ensure_ascii=False) + "\n")

    try:
        kwargs = {"on_record": _write}
        if args.slugs and hasattr(s, "_parse_company_page"):
            kwargs["slugs"] = args.slugs
        records = s.scrape(**kwargs)
    finally:
        if jsonl_fh:
            jsonl_fh.close()

    if not args.jsonl:
        preview = records[:5]
        for r in preview:
            d = {k: v for k, v in asdict(r).items()
                 if k != "scraped_at" and v not in (None, "", [], {})}
            print(json.dumps(d, default=str, indent=2))
    print(f"\n=> {len(records)} records from {args.program}" +
          (f" -> {args.jsonl}" if args.jsonl else ""))
