"""
Cohort scrapers — accelerator/venture-builder announcement sources.

An accelerator picks up a company → that's one of the strongest leading
signals in the stealth-detection playbook (Harmonic calls them "priority
organizations"). Each program's cohort page becomes a signal source
whose output lands as `cohort_announcement` signals, weighted 30
(highest-tier evidence alongside Handelsregister registration).

Framework shape:

    programs = [CohortSource(name="SPRIND", index_url=..., parser=_sprind, weight=30), …]
    for p in programs:
        html = _fetch(p.index_url)
        for entry in p.parser(html, p.index_url):
            emit cohort_announcement signal (idempotent via source_url)

Adding a new program = ~30 lines (one parser function + one config entry).
Each parser is isolated so a broken selector in one program doesn't
cascade. Fetches are rate-limited (2s between programs).

DACH focus: SPRIND (German federal breakthrough agency) runs first,
EWOR + EF-Berlin cohort-filtered next, global sources (YC-diff, Speedrun)
last and tagged with geo for post-hoc filtering.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .common import ensure_json, iso_now, normalize_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry shape — what every parser must return
# ---------------------------------------------------------------------------


@dataclass
class CohortEntry:
    """One company announced by an accelerator cohort."""

    company_name: str  # required
    program_name: str  # SPRIND / EWOR / EF / YC / Speedrun / FR8
    url: Optional[str] = None  # link to the detail page (also acts as signal source_url)
    cohort_name: Optional[str] = None  # e.g. "W2026" for YC, "2026" for EWOR
    description: Optional[str] = None  # one-liner
    founder_names: List[str] = field(default_factory=list)
    sector_tags: List[str] = field(default_factory=list)
    geo_region: Optional[str] = None  # DE/AT/CH/EU/US/…
    website: Optional[str] = None  # company homepage (not accelerator detail page)


# ---------------------------------------------------------------------------
# Fetch helper
# ---------------------------------------------------------------------------


def _fetch(url: str, timeout: int = 20) -> Optional[str]:
    """Simple GET with a browser-ish User-Agent. Returns None on any failure."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36 handelsregister-scraper"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en;q=0.9,de;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        logger.warning("cohort fetch failed %s: %s", url, e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("cohort fetch unexpected %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# SPRIND — German federal breakthrough-innovation agency
# ---------------------------------------------------------------------------

_SPRIND_SLUG_RE = re.compile(
    r'href="(/en/actions/projects/[a-z0-9-]+)"',
    re.I,
)

_SPRIND_DACH_CITIES = {
    "berlin",
    "hamburg",
    "münchen",
    "muenchen",
    "köln",
    "koeln",
    "frankfurt",
    "stuttgart",
    "düsseldorf",
    "duesseldorf",
    "leipzig",
    "aachen",
    "münster",
    "muenster",
    "jena",
    "dresden",
    "nürnberg",
    "wien",
    "vienna",
    "zürich",
    "zurich",
    "basel",
    "lausanne",
    "genf",
    "innsbruck",
    "graz",
    "linz",
}


def _parse_sprind(html: str, index_url: str) -> List[CohortEntry]:
    """SPRIND project directory parser.

    Scans for /en/actions/projects/{slug} hrefs. SPRIND's SSR uses Nuxt UI
    without stable class names, so slug → humanized name is more reliable
    than digging through the DOM. We only get the top ~10 per daily run
    (Load-More is client-JS-driven), but over time the directory rotates
    so coverage extends through repeated runs.
    """
    out: List[CohortEntry] = []
    seen: set = set()
    for m in _SPRIND_SLUG_RE.finditer(html):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        slug = path.rsplit("/", 1)[-1]
        # Humanize slug: "pixel-photonics" → "Pixel Photonics"
        display = " ".join(w.capitalize() for w in slug.split("-"))
        out.append(
            CohortEntry(
                company_name=display,
                program_name="SPRIND",
                url="https://www.sprind.org" + path,
                geo_region="DE",  # SPRIND is German federal — bias DE
                sector_tags=["deeptech", "sprind_project"],
                description="SPRIND funded project",
            )
        )
    return out


# ---------------------------------------------------------------------------
# EWOR — Munich venture builder (fellows + startups)
# ---------------------------------------------------------------------------

_EWOR_STARTUP_RE = re.compile(r'href="(/startups/[a-z0-9-]+)"', re.I)
_EWOR_NAME_FROM_SLUG = re.compile(r"/startups/([a-z0-9-]+)")


def _parse_ewor(html: str, index_url: str) -> List[CohortEntry]:
    """EWOR startup directory. All 50 entries are server-rendered — one
    pass gets them all. Company names derived from slugs because the HTML
    structure is Webflow-generated without stable class names."""
    out: List[CohortEntry] = []
    seen: set = set()
    for m in _EWOR_STARTUP_RE.finditer(html):
        path = m.group(1)
        if path in seen:
            continue
        seen.add(path)
        slug = _EWOR_NAME_FROM_SLUG.search(path).group(1)
        # Humanize slug → "quanted ai" → "Quanted AI"
        display = " ".join(w.capitalize() for w in slug.replace("-", " ").split())
        out.append(
            CohortEntry(
                company_name=display,
                program_name="EWOR",
                url="https://www.ewor.com" + path,
                geo_region="DE",  # EWOR is Munich-HQ, bias cohort toward DACH
                sector_tags=["venture_builder"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Entrepreneur First — DACH filter (Berlin cohort)
# ---------------------------------------------------------------------------

# EF's live URL redirect chain is /companies → /portfolio/ (query string
# filters 403 on direct GET). Parse the full /portfolio/ list and
# post-filter by Berlin mention in the surrounding card text.
_EF_COMPANY_HREF_RE = re.compile(
    r'<a[^>]*href="(/portfolio/[a-z0-9-]+/?)"[^>]*>',
    re.I,
)


def _parse_ef(html: str, index_url: str) -> List[CohortEntry]:
    """EF portfolio parser — emits ALL companies with DACH-aware geo tag
    based on nearby "Berlin" / "Munich" / "DACH" text in each card."""
    out: List[CohortEntry] = []
    seen: set = set()
    for m in _EF_COMPANY_HREF_RE.finditer(html):
        path = m.group(1).rstrip("/") + "/"
        if path in seen:
            continue
        seen.add(path)
        slug = path.strip("/").split("/")[-1]
        display = " ".join(w.capitalize() for w in slug.replace("-", " ").split())
        # Post-filter: check surrounding 400 chars for Berlin / Munich / DACH
        chunk = html[max(0, m.start() - 200) : m.end() + 400].lower()
        geo = None
        if "berlin" in chunk:
            geo = "DE"
            cohort = "Berlin"
        elif "munich" in chunk or "münchen" in chunk:
            geo = "DE"
            cohort = "Munich"
        elif "zurich" in chunk or "zürich" in chunk:
            geo = "CH"
            cohort = "Zurich"
        elif "vienna" in chunk or "wien" in chunk:
            geo = "AT"
            cohort = "Vienna"
        else:
            geo = None
            cohort = None
        out.append(
            CohortEntry(
                company_name=display,
                program_name="Entrepreneur First",
                url="https://www.joinef.com" + path,
                cohort_name=cohort,
                geo_region=geo,
                sector_tags=["accelerator_cohort"],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Program registry
# ---------------------------------------------------------------------------


@dataclass
class CohortProgram:
    name: str
    index_url: str
    parser: Callable[[str, str], List[CohortEntry]]
    enabled: bool = True
    rate_limit_s: float = 2.5


PROGRAMS: List[CohortProgram] = [
    CohortProgram(
        name="SPRIND",
        index_url="https://www.sprind.org/en/actions/projects",
        parser=_parse_sprind,
    ),
    CohortProgram(
        name="EWOR",
        index_url="https://www.ewor.com/our-startups",
        parser=_parse_ewor,
    ),
    # EF is disabled until we wire a Playwright-based fetch:
    # /portfolio/ renders the company list client-side via JavaScript,
    # the WP JSON API is locked down, and plain curl of the page returns
    # "There are no companies matching these filters". Follow-up: either
    # add Playwright to this source, or build a dedicated EF scraper
    # alongside sources/accelerator_scraper.py which already has
    # Playwright infrastructure.
    CohortProgram(
        name="Entrepreneur First",
        index_url="https://www.joinef.com/portfolio/",
        parser=_parse_ef,
        enabled=False,  # requires JS execution; see note above
    ),
    # YC-diff: handled by scheduler.jobs.hn_show_job in diff mode — follow-up.
    # Speedrun: a16z.com/speedrun announcements are irregular, no stable
    #           listing — low priority, follow-up.
    # FR8: Helsinki fellowship with no public cohort list; monitor LinkedIn
    #      only — not scrapable at this layer.
]


# ---------------------------------------------------------------------------
# Signal emission
# ---------------------------------------------------------------------------


def entries_to_signals(entries: List[CohortEntry]) -> List[dict]:
    out = []
    for e in entries:
        out.append(
            {
                "signal_type": "cohort_announcement",
                "observed_at": iso_now(),
                "source_url": e.url or f"cohort://{e.program_name}/{e.company_name}",
                "entity_name": e.company_name,
                "entity_domain": normalize_domain(e.website) if e.website else None,
                "sector_tags": e.sector_tags,
                "geo_region": e.geo_region,
                "confidence": 75.0,
                "raw_payload": {
                    "program": e.program_name,
                    "cohort": e.cohort_name,
                    "description": e.description,
                    "founders": e.founder_names,
                    "website": e.website,
                },
            }
        )
    return out


def write_signals(conn, candidates: List[dict]) -> dict:
    cur = conn.cursor()
    inserted = skipped = 0
    for c in candidates:
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, entity_domain,
                 sector_tags, geo_region, confidence, raw_payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c["signal_type"],
                c["observed_at"],
                c["source_url"],
                c.get("entity_name"),
                c.get("entity_domain"),
                ensure_json(c.get("sector_tags")),
                c.get("geo_region"),
                c.get("confidence", 60.0),
                ensure_json(c.get("raw_payload")),
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _adapt_from_company_ideas(conn) -> dict:
    """Re-use the existing accelerator scrapers' output.

    scheduler.jobs.hn_show_job + sources/accelerator_scraper populate
    company_ideas with ~6k YC + ~240 a16z Speedrun rows (plus Khosla,
    General Catalyst, Lux, Playground Global). Every row IS a
    cohort_announcement — reshape them without re-scraping.

    Idempotent via source_url = cohort_idea://{idea_id}. DACH tagging
    uses company_ideas.country when present.
    """
    cur = conn.cursor()
    # Accelerator programs — anything that's not Reddit/HN self-posts
    accel_programs = (
        "Y Combinator",
        "a16z Speedrun",
        "General Catalyst",
        "Lux Capital",
        "Playground Global",
        "Khosla Ventures",
        "Sequoia Arc",
        "Polsia",
        "IndiePage",
        "Tiny.com",
        "Claude Customers",
    )
    placeholders = ",".join(["?"] * len(accel_programs))
    rows = cur.execute(
        f"""SELECT id, program, company, one_liner, country, year_founded,
                   normalized_website, batch, tags_json
             FROM company_ideas
            WHERE program IN ({placeholders})
              AND company IS NOT NULL AND company != ''""",
        accel_programs,
    ).fetchall()

    sigs: List[dict] = []
    for r in rows:
        country = (r["country"] or "").strip()
        geo = None
        if country.lower() in {"germany", "de", "deutschland"}:
            geo = "DE"
        elif country.lower() in {"austria", "at", "österreich"}:
            geo = "AT"
        elif country.lower() in {"switzerland", "ch", "schweiz", "suisse"}:
            geo = "CH"
        tags = []
        try:
            raw_tags = json.loads(r["tags_json"] or "[]")
            if isinstance(raw_tags, list):
                tags.extend(t for t in raw_tags if isinstance(t, str))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        sigs.append(
            {
                "signal_type": "cohort_announcement",
                "observed_at": iso_now(),
                "source_url": f"cohort_idea://{r['id']}",
                "entity_name": r["company"],
                "entity_domain": r["normalized_website"],
                "sector_tags": tags,
                "geo_region": geo,
                "confidence": 80.0,  # existing cohort scraper = already filtered
                "raw_payload": {
                    "program": r["program"],
                    "cohort": r["batch"],
                    "country": country,
                    "one_liner": r["one_liner"],
                    "year_founded": r["year_founded"],
                    "idea_id": r["id"],
                },
            }
        )
    return write_signals(conn, sigs)


def run(conn) -> dict:
    """One pass across every enabled program + an adapter over the
    existing accelerator scraper's company_ideas table (covers YC,
    a16z Speedrun, Khosla, General Catalyst, Lux, Playground Global
    without any new scraping)."""
    stats = {}
    # First: the HTML/network programs
    for prog in PROGRAMS:
        if not prog.enabled:
            stats[prog.name] = {"skipped_disabled": True}
            continue
        logger.info("cohort source: fetching %s (%s)", prog.name, prog.index_url)
        html = _fetch(prog.index_url)
        if not html:
            stats[prog.name] = {"fetch_failed": True}
            continue
        try:
            entries = prog.parser(html, prog.index_url)
        except Exception as e:  # noqa: BLE001
            logger.exception("cohort parser failed for %s: %s", prog.name, e)
            stats[prog.name] = {"parser_error": str(e)}
            continue
        sigs = entries_to_signals(entries)
        wstats = write_signals(conn, sigs)
        wstats["entries_parsed"] = len(entries)
        stats[prog.name] = wstats
        time.sleep(prog.rate_limit_s)

    # Second: reshape company_ideas (YC + Speedrun + friends) — no network
    logger.info("cohort source: adapting company_ideas (existing accelerator scrape)")
    stats["from_company_ideas"] = _adapt_from_company_ideas(conn)
    return stats


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(run(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
