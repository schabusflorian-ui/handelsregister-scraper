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
# free-form text (news titles, LinkedIn headlines).
#
# Catalog is DACH-aware: German/Austrian/Swiss patterns are first-class, not
# afterthoughts. English catches global coverage. Sub-sector tags (humanoid,
# dac, agentic, …) let the UI filter at narrower granularity than the top
# categories alone — needed because "robotics" or "climate" alone covers too
# wide a surface area for scouting.
_SECTOR_KEYWORDS = {
    # ---- AI ------------------------------------------------------------
    "ai": re.compile(
        r"\b(a\.?i\.?|artificial intelligence|machine learning|llm|generative|"
        r"künstliche intelligenz|ki\b|deep learning|neural)\b",
        re.I,
    ),
    "agentic": re.compile(r"\b(agent|agentic|agentisch|autonomous agent|multi[- ]agent)\w*", re.I),
    "rag": re.compile(r"\b(rag\b|retrieval[- ]augmented|vector db|embedding)\w*", re.I),
    "llmops": re.compile(r"\b(llmops|mlops|model ops|eval\w* platform|prompt ops)\b", re.I),
    "voice_ai": re.compile(r"\b(voice ai|speech recognition|tts|text[- ]to[- ]speech|conversational ai)\b", re.I),
    "computer_vision": re.compile(r"\b(computer vision|image recognition|visual ai|bildverarbeitung)\b", re.I),
    # ---- Robotics (sub-tagged) -----------------------------------------
    "robotics": re.compile(r"\b(robot|robotic|robotik|cobot|roboter)\w*", re.I),
    "humanoid_robot": re.compile(r"\b(humanoid|bipedal robot|android\b)\w*", re.I),
    "surgical_robot": re.compile(r"\b(surgical robot|chirurgi\w* robot|medical robot)\w*", re.I),
    "agritech_robot": re.compile(r"\b(agritech|agribot|farm robot|landwirt\w* robot)\w*", re.I),
    "logistics_robot": re.compile(r"\b(warehouse robot|logistik robot|amr\b|autonomous mobile)\w*", re.I),
    # ---- Climate / energy (sub-tagged) --------------------------------
    "climate": re.compile(
        r"\b(climate|klima|decarbon|dekarbon|emission|co2|carbon capture|cleantech|net[- ]zero)\w*", re.I
    ),
    "carbon_removal": re.compile(r"\b(carbon removal|cdr\b|dac\b|direct air capture|co2 removal)\w*", re.I),
    "grid": re.compile(r"\b(grid tech|grid optim|stromnetz|smart grid|energy management)\w*", re.I),
    "ev_charging": re.compile(r"\b(ev charg|ladesäul|charging infra|elektromob\w* lad)\w*", re.I),
    "geothermal": re.compile(r"\b(geothermal|geothermie|erdwärme)\w*", re.I),
    "fusion": re.compile(r"\b(fusion energy|kernfusion|nuclear fusion)\b", re.I),
    "hydrogen": re.compile(r"\b(hydrogen|wasserstoff|h2 \w+|electrolyzer|elektrolys)\w*", re.I),
    "solar": re.compile(r"\b(solar|photovolt|pv anlage|solarzelle)\w*", re.I),
    "wind": re.compile(r"\b(wind energy|wind power|windkraft|offshore wind|windenergie)\b", re.I),
    "battery": re.compile(r"\b(battery|batterie|accu|lithium|solid[- ]state|ess\b)\w*", re.I),
    # ---- Hardware / deeptech -------------------------------------------
    "hardware": re.compile(r"\b(hardware|chip|semiconductor|wafer|silicon|halbleiter)\w*", re.I),
    "quantum": re.compile(r"\b(quantum|quanten|qubit|quantum computing)\w*", re.I),
    "photonics": re.compile(r"\b(photonic|photonik|optical computing|silicon photonic)\w*", re.I),
    # ---- Software verticals ---------------------------------------------
    "devtools": re.compile(r"\b(devtools|developer tools|dev experience|dx\b|ide for)\b", re.I),
    "devops": re.compile(r"\b(devops|platform engineering|k8s|kubernetes|infra as code)\b", re.I),
    "security": re.compile(r"\b(cybersecurity|cyber security|infosec|appsec|security startup|siem\b|soc\b)\b", re.I),
    "nocode": re.compile(r"\b(no[- ]code|low[- ]code|visual programming)\b", re.I),
    "vertical_saas": re.compile(r"\b(vertical saas|industry saas)\b", re.I),
    # ---- Life sciences / health ----------------------------------------
    "biotech": re.compile(r"\b(biotech|synbio|crispr|mrna|life sciences|lifesci)\w*", re.I),
    "medtech": re.compile(r"\b(medtech|medical device|digital health|healthtech|telemedizin)\w*", re.I),
    # ---- Mobility --------------------------------------------------------
    "mobility": re.compile(r"\b(mobility|mobilität|ev charg|autonom\w*|elektromob|micromobil)\w*", re.I),
    "aviation": re.compile(r"\b(aviation|aerospace|evtol|air taxi|drone\b|drohne|urban air)\w*", re.I),
    "space": re.compile(r"\b(aerospace|satellite|new ?space|rocket|orbital|raumfahrt)\w*", re.I),
    # ---- Fintech ---------------------------------------------------------
    "fintech": re.compile(r"\b(fintech|neobank|paytech|insurtech|wealthtech|regtech)\w*", re.I),
    "crypto": re.compile(r"\b(crypto|blockchain|web3|defi|zero[- ]knowledge|zk[- ]rollup)\w*", re.I),
    # ---- Industrial / enterprise ----------------------------------------
    "industry_40": re.compile(r"\b(industrie 4\.0|industry 4\.0|iiot|smart factory|industrial iot)\b", re.I),
    "logistics": re.compile(r"\b(logistics tech|supply chain tech|freight tech|logistik tech)\b", re.I),
}


