"""
sync_to_sheets — push three key tables to Google Sheets as separate tabs.

Target spreadsheet is identified by GOOGLE_SHEETS_ID; auth via service-account
JSON in GOOGLE_SHEETS_CREDENTIALS (base64-encoded). Both in .env.

Each tab is wiped and rewritten on every run (full replace, not append) so
the sheet always matches the local DB.

Tabs produced:
  ideas_climate_hardware_robotics      — filtered ideas, ~1,100 rows
  companies_climate_hardware_robotics  — filtered German companies, ~1,500 rows
  stealth_founders                     — all stealth LinkedIn profiles, ~1,500+ rows

Usage:
    python3 -m scripts.sync_to_sheets                 # all three tabs
    python3 -m scripts.sync_to_sheets --only stealth  # just one tab
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Queries — one per tab. Each returns (column_order, rows_iterable).
# Reuses the same filter logic as the CSV exporters so Sheets stays in sync.
# ---------------------------------------------------------------------------

IDEAS_MATCH_SQL = """
WITH matches AS (
  SELECT ie.company_idea_id AS id, 'bm=hardware' AS reason
    FROM idea_extraction ie
   WHERE ie.error IS NULL AND ie.business_model='hardware'
  UNION
  SELECT ie.company_idea_id, 'sector:' || v.value
    FROM idea_extraction ie, json_each(ie.sector_tags) v
   WHERE ie.error IS NULL AND (
       v.value IN ('hardware','robotics','robotic','energy',
                   'climate','climate-tech','sustainability-climate',
                   'renewable-energy','cleantech','solar-renewable',
                   'iot-hardware','hardware-form-factor')
       OR v.value LIKE 'robotics-%'
       OR v.value LIKE 'climate-%'
       OR v.value LIKE 'renewable-%'
       OR v.value LIKE '%-energy'
     )
  UNION
  SELECT ie.company_idea_id, 'mech=hardware-form-factor'
    FROM idea_extraction ie, json_each(ie.mechanism_tags) v
   WHERE ie.error IS NULL AND v.value='hardware-form-factor'
  UNION
  SELECT ci.id, 'fts'
    FROM company_ideas ci
   WHERE ci.id IN (
     SELECT rowid FROM company_ideas_fts
      WHERE company_ideas_fts MATCH
        'robot OR robotics OR robotic
         OR climate OR decarbon OR emissions OR "carbon capture"
         OR battery OR solar OR microgrid OR "wind energy"'
   )
)
SELECT id, GROUP_CONCAT(DISTINCT reason) AS match_reasons
  FROM matches
 GROUP BY id
"""


COMPANIES_MATCH_SQL = """
WITH matches AS (
  SELECT companies.id AS id, 'cat:' || v.value AS reason
    FROM companies, json_each(companies.tech_categories) v
   WHERE v.value IN ('robotics','climate_tech','industry_40',
                     'deeptech','computer_vision')
  UNION SELECT id, 'ai_robotics_score>=3' FROM companies WHERE ai_robotics_score >= 3
  UNION SELECT id, 'climate_score>=3'     FROM companies WHERE climate_score    >= 3
  UNION
  SELECT id, 'kw' FROM companies
   WHERE matched_keywords IS NOT NULL
     AND (matched_keywords LIKE '%hardware%' OR matched_keywords LIKE '%robot%'
       OR matched_keywords LIKE '%climate%' OR matched_keywords LIKE '%energie%'
       OR matched_keywords LIKE '%solar%'   OR matched_keywords LIKE '%batterie%'
       OR matched_keywords LIKE '%battery%' OR matched_keywords LIKE '%wind%'
       OR matched_keywords LIKE '%co2%'     OR matched_keywords LIKE '%emiss%')
  UNION
  SELECT id, 'purpose' FROM companies
   WHERE purpose IS NOT NULL AND (
       lower(purpose) LIKE '%robotik%'  OR lower(purpose) LIKE '%robotic%'
    OR lower(purpose) LIKE '%hardware%' OR lower(purpose) LIKE '%climate%'
    OR lower(purpose) LIKE '%klima%'    OR lower(purpose) LIKE '%solar%'
    OR lower(purpose) LIKE '%batter%'   OR lower(purpose) LIKE '%wasserstoff%'
    OR lower(purpose) LIKE '%hydrogen%' OR lower(purpose) LIKE '%erneuerbar%'
    OR lower(purpose) LIKE '%renewable%' OR lower(purpose) LIKE '%co2%'
    OR lower(purpose) LIKE '%dekarbon%' OR lower(purpose) LIKE '%cleantech%'
    OR lower(purpose) LIKE '%emiss%'
   )
)
SELECT id, GROUP_CONCAT(DISTINCT reason) AS match_reasons
  FROM matches
 GROUP BY id
