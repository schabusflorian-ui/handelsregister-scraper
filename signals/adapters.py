"""
Adapters — reshape existing tables into `signals` rows.

Each adapter is idempotent (uses `INSERT OR IGNORE` keyed on
`UNIQUE(signal_type, source_url)`) and reports how many rows it emitted.
Running all three on first boot populates `signals` without any new scraping;
re-running them later is cheap and only adds net-new rows.

The three sources we already have:

1. `companies.first_seen_date`  → `handelsregister_new`
2. `news_articles`              → `news_mention`
3. `stealth_founders`           → `linkedin_stealth`
   + `founder_history` deltas   → `linkedin_transition`
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import List, Optional

from .common import ensure_json, iso_now, normalize_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sector-tag derivation
# ---------------------------------------------------------------------------


def _company_sector_tags(row: sqlite3.Row) -> List[str]:
    """Derive sector_tags from the companies columns we have."""
    tags: List[str] = []
    try:
        cat = json.loads(row["tech_categories"] or "[]")
        if isinstance(cat, list):
            tags.extend(c for c in cat if isinstance(c, str))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if row["ai_robotics_score"] and row["ai_robotics_score"] >= 2:
        tags.append("ai_robotics")
    if row["climate_score"] and row["climate_score"] >= 2:
        tags.append("climate")
    # dedupe, preserve order
    seen = set()
    out = []
    for t in tags:
        if t and t not in seen:
            out.append(t)
            seen.add(t)
    return out


# Keyword vocabularies, used across adapters to extract sector hints from
# free-form text (news titles, LinkedIn headlines)
_SECTOR_KEYWORDS = {
    "robotics": re.compile(r"\b(robot|robotic|robotik|cobot|roboter)\w*", re.I),
    "ai": re.compile(
        r"\b(a\.?i\.?|artificial intelligence|machine learning|llm|generative|künstliche intelligenz)\b", re.I
    ),
    "climate": re.compile(r"\b(climate|klima|decarbon|dekarbon|emission|co2|carbon capture|cleantech)\w*", re.I),
    "hardware": re.compile(r"\b(hardware|chip|semiconductor|wafer|silicon)\w*", re.I),
    "battery": re.compile(r"\b(battery|batterie|accu|lithium|solid[- ]state)\w*", re.I),
    "solar": re.compile(r"\b(solar|photovolt|pv anlage)\w*", re.I),
    "wind": re.compile(r"\b(wind energy|wind power|windkraft|offshore wind)\b", re.I),
    "hydrogen": re.compile(r"\b(hydrogen|wasserstoff|h2 \w+)\b", re.I),
    "mobility": re.compile(r"\b(mobility|mobilität|ev charg|autonom\w*|elektromob)\w*", re.I),
    "biotech": re.compile(r"\b(biotech|synbio|crispr|mrna|life sciences)\w*", re.I),
    "fintech": re.compile(r"\b(fintech|neobank|paytech|insurtech)\w*", re.I),
    "space": re.compile(r"\b(aerospace|satellite|new ?space|rocket|orbital)\w*", re.I),
}


def _sectors_from_text(*texts: Optional[str]) -> List[str]:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return []
    return [tag for tag, pat in _SECTOR_KEYWORDS.items() if pat.search(joined)]


# ---------------------------------------------------------------------------
# 1. companies → handelsregister_new
# ---------------------------------------------------------------------------


def adapt_companies(conn, *, min_seen_date: Optional[str] = None, require_startup_flag: bool = True) -> dict:
    """Emit a `handelsregister_new` signal per sector-relevant company.

    `source_url` key is `handelsregister://company/{id}` so re-runs stay idempotent.
    `observed_at` is the company's `first_seen_date` → recency decay handles
    ranking naturally (older rows score lower).
    """
    cur = conn.cursor()
    where = []
    params: List = []
    if min_seen_date:
        where.append("substr(first_seen_date,1,10) >= ?")
        params.append(min_seen_date)
    if require_startup_flag:
        where.append(
            "(ai_robotics_score >= 2 OR climate_score >= 2 OR startup_score >= 3 OR startup_classification IS NOT NULL)"
        )
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, name, city, state, website, first_seen_date, "
        "       ai_robotics_score, climate_score, tech_categories, "
        "       startup_classification "
        f"  FROM companies{where_sql}"
    )

    rows = cur.execute(sql, params).fetchall()
    inserted = skipped = 0
    for row in rows:
        payload = {
            "company_id": row["id"],
            "city": row["city"],
            "state": row["state"],
            "classification": row["startup_classification"],
        }
        tags = _company_sector_tags(row)
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, entity_domain,
                 sector_tags, geo_region, raw_payload, confidence
               ) VALUES ('handelsregister_new', ?, ?, ?, ?, ?, 'DE', ?, 70)""",
            (
                row["first_seen_date"] or iso_now(),
                f"handelsregister://company/{row['id']}",
                row["name"],
                normalize_domain(row["website"]),
                ensure_json(tags),
                ensure_json(payload),
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    logger.info("companies adapter: inserted=%d skipped=%d", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# 2. news_articles → news_mention
# ---------------------------------------------------------------------------

# Rough entity extraction from a title: pull the first capitalized multi-word
# chunk that isn't a stop phrase. Good enough for a signal — the joiner
# fuzzy-matches, so near-misses still land on the right entity when LinkedIn
# or Handelsregister adds a sharper key later.
_TITLE_STOPWORDS = {
    "the",
    "and",
    "with",
    "for",
    "that",
    "this",
    "from",
    "into",
    "about",
    "startup",
    "startups",
    "founder",
    "founders",
    "ai",
    "robotics",
    "climate",
    "millionen",
    "million",
    "mio",
    "euro",
    "funding",
    "runde",
    "raised",
    "secures",
    "launches",
    "neue",
    "new",
    "nach",
    "mit",
    "bei",
    "in",
    "von",
    "zu",
    "zum",
    "zur",
    "wie",
    "warum",
    "wird",
    "ist",
    "hat",
    "says",
    "report",
    "news",
    "latest",
    "berlin",
    "münchen",
    "hamburg",
    "muenchen",
    "köln",
    "koeln",
    "frankfurt",
    "stuttgart",
    "paris",
    "london",
    "wien",
    "zürich",
    "zurich",
    "amsterdam",
    # German weekdays, months — title-case gets them a lot
    "montag",
    "dienstag",
    "mittwoch",
    "donnerstag",
    "freitag",
    "samstag",
    "sonntag",
    "januar",
    "februar",
    "märz",
    "maerz",
    "april",
    "juni",
    "juli",
    "august",
    "september",
    "oktober",
    "november",
    "dezember",
    # Topic/category words frequently title-cased in German press
    "elektroauto",
    "elektroautos",
    "verkehrswende",
    "lithiumabbau",
    "wasserstoff",
    "klimawandel",
    "energiewende",
    "künstliche",
    "kuenstliche",
    "intelligence",
    "intelligenz",
    "artificial",
    "wirtschaft",
    "politik",
    "regierung",
    "bundestag",
    "europa",
    "deutschland",
    "österreich",
    "oesterreich",
    "schweiz",
    "tech",
    "startup-szene",
    "venture",
    "unicorn",
    # Generic news/finance nouns that cap in German headlines
    "studie",
    "exklusiv",
    "meinung",
    "kommentar",
    "interview",
    "podcast",
    "chef",
    "chefin",
    "ceo",
    "cfo",
    "cto",
    "boss",
    "deal",
    "deals",
    "megadeal",
    "trend",
    "trends",
    "jahr",
    "jahre",
    "woche",
    "monat",
    "quartal",
    "umsatz",
    "gewinn",
    "verlust",
    "aktien",
    "börse",
    "geld",
    "preis",
    "preise",
    "kosten",
    "mobility",
    "lifescience",
    "fintech",
    "biotech",
    "cleantech",
    "deeptech",
    "insurtech",
    "proptech",
    "edtech",
    "medtech",
    # Very common company suffixes if they slip through legal-form stripping
    "corp",
    "inc",
    "ltd",
    "llc",
    "ag",
}

# Funding / launch / stealth-ish verbs that must appear in a title for
# a news_mention to be strong enough to emit. If none present, we skip the
# article — a plain "Startup XYZ interviewed" by itself is too weak as a
# signal and false-matches too much.
_NEWS_FUNDING_PATTERNS = re.compile(
    r"\b("
    r"raised|raises|secures|secured|closes?|closing|funding|funded|"
    r"series\s+[a-z]|seed(?:ed)?|pre-seed|runde|finanzierung|funding\s+round|"
    r"launches?|launching|launched|debut|unveils?|unveiled|"
    r"stealth|emerges?|coming\s+out\s+of|exits\s+stealth|"
    r"gründet|gegründet|startet|startup|neues|erste[sr]?\s+produkt|"
    r"acquires?|acquired|acquisition"
    r")\b",
    re.I,
)

# Regex is permissive on token length (1+ after capital) so "AI", "ML", "VC"
# token second words are captured. Combined with the stopword list + the
# single-token CamelCase guard in _guess_entity, this stays conservative.
_TITLE_ENTITY = re.compile(r"\b([A-Z][\w&\.\-]{1,}(?:\s+[A-Z][\w&\.\-]{1,}){0,3})\b")


def _guess_entity(title: str) -> Optional[str]:
    """Best-effort entity extraction from a news title.

    Rules:
      - Multi-token candidates ("Peec AI", "Open Source Robotics") are
        accepted as-is as long as at least one token isn't a stopword.
        Stopwords inside the phrase ("AI", "ML") are kept — they're part
        of the brand name.
      - Single-token candidates are accepted only when brand-y: CamelCase
        in the base word, or contains a digit.
      - Plain single Capitalized Words ("Berlin", "Studie") are REJECTED.
    """
    if not title:
        return None
    for m in _TITLE_ENTITY.finditer(title):
        cand = m.group(1).strip()
        raw_tokens = cand.split()
        # Must have at least one non-stopword token
        if not any(t.lower() not in _TITLE_STOPWORDS for t in raw_tokens):
            continue
        if len(cand) < 3:
            continue
        if len(raw_tokens) >= 2:
            return cand
        # Single-token: accept only if brand-y. CamelCase AFTER a dash
        # doesn't count ("Startup-Ökosystem" is a hyphenated German word).
        tok = raw_tokens[0]
        if tok.lower() in _TITLE_STOPWORDS:
            continue
        base = tok.split("-", 1)[0]
        has_internal_upper = any(c.isupper() for c in base[1:])
        has_digit = any(c.isdigit() for c in tok)
        if has_internal_upper or has_digit:
            return tok
        continue
    return None


def adapt_news(conn, *, min_date: Optional[str] = None, early_stage_only: bool = True) -> dict:
    cur = conn.cursor()
    where = []
    params: List = []
    if early_stage_only:
        where.append("(is_early_stage_related = 1 OR is_funding_related = 1)")
    if min_date:
        where.append("substr(published_date,1,10) >= ?")
        params.append(min_date)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT id, url, title, source, published_date, fetched_at, "
        f"       is_ai_related, is_funding_related, is_early_stage_related "
        f"  FROM news_articles{where_sql}"
    )

    rows = cur.execute(sql, params).fetchall()
    inserted = skipped = no_entity = no_funding_verb = 0
    for row in rows:
        title = row["title"] or ""
        # First guard: require a funding/launch/stealth verb in the title.
        # Without one we're just looking at generic news and the extracted
        # entity false-matches too much.
        if not _NEWS_FUNDING_PATTERNS.search(title):
            no_funding_verb += 1
            continue
        entity = _guess_entity(title)
        if not entity:
            no_entity += 1
            continue
        tags = _sectors_from_text(title)
        if row["is_ai_related"] and "ai" not in tags:
            tags.append("ai")
        # Confidence boost when the news is also AI-flagged OR specifically
        # funding (vs. just "launches"): both suggest a real startup event.
        conf = 60.0 if row["is_funding_related"] else 45.0
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, sector_tags, raw_payload, confidence
               ) VALUES ('news_mention', ?, ?, ?, ?, ?, ?)""",
            (
                row["published_date"] or row["fetched_at"] or iso_now(),
                row["url"],
                entity,
                ensure_json(tags),
                ensure_json({"source": row["source"], "title": title}),
                conf,
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    logger.info(
        "news adapter: inserted=%d skipped=%d no_entity=%d no_funding_verb=%d",
        inserted,
        skipped,
        no_entity,
        no_funding_verb,
    )
    return {
        "inserted": inserted,
        "skipped": skipped,
        "no_entity": no_entity,
        "no_funding_verb": no_funding_verb,
    }


# ---------------------------------------------------------------------------
# 3. stealth_founders → linkedin_stealth  (+ linkedin_transition from history)
# ---------------------------------------------------------------------------


def adapt_stealth_founders(conn, *, min_confidence: float = 0.3) -> dict:
    """Base LinkedIn-stealth signal per founder, plus the stronger
    linkedin_transition signal whenever founder_history records that this
    person recently *went* stealth (field=headline/current_company,
    new_value matches stealth patterns, within last 365d).
    """
    cur = conn.cursor()
    stats = {
        "stealth_inserted": 0,
        "stealth_skipped": 0,
        "transitions_inserted": 0,
        "transitions_skipped": 0,
    }

    # ---- base LinkedIn stealth ----
    rows = cur.execute(
        """SELECT id, linkedin_url, name, headline, location, current_company,
                  confidence_score, first_seen_at, last_checked_at,
                  sector_tags, geo_region, detection_source
             FROM stealth_founders
            WHERE confidence_score >= ?""",
        (min_confidence,),
    ).fetchall()
    for r in rows:
        tags = _sectors_from_text(r["headline"], r["name"])
        try:
            existing = json.loads(r["sector_tags"] or "[]")
            if isinstance(existing, list):
                for t in existing:
                    if isinstance(t, str) and t and t not in tags:
                        tags.append(t)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, person_name, person_linkedin,
                 sector_tags, geo_region, raw_payload, confidence
               ) VALUES ('linkedin_stealth', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["last_checked_at"] or r["first_seen_at"] or iso_now(),
                r["linkedin_url"] or f"stealth_founder://{r['id']}",
                r["current_company"] or r["name"] or "(stealth)",
                r["name"],
                r["linkedin_url"],
                ensure_json(tags),
                r["geo_region"] or "DE",
                ensure_json(
                    {
                        "founder_id": r["id"],
                        "headline": r["headline"],
                        "location": r["location"],
                        "detection_source": r["detection_source"],
                    }
                ),
                min(100.0, float(r["confidence_score"] or 0.5) * 100.0),
            ),
        )
        if res.rowcount:
            stats["stealth_inserted"] += 1
        else:
            stats["stealth_skipped"] += 1

    # ---- transitions from founder_history ----
    # A "transition" = headline/current_company changed to something containing
    # stealth-indicative text in the last 365 days. Much stronger signal than
    # a static stealth bio because it tells us this is NEWLY stealth.
    stealth_patterns = re.compile(
        r"stealth|building something|new venture|founder.{0,20}(mode|new)|"
        r"undisclosed|in beta|coming soon|something new",
        re.I,
    )
    transition_rows = cur.execute(
        """SELECT fh.id, fh.founder_id, fh.field_name, fh.old_value, fh.new_value,
                  fh.changed_at,
                  sf.linkedin_url, sf.name, sf.current_company, sf.geo_region
             FROM founder_history fh
             JOIN stealth_founders sf ON sf.id = fh.founder_id
            WHERE fh.changed_at >= date('now','-365 days')
              AND fh.field_name IN ('headline','current_company','summary')
              AND fh.new_value IS NOT NULL"""
    ).fetchall()
    for r in transition_rows:
        if not stealth_patterns.search(r["new_value"] or ""):
            continue
        tags = _sectors_from_text(r["new_value"])
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, person_name, person_linkedin,
                 sector_tags, geo_region, raw_payload, confidence
               ) VALUES ('linkedin_transition', ?, ?, ?, ?, ?, ?, ?, ?, 80)""",
            (
                r["changed_at"],
                f"founder_history://{r['id']}",
                r["current_company"] or r["name"] or "(stealth)",
                r["name"],
                r["linkedin_url"],
                ensure_json(tags),
                r["geo_region"] or "DE",
                ensure_json(
                    {
                        "founder_id": r["founder_id"],
                        "field": r["field_name"],
                        "old": r["old_value"],
                        "new": r["new_value"],
                    }
                ),
            ),
        )
        if res.rowcount:
            stats["transitions_inserted"] += 1
        else:
            stats["transitions_skipped"] += 1

    conn.commit()
    logger.info("stealth_founders adapter: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_all(conn) -> dict:
    """Convenience: run the adapters in dependency-free order."""
    from .officer_adapter import adapt_officers

    return {
        "companies": adapt_companies(conn),
        "news": adapt_news(conn),
        "stealth_founders": adapt_stealth_founders(conn),
        "officers": adapt_officers(conn),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--only", choices=["companies", "news", "stealth_founders", "officers", "all"], default="all")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.only == "all":
            out = run_all(conn)
        elif args.only == "officers":
            from .officer_adapter import adapt_officers

            out = {"officers": adapt_officers(conn)}
        else:
            out = {args.only: globals()[f"adapt_{args.only}"](conn)}
        print(json.dumps(out, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