def _sectors_from_text(*texts: Optional[str]) -> List[str]:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return []
    return [tag for tag, pat in _SECTOR_KEYWORDS.items() if pat.search(joined)]


# ---------------------------------------------------------------------------
# Stealth-pattern + exclusion-pattern catalogs
# ---------------------------------------------------------------------------
# Shared between adapt_stealth_founders (static bios) and the transition
# loop. Compile once at module load; both patterns are read-only.
#
# Catalog combines three source types:
#   - English stealth phrases (industry-standard "building something new")
#   - German/DACH phrases (DACH focus — "baue gerade", "arbeite an etwas Neuem")
#   - Emoji indicators (ninja = stealth is a widespread convention)
#
# Harmonic / Evertrace both emphasize that these phrases alone are weak
# (sabbaticals also trigger "something new") — the composite_score +
# cross-source corroboration is what carries actual precision. Here we
# need high recall; the EXCLUSION patterns below carve away the known
# false-positive classes (sabbatical, advisor, parental leave, retired).
STEALTH_PATTERNS = re.compile(
    r"stealth|"
    # English stealth / early-stage phrases
    r"building (something|an?|new|the next)|"
    r"working on (something|a new|an? \w+ (startup|company|venture))|"
    r"up to something|cooking (something|up)|brewing|"
    r"something (exciting|new|interesting|ambitious|big|cool)|"
    r"new (venture|chapter|journey|adventure|project)|"
    r"founder.{0,20}(mode|new|in stealth|at stealth)|"
    r"\b(co[- ]?)?founder (@|at) stealth\b|"
    r"solo[- ]?founder\b|"
    r"indie (hacker|maker|founder)|"
    r"0\s*(?:to|→|->)\s*1|zero to one|day one\b|day 1\b|"
    r"pre[- ](seed|launch|product)|early[- ]stage founder|"
    r"undisclosed|confidential|secret project|"
    r"in beta|coming soon|next chapter|"
    r"on to the next|next thing|next startup|"
    # German / DACH stealth phrases
    r"baue (gerade|etwas|an etwas)|"
    r"baue meine (eigene |nächste )?(firma|startup|company)|"
    r"arbeite an etwas (neuem|neues|spannendem|aufregendem)|"
    r"neues (projekt|startup|unternehmen|abenteuer)|"
    r"starte (etwas neues|ein startup|gerade)|"
    r"gründe gerade|im aufbau|in vorbereitung|"
    r"nächstes (kapitel|projekt|ding)|"
    r"neue reise|neuer weg|"
    r"etwas großes|etwas spannendes|"
    r"in stealth mode|stealth modus|"
    r"pre[- ]launch|vor dem launch|"
    r"stealth[- ]startup|stealth[- ]gründer|"
    # Emoji conventions — ninja = stealth, construction + rocket at start
    r"🥷|🤫|🔭🧪|🚀\s*(baue|building|working)|🏗️\s*(startup|building)",
    re.I,
)