"""


def _flatten_json_list(raw):
    """JSON list → 'a; b; c' so a Sheets cell stays single-value."""
    if not raw:
        return ""
    try:
        items = json.loads(raw)
    except Exception:  # noqa: BLE001
        return raw
    return "; ".join(str(x) for x in items if x is not None)


def _rows_as_list_of_lists(cursor_iterable, columns: List[str], json_cols: Tuple[str, ...] = ()) -> List[list]:
    """Convert a sqlite cursor row stream → list-of-lists matching `columns`.

    JSON-array cells get flattened. Falsy/None values become ''. Everything
    else is stringified so gspread sends a consistent payload.
    """
    out: List[list] = [list(columns)]
    for row in cursor_iterable:
        d = dict(row)
        for jc in json_cols:
            if jc in d:
                d[jc] = _flatten_json_list(d[jc])
        out.append(["" if d.get(c) is None else str(d.get(c)) for c in columns])
    return out


# ---------------------------------------------------------------------------
# Per-tab builders
# ---------------------------------------------------------------------------

IDEAS_COLUMNS = [
    "id",
    "program",
    "company",
    "year_founded",
    "country",
    "one_liner",
    "long_description",
    "company_website",
    "source_url",
    "opportunity_score",
    "cluster_id",
    "cluster_label",
    "cluster_era",
    "problem_statement",
    "mechanism_tags",
    "sector_tags",
    "customer_verticals",
    "customer_size",
    "business_model",
    "solo_buildable",
    "solo_buildable_reasoning",
    "ai_first_advantage",
    "ai_first_reasoning",
    "moat_type",
    "niche_specificity",
    "match_reasons",
]

IDEAS_ROW_SQL = """
SELECT
  ci.id, ci.program, ci.company, ci.year_founded, ci.country,
  ci.one_liner, ci.long_description,
  ci.company_website, ci.source_url,
  ci.opportunity_score,
  ci.cluster_id,
  c.label  AS cluster_label,
  c.era_class AS cluster_era,
  ie.problem_statement,
  ie.mechanism_tags, ie.sector_tags, ie.customer_verticals,
  ie.customer_size, ie.business_model,
  ie.solo_buildable, ie.solo_buildable_reasoning,
  ie.ai_first_advantage, ie.ai_first_reasoning,
  ie.moat_type, ie.niche_specificity
FROM company_ideas ci
LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id AND ie.error IS NULL
LEFT JOIN idea_clusters   c  ON c.cluster_id       = ci.cluster_id
WHERE ci.id = ?
"""


def _build_ideas(conn: sqlite3.Connection) -> List[list]:
    matches = {r["id"]: r["match_reasons"] for r in conn.execute(IDEAS_MATCH_SQL)}
    rows: List[Dict] = []
    for idea_id, reasons in matches.items():
        r = conn.execute(IDEAS_ROW_SQL, (idea_id,)).fetchone()
        if not r:
            continue
        d = dict(r)
        d["match_reasons"] = reasons
        rows.append(d)

    def _gen():
        yield from rows

    return _rows_as_list_of_lists(
        _gen(),
        IDEAS_COLUMNS,
        json_cols=("mechanism_tags", "sector_tags", "customer_verticals"),
    )


COMPANIES_COLUMNS = [
    "id",
    "company_number",
    "name",
    "legal_form",
    "current_status",
    "registration_date",
    "first_registered_date",
    "first_seen_date",
    "city",
    "state",
    "postal_code",
    "street",
    "purpose",
    "website",
    "linkedin_url",
    "twitter_url",
    "github_url",
    "capital_amount",
    "capital_currency",
    "ai_robotics_score",
    "climate_score",
    "startup_score",
    "brand_name_score",
    "tech_categories",
    "matched_keywords",
    "tech_stack",
    "startup_classification",
    "relevance",
    "team_size",
    "job_count",
    "source",  # ← includes news/registration-scan provenance per earlier Q
    "contacted",
    "viewed",
    "match_reasons",
]

COMPANIES_ROW_SQL = """
SELECT
  c.id, c.company_number, c.name, c.legal_form, c.current_status,
  c.registration_date, c.first_registered_date, c.first_seen_date,
  c.city, c.state, c.postal_code, c.street,
  c.purpose,
  c.website, c.linkedin_url, c.twitter_url, c.github_url,
  c.capital_amount, c.capital_currency,
  c.ai_robotics_score, c.climate_score, c.startup_score, c.brand_name_score,
  c.tech_categories, c.matched_keywords, c.tech_stack,
  c.startup_classification, c.relevance,
  c.team_size, c.job_count,
  c.source,
  c.contacted, c.viewed