# Harmonic explicit: "sabbaticals and ordinary lateral moves should not be
# read as startup signals". Any match here VETOES the stealth signal.
EXCLUSION_PATTERNS = re.compile(
    r"\b("
    # Time off / breaks
    r"sabbatical|gap year|parental leave|elternzeit|mutterschutz|"
    r"vaterschaftsurlaub|time off|career break|burnout|"
    r"garden leave|freistellung|urlaub\b|"
    # Advisory / passive roles
    r"advisor only|advisor at|angel investor|angel invest|investor only|"
    r"board member|boardmember|non[- ]executive|aufsichtsrat|beirat|"
    # Service work (not building a company)
    r"freelance consultant|freelancer|consultant at|interim\b|"
    r"interim (ceo|cto|coo|cfo|manager)|contract role|"
    r"coaching|mentor at|"
    # Retirement / exit
    r"retired|pensioniert|im ruhestand|exited and|"
    # Education / academia
    r"phd student|mba candidate|researcher at|professor at|dozent|lehrbeauftragt|"
    r"studying at|studium an|teaching at"
    r")\b",
    re.I,
)


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
        "stealth_excluded": 0,  # new: killed by sabbatical/advisor filter
        "transitions_inserted": 0,
        "transitions_skipped": 0,
        "transitions_excluded": 0,  # new: killed by sabbatical/advisor filter
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
        # Exclusion guard before anything else: if headline/current_company
        # looks like a sabbatical/advisor/consulting bio, skip even if an
        # earlier scraper tagged confidence_score high enough to pass the
        # min_confidence filter above. Keeps the stream person-level clean.
        bio_text = " ".join(filter(None, [r["headline"], r["current_company"]]))
        if EXCLUSION_PATTERNS.search(bio_text):
            stats["stealth_excluded"] += 1
            continue
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
    # A "transition" = headline/current_company changed to something matching
    # STEALTH_PATTERNS in the last 365 days. Much stronger signal than a
    # static stealth bio because it tells us this is NEWLY stealth. See
    # module-level STEALTH_PATTERNS + EXCLUSION_PATTERNS for the catalog.
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
        new_val = r["new_value"] or ""
        if not STEALTH_PATTERNS.search(new_val):
            continue
        # Negative-filter guard: sabbatical / advisor / parental leave
        # should not fire as stealth transitions even if the text happens to
        # match an "exciting new chapter" phrase.
        if EXCLUSION_PATTERNS.search(new_val):
            stats["transitions_excluded"] += 1
            continue
        tags = _sectors_from_text(new_val)
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


# ---------------------------------------------------------------------------
# Handelsregister holding/Verwaltungs/i.G. — DACH pre-launch signal
# ---------------------------------------------------------------------------
#
# In DE/AT/CH, founders routinely register a holding or "Verwaltungs"
# shell entity BEFORE the operating company. These are leading indicators:
# someone is preparing to build something, the papers are in motion.
#
# Patterns (case-insensitive):
#   - "Beteiligungs(gesellschaft)?" + legal form (UG, GmbH, AG)
#   - "Verwaltungs(gesellschaft)?" + legal form
#   - "Holding" as a standalone word in the name
#   - "i.G." / "i. G." / "i.Gr." (in Gründung = in formation, pre-op)
#   - "Vermögens(gesellschaft|verwaltung)" (asset-holding structures)
#
# Weighted lower than handelsregister_new because many of these are
# genuine passive holding vehicles (family office, inheritance structure)
# — only the composite_score with 2+ sources elevates these.