FROM companies c
WHERE c.id = ?
"""


def _build_companies(conn: sqlite3.Connection) -> List[list]:
    matches = {r["id"]: r["match_reasons"] for r in conn.execute(COMPANIES_MATCH_SQL)}
    rows: List[Dict] = []
    for cid, reasons in matches.items():
        r = conn.execute(COMPANIES_ROW_SQL, (cid,)).fetchone()
        if not r:
            continue
        d = dict(r)
        d["match_reasons"] = reasons
        rows.append(d)

    def _gen():
        yield from rows

    return _rows_as_list_of_lists(
        _gen(),
        COMPANIES_COLUMNS,
        json_cols=("tech_categories",),
    )


def _build_stealth(conn: sqlite3.Connection) -> List[list]:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(stealth_founders)")]
    rows = conn.execute("SELECT * FROM stealth_founders ORDER BY confidence_score DESC, last_checked_at DESC")
    return _rows_as_list_of_lists(rows, cols)


# ---------------------------------------------------------------------------
# Signals-pipeline tabs — the real surfacing views
# ---------------------------------------------------------------------------

SIGNALS_PEOPLE_COLUMNS = [
    "id",
    "founder_confidence",
    "composite_score",
    "primary_name",
    "person_role",
    "primary_linkedin",
    "primary_domain",
    "primary_github",
    "watchlist_tier",
    "watchlist_employer",
    "academic_founder",
    "academic_institution",
    "website_title",
    "website_description",
    "sector_tags",
    "geo_region",
    "signal_count",
    "signal_types",
    "first_signal_at",
    "last_signal_at",
    "last_enriched_at",
    "detail_url",
]

SIGNALS_PEOPLE_SQL = """
SELECT id, founder_confidence, composite_score,
       primary_name, person_role,
       primary_linkedin, primary_domain, primary_github,
       watchlist_tier, watchlist_employer,
       academic_founder, academic_institution,
       website_title, website_description,
       sector_tags, geo_region,
       signal_count, signal_types,
       first_signal_at, last_signal_at,
       last_enriched_at
  FROM stealth_entities
 WHERE is_person = 1
   AND founder_confidence >= 30
 ORDER BY founder_confidence DESC, composite_score DESC, signal_count DESC
"""


def _build_signals_people(conn: sqlite3.Connection) -> List[list]:
    """DACH-focused people view — same ranking as /signals/people in the UI.

    Includes a detail_url column so you can click straight to the full
    signal timeline per founder. Thresholds: founder_confidence >= 30
    (keeps the long tail for scouting), ordered strongest-first.
    """
    rows = conn.execute(SIGNALS_PEOPLE_SQL).fetchall()
    detail_base = "https://fabulous-fascination-production-4638.up.railway.app/signals/entity"
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d["detail_url"] = f"{detail_base}/{d['id']}"
        out.append(d)

    def _gen():
        yield from out

    return _rows_as_list_of_lists(
        _gen(),
        SIGNALS_PEOPLE_COLUMNS,
        json_cols=("sector_tags", "signal_types"),
    )


SIGNALS_ENTITIES_COLUMNS = [
    "id",
    "composite_score",
    "signal_count",
    "primary_name",
    "primary_domain",
    "primary_github",
    "is_person",
    "founder_confidence",
    "person_role",
    "watchlist_tier",
    "watchlist_employer",
    "academic_founder",
    "academic_institution",
    "website_title",
    "website_description",
    "sector_tags",
    "signal_types",
    "geo_region",
    "first_signal_at",
    "last_signal_at",
    "detail_url",
]

SIGNALS_ENTITIES_SQL = """
SELECT id, composite_score, signal_count,
       primary_name, primary_domain, primary_github,
       is_person, founder_confidence, person_role,
       watchlist_tier, watchlist_employer,
       academic_founder, academic_institution,
       website_title, website_description,
       sector_tags, signal_types, geo_region,
       first_signal_at, last_signal_at
  FROM stealth_entities
 WHERE signal_count >= 2
 ORDER BY composite_score DESC, signal_count DESC