# Note: trailing \b is incompatible with patterns that end in `.` (like
# "i.G.") because period isn't a word char — the match would need the
# post-period position to boundary against a word char. Terminal period
# is left OPTIONAL so the \b can anchor on the preceding 'g'.
_HOLDING_PATTERNS = re.compile(
    r"\b("
    r"beteiligungs(gesellschaft|-ug|-gmbh|-ag)?|"
    r"verwaltungs(gesellschaft|-ug|-gmbh|-ag)?|"
    r"holding|"
    r"vermögens(gesellschaft|verwaltung)?|"
    r"vermoegens(gesellschaft|verwaltung)?|"
    r"i\.\s?g\b|i\.\s?gr\b|in gründung|in gruendung"
    r")\b",
    re.I,
)


def adapt_holdings(conn, *, min_seen_date: Optional[str] = None) -> dict:
    """Emit `handelsregister_holding_formed` signals for DACH shell entities.

    Reuses the companies table (no new scraping). Scans every row whose
    name matches a holding/Verwaltungs/i.G. pattern. Idempotent via
    UNIQUE(signal_type, source_url) with the same company_id-based URL
    scheme as adapt_companies, differentiated by prefix.
    """
    cur = conn.cursor()
    where = []
    params: List = []
    if min_seen_date:
        where.append("substr(first_seen_date,1,10) >= ?")
        params.append(min_seen_date)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, name, city, state, website, first_seen_date, "
        "       ai_robotics_score, climate_score, tech_categories, "
        "       startup_classification "
        f"  FROM companies{where_sql}"
    )
    rows = cur.execute(sql, params).fetchall()

    inserted = skipped = non_matching = 0
    for row in rows:
        if not _HOLDING_PATTERNS.search(row["name"] or ""):
            non_matching += 1
            continue
        tags = _company_sector_tags(row)
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, entity_domain,
                 sector_tags, geo_region, raw_payload, confidence
               ) VALUES ('handelsregister_holding_formed', ?, ?, ?, ?, ?, 'DE', ?, 65)""",
            (
                row["first_seen_date"] or iso_now(),
                f"handelsregister://holding/{row['id']}",
                row["name"],
                normalize_domain(row["website"]),
                ensure_json(tags),
                ensure_json(
                    {
                        "company_id": row["id"],
                        "city": row["city"],
                        "state": row["state"],
                        "classification": row["startup_classification"],
                        "shell_type": _classify_shell(row["name"]),
                    }
                ),
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    logger.info("holdings adapter: inserted=%d skipped=%d non_matching=%d", inserted, skipped, non_matching)
    return {"inserted": inserted, "skipped": skipped, "non_matching": non_matching}


def _classify_shell(name: str) -> str:
    """Which shell-type is this? beteiligungs | verwaltungs | holding | i_g | vermoegens | other"""
    n = (name or "").lower()
    if "beteiligungs" in n:
        return "beteiligungs"
    if "verwaltungs" in n:
        return "verwaltungs"
    if "holding" in n:
        return "holding"
    if "vermögens" in n or "vermoegens" in n:
        return "vermoegens"
    if re.search(r"\bi\.\s?g\.?", n) or "in gründung" in n or "in gruendung" in n:
        return "i_g"
    return "other"


def run_all(conn) -> dict:
    """Convenience: run the adapters in dependency-free order."""
    from .officer_adapter import adapt_officers

    return {
        "companies": adapt_companies(conn),
        "holdings": adapt_holdings(conn),
        "news": adapt_news(conn),
        "stealth_founders": adapt_stealth_founders(conn),
        "officers": adapt_officers(conn),
    }


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--only",
        choices=["companies", "holdings", "news", "stealth_founders", "officers", "all"],
        default="all",
    )
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