"""


def _build_signals_entities(conn: sqlite3.Connection) -> List[list]:
    """Multi-signal entities (2+ independent sources) — the cross-source
    quality cut. Covers both companies and persons."""
    rows = conn.execute(SIGNALS_ENTITIES_SQL).fetchall()
    detail_base = "https://fabulous-fascination-production-4638.up.railway.app/signals/entity"
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d["detail_url"] = f"{detail_base}/{d['id']}"
        out.append(d)

    def _gen():
        yield from out

    return _rows_as_list_of_lists(
        _gen(),
        SIGNALS_ENTITIES_COLUMNS,
        json_cols=("sector_tags", "signal_types"),
    )


TAB_BUILDERS: Dict[str, Callable[[sqlite3.Connection], List[list]]] = {
    # Legacy CSV-style tabs (kept so you don't lose history)
    "ideas_climate_hardware_robotics": _build_ideas,
    "companies_climate_hardware_robotics": _build_companies,
    "stealth_founders": _build_stealth,
    # New signals-pipeline tabs (the actual deliverable)
    "signals_people": _build_signals_people,
    "signals_entities_multi": _build_signals_entities,
}


# ---------------------------------------------------------------------------
# Sheets client + push
# ---------------------------------------------------------------------------


def _open_spreadsheet():
    """Return the Sheets handle, or None when credentials aren't present.

    None (not sys.exit) is important: this script runs as a scheduled
    subprocess, and a missing-credentials environment (CI, Railway without
    the Sheets env vars set, a fresh dev checkout) should log + no-op,
    not fail the pipeline step.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    creds_b64 = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    if not creds_b64 or not sheet_id:
        logger.warning("GOOGLE_SHEETS_CREDENTIALS or GOOGLE_SHEETS_ID missing — skipping sync (not an error)")
        return None

    creds_data = json.loads(base64.b64decode(creds_b64))
    creds = Credentials.from_service_account_info(
        creds_data,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )
    return gspread.authorize(creds).open_by_key(sheet_id)


def _ensure_tab(sh, title: str, rows: int, cols: int):
    """Return a worksheet by title, creating or resizing as needed."""
    import gspread

    try:
        ws = sh.worksheet(title)
        # Resize so a smaller refresh doesn't leave stale rows at the bottom.
        ws.clear()
        if ws.row_count < rows or ws.col_count < cols:
            ws.resize(rows=max(rows, 2), cols=max(cols, 1))
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=max(rows, 2), cols=max(cols, 1))
    return ws


def _push(ws, values: List[list]):
    """Upload full tab contents in a single batch call."""
    if not values:
        return
    # gspread expects A1-range + 2D list. Column letter for last col:
    import string

    last_col_idx = len(values[0])
    letters = ""
    n = last_col_idx
    while n:
        n, rem = divmod(n - 1, 26)
        letters = string.ascii_uppercase[rem] + letters
    end_cell = f"{letters}{len(values)}"
    ws.update(values=values, range_name=f"A1:{end_cell}", value_input_option="RAW")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--only", choices=list(TAB_BUILDERS.keys()), help="Push just one tab")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    sh = _open_spreadsheet()
    if sh is None:
        # Credentials not configured — this is a clean no-op, not a failure.
        # The scheduler sees exit 0 and moves on; a human running the script
        # ad-hoc sees the warning in the log.
        return
    logger.info("spreadsheet: %s", sh.title)

    tabs = [args.only] if args.only else list(TAB_BUILDERS)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        for tab in tabs:
            logger.info("building %s …", tab)
            values = TAB_BUILDERS[tab](conn)
            n_rows, n_cols = len(values) - 1, (len(values[0]) if values else 0)
            logger.info("  %d rows × %d cols  — uploading", n_rows, n_cols)
            ws = _ensure_tab(sh, tab, rows=len(values), cols=n_cols)
            _push(ws, values)
            logger.info("  ✓ %s", tab)
    finally:
        conn.close()

    logger.info("done — sheet: https://docs.google.com/spreadsheets/d/%s", os.environ["GOOGLE_SHEETS_ID"])


if __name__ == "__main__":
    main()
