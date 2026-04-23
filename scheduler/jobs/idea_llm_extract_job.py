"""
idea_llm_extract_job — enrich each `company_ideas` row with structured
fields via Claude Haiku 4.5, optimised for microbusiness-idea discovery
and mechanism × sector gap-finding.

Two orthogonal tag axes per row:
  * mechanism_tags — WHAT the company does as a primitive (e.g.
    'llm-copilot', 'marketplace-with-escrow', 'vertical-saas-scheduling')
  * sector_tags    — WHO / WHERE applied (e.g. 'legal', 'healthcare',
    'construction')

Plus launch-assessment fields: problem_statement, customer_verticals,
customer_size, business_model, solo_buildable, ai_first_advantage,
moat_type, niche_specificity.

Design notes:
  - Haiku 4.5 via the Anthropic API. Tool-use with a strict JSON schema
    guarantees parseable output; we set `tool_choice` to force the tool.
  - Prompt caching on the large system prompt (schema + seed vocab +
    guidance). With a warm cache this is ~$0.001/row.
  - Concurrency via ThreadPoolExecutor (default 5) with 429/5xx retries.
  - Idempotent: skips rows that already have an extraction unless
    --refresh is passed.

Usage:
  python3 -m scheduler.jobs.idea_llm_extract_job --limit 10   # smoke
  python3 -m scheduler.jobs.idea_llm_extract_job              # full run
  python3 -m scheduler.jobs.idea_llm_extract_job --report-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Load .env before anything reads os.getenv. override=True because Claude
# Code exports ANTHROPIC_API_KEY='' which otherwise wins silently over the
# real key in .env (load_dotenv defaults to not overriding existing env).
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    # python-dotenv is an optional convenience — fall back to raw env vars.
    pass

from persistence.database import Database

logger = logging.getLogger(__name__)


MODEL = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 700


# --- seed vocabularies (kept here so prompt caching catches them) ---------

MECHANISM_VOCAB = [
    # Business-model primitives
    "subscription-saas", "usage-based-pricing", "per-seat-pricing",
    "marketplace-take-rate", "escrow-marketplace", "freemium", "bnpl",
    "transaction-fee", "licensing", "white-label", "data-broker", "ads",
    # AI primitives
    "llm-copilot", "rag-over-docs", "agent-automation", "voice-agent",
    "image-generation", "predictive-scoring", "anomaly-detection",
    "recommendation-engine", "ai-summarization", "ai-coding-assistant",
    # Distribution primitives
    "plg-bottom-up", "content-seo", "community-led-growth",
    "ambassador-referral", "embedded-integration", "api-distribution",
    "app-store",
    # UX / surface primitives
    "ai-chat-interface", "no-code-builder", "browser-extension",
    "mobile-first-app", "desktop-app", "slack-bot", "figma-plugin",
    "email-automation",
    # Data / moat primitives
    "proprietary-dataset", "network-effects", "reviews-and-ratings",
    "benchmarks", "anonymized-aggregation", "hardware-form-factor",
    # Workflow primitives
    "vertical-saas-crm", "document-extraction", "invoicing-ap-automation",
    "expense-reconciliation", "scheduling-automation", "lead-scraping",
    "contract-review", "compliance-monitoring", "data-pipeline",
    "observability-monitoring", "qa-testing-automation",
]

SECTOR_VOCAB = [
    # Verticals (who the customer operates in)
    "legal", "healthcare", "education", "construction", "real-estate",
    "finance", "insurance", "logistics", "manufacturing", "retail",
    "food-beverage", "agriculture", "automotive", "aerospace", "defense",
    "energy", "travel-hospitality", "fashion", "beauty", "fitness-wellness",
    "media", "entertainment", "gaming", "publishing", "government",
    "nonprofit", "biotech-pharma",
    # Horizontal buyers (functional role inside a company)
    "sales", "marketing", "hr-recruiting", "finance-ops", "it-security",
    "devops", "product-design", "data-engineering", "customer-support",
    "operations", "procurement",
    # Audience categories
    "developers", "creators", "sme-smb", "enterprise", "consumer",
    "prosumer-freelancers",
]


# --- DDL -------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS idea_extraction (
    company_idea_id     INTEGER PRIMARY KEY REFERENCES company_ideas(id),
    problem_statement   TEXT,
    customer_verticals  TEXT,      -- JSON array
    mechanism_tags      TEXT,      -- JSON array
    sector_tags         TEXT,      -- JSON array
    customer_size       TEXT,
    business_model      TEXT,
    solo_buildable      INTEGER,   -- 0/1
    solo_buildable_reasoning TEXT,
    ai_first_advantage  INTEGER,   -- 0/1
    ai_first_reasoning  TEXT,
    moat_type           TEXT,
    niche_specificity   TEXT,
    model               TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_read_tokens   INTEGER,
    error               TEXT,
    extracted_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_extract_model     ON idea_extraction(business_model);
CREATE INDEX IF NOT EXISTS idx_extract_size      ON idea_extraction(customer_size);
CREATE INDEX IF NOT EXISTS idx_extract_solo      ON idea_extraction(solo_buildable);
CREATE INDEX IF NOT EXISTS idx_extract_ai_first  ON idea_extraction(ai_first_advantage);
CREATE INDEX IF NOT EXISTS idx_extract_moat      ON idea_extraction(moat_type);
"""


# --- prompt ----------------------------------------------------------------

SYSTEM_PROMPT = f"""You extract structured metadata from startup descriptions
to support a microbusiness-idea discovery pipeline. The user is looking
for SaaS and service microbusiness opportunities they could launch solo
or with a tiny team. The data will be queried to find gaps in a
(mechanism × sector) matrix — so tagging consistency and two-axis
orthogonality are critical.

You will always respond by calling the `record_idea_extraction` tool
with all fields populated. Never return prose.

--- FIELD GUIDANCE ---

`problem_statement` — one sentence describing the CONCRETE PAIN being
  solved. Not marketing copy. Good: "B2B SaaS teams spend hours hunting
  for customer data across Stripe, Salesforce and HubSpot." Bad: "We're
  the operating system for customer data."

`customer_verticals` — 1-3 specific sub-segments the company SELLS TO.
  Be narrow. Good: ["solo landlords with 1-4 units", "property managers
  in the UK"]. Bad: ["real estate companies"].

`mechanism_tags` — 2-5 tags describing WHAT the company does as a
  primitive, independent of sector. STRONGLY PREFER these seed tags; only
  invent a new one if nothing in the vocab fits and label it with a
  hyphenated kebab-case slug (e.g. "wearable-continuous-monitoring"):
  {MECHANISM_VOCAB}

`sector_tags` — 1-3 tags for WHERE / TO WHOM this applies. Prefer these
  seed tags; invent in kebab-case only if necessary:
  {SECTOR_VOCAB}

`customer_size` — one of consumer|prosumer|smb|mid_market|enterprise|
  developer. Pick 'developer' only if the product's primary user is a
  developer (APIs, dev tools, infra).

`business_model` — one of saas|service|marketplace|api|hardware|agency|
  course|community|consumer_app|hybrid.

`solo_buildable` — true ONLY if a team of 1-3 people could reasonably
  ship and operate the product within 12 months. Set FALSE if any of:
    - hardware / physical devices required
    - regulatory approval needed (FDA, banking license, SOC2 is optional)
    - enterprise sales (6+ month sales cycle, RFPs)
    - requires proprietary dataset of meaningful scale
    - requires large model training from scratch
  Frontier model finetuning is NOT solo-buildable; using public LLM APIs IS.

`solo_buildable_reasoning` — one sentence, cites the specific blocker or
  the reason it IS solo-buildable.

`ai_first_advantage` — true if building this specifically with LLMs /
  agents / modern AI yields a meaningfully better product than the
  pre-2022 approach. False if the idea is orthogonal to AI (marketplaces,
  hardware, fintech infra, etc.) even if they use AI in features.

`ai_first_reasoning` — one sentence.

`moat_type` — one of network|data|regulatory|brand|capital|integration|
  domain_expertise|none. Pick the STRONGEST moat; use 'none' freely if no
  structural moat exists (most microbusinesses).

`niche_specificity` — broad (serves many industries), medium (one
  industry or role), narrow (specific sub-segment within an industry).

Be rigorous. An idea that sounds vaguely plausible but lacks an obvious
moat and is solo-buildable is exactly what the user is looking for — mark
it honestly."""


TOOL_SCHEMA = {
    "name": "record_idea_extraction",
    "description": "Record structured metadata for a scraped startup idea.",
    "input_schema": {
        "type": "object",
        "required": [
            "problem_statement", "customer_verticals", "mechanism_tags",
            "sector_tags", "customer_size", "business_model",
            "solo_buildable", "solo_buildable_reasoning",
            "ai_first_advantage", "ai_first_reasoning",
            "moat_type", "niche_specificity",
        ],
        "properties": {
            "problem_statement": {"type": "string"},
            "customer_verticals": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 4,
            },
            "mechanism_tags": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 5,
            },
            "sector_tags": {
                "type": "array", "items": {"type": "string"},
                "minItems": 1, "maxItems": 3,
            },
            "customer_size": {
                "type": "string",
                "enum": ["consumer", "prosumer", "smb", "mid_market",
                         "enterprise", "developer"],
            },
            "business_model": {
                "type": "string",
                "enum": ["saas", "service", "marketplace", "api", "hardware",
                         "agency", "course", "community", "consumer_app",
                         "hybrid"],
            },
            "solo_buildable":             {"type": "boolean"},
            "solo_buildable_reasoning":   {"type": "string"},
            "ai_first_advantage":         {"type": "boolean"},
            "ai_first_reasoning":         {"type": "string"},
            "moat_type": {
                "type": "string",
                "enum": ["network", "data", "regulatory", "brand", "capital",
                         "integration", "domain_expertise", "none"],
            },
            "niche_specificity": {
                "type": "string",
                "enum": ["broad", "medium", "narrow"],
            },
        },
    },
}


# --- input formatting ------------------------------------------------------

def _row_to_user_content(row: dict) -> str:
    parts = [f"Company: {row['company']}",
             f"Scouting source: {row['program']}"]
    if row.get("batch"):
        parts.append(f"Batch/cohort: {row['batch']}")
    if row.get("year_founded"):
        parts.append(f"Year founded: {row['year_founded']}")
    if row.get("country"):
        parts.append(f"Country: {row['country']}")
    if row.get("one_liner"):
        parts.append(f"\nOne-liner: {row['one_liner']}")
    if row.get("long_description"):
        parts.append(f"\nDescription: {row['long_description']}")
    if row.get("tags_json"):
        try:
            tag_list = json.loads(row["tags_json"])
            if tag_list:
                parts.append(f"\nSource tags: {', '.join(tag_list[:10])}")
        except Exception:  # noqa: BLE001
            pass
    if row.get("meta_description"):
        parts.append(f"\nWebsite meta: {row['meta_description']}")
    if row.get("hero_h1"):
        parts.append(f"\nWebsite H1: {row['hero_h1']}")
    if row.get("hero_text"):
        parts.append(f"\nWebsite body (truncated): {row['hero_text'][:1200]}")
    return "\n".join(parts)


# --- API call --------------------------------------------------------------

def _extract_one(client, row: dict, max_retries: int = 4) -> Dict[str, object]:
    import anthropic

    user_content = _row_to_user_content(row)
    attempts = 0
    while True:
        attempts += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}},
                ],
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "record_idea_extraction"},
                messages=[{"role": "user", "content": user_content}],
            )
            tool_calls = [b for b in resp.content if b.type == "tool_use"]
            if not tool_calls:
                return {"error": f"no_tool_use (stop_reason={resp.stop_reason})"}
            args = tool_calls[0].input
            args["_tokens"] = {
                "input":      getattr(resp.usage, "input_tokens", None),
                "output":     getattr(resp.usage, "output_tokens", None),
                "cache_read": getattr(resp.usage,
                                      "cache_read_input_tokens", None),
            }
            return args
        except anthropic.APIStatusError as e:
            # Auth / client errors won't be fixed by retrying — fail fast so a
            # bad API key doesn't burn the whole run on pointless retries.
            if e.status_code in (401, 403, 400):
                return {"error": f"api_status_{e.status_code}"}
            if attempts >= max_retries:
                return {"error": f"api_status_{e.status_code}"}
            time.sleep(min(2 ** attempts + random.random(), 30))
        except Exception as e:  # noqa: BLE001
            if attempts >= max_retries:
                return {"error": f"exception:{type(e).__name__}"}
            time.sleep(min(2 ** attempts + random.random(), 30))


# --- DB helpers ------------------------------------------------------------

_LOCK = threading.Lock()


def _ensure_schema(db: Database) -> None:
    cur = db.conn.cursor()
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    db.conn.commit()


def _fetch_targets(db: Database, limit: Optional[int], refresh: bool,
                   sample_diverse: bool,
                   program_like: Optional[str] = None) -> List[dict]:
    cur = db.conn.cursor()
    clauses: List[str] = []
    params: List = []
    if not refresh:
        # Skip rows that already have a SUCCESSFUL extraction. Error rows
        # (e.g. from past credit-exhaustion or transient 5xx) will be
        # retried automatically on re-run — that's almost always what you
        # want, since the causes are external and transient.
        clauses.append(
            "ci.id NOT IN (SELECT company_idea_id FROM idea_extraction "
            "WHERE error IS NULL)"
        )
    if program_like:
        # SQL LIKE pattern — e.g. 'Reddit r/%' or 'Show HN'.
        clauses.append("ci.program LIKE ?")
        params.append(program_like)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = ""
    if sample_diverse:
        # one row per program, spread across clusters, with both recent and
        # old entries. Simple stratified-ish sample: random per program then
        # take up to `limit//n_programs` from each.
        order = "ORDER BY ci.program, RANDOM()"
    else:
        order = "ORDER BY ci.id"
    sql = f"""
        SELECT ci.id, ci.program, ci.company, ci.batch, ci.year_founded,
               ci.country, ci.one_liner, ci.long_description,
               ci.tags_json, we.meta_description, we.hero_h1, we.hero_text
          FROM company_ideas ci
     LEFT JOIN website_enrichment we
            ON we.normalized_website = ci.normalized_website
          {where}
        {order}
    """
    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    if sample_diverse and limit:
        by_prog: Dict[str, List[dict]] = {}
        for r in rows:
            by_prog.setdefault(r["program"], []).append(r)
        per = max(1, limit // max(1, len(by_prog)))
        picked: List[dict] = []
        for _, rs in by_prog.items():
            picked.extend(rs[:per])
        random.shuffle(picked)
        rows = picked[:limit]
    elif limit:
        rows = rows[:limit]
    return rows


def _save(db: Database, row_id: int, data: Dict[str, object]) -> None:
    tok = data.get("_tokens", {}) or {}
    err = data.get("error")
    with _LOCK:
        cur = db.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO idea_extraction
            (company_idea_id, problem_statement, customer_verticals,
             mechanism_tags, sector_tags, customer_size, business_model,
             solo_buildable, solo_buildable_reasoning,
             ai_first_advantage, ai_first_reasoning, moat_type,
             niche_specificity, model, input_tokens, output_tokens,
             cache_read_tokens, error, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_id,
             data.get("problem_statement"),
             json.dumps(data.get("customer_verticals") or []),
             json.dumps(data.get("mechanism_tags") or []),
             json.dumps(data.get("sector_tags") or []),
             data.get("customer_size"),
             data.get("business_model"),
             1 if data.get("solo_buildable") else (
                 0 if data.get("solo_buildable") is False else None),
             data.get("solo_buildable_reasoning"),
             1 if data.get("ai_first_advantage") else (
                 0 if data.get("ai_first_advantage") is False else None),
             data.get("ai_first_reasoning"),
             data.get("moat_type"),
             data.get("niche_specificity"),
             MODEL,
             tok.get("input"), tok.get("output"), tok.get("cache_read"),
             err,
             datetime.now().isoformat()),
        )
        db.conn.commit()


# --- main ------------------------------------------------------------------

def _make_client():
    """Anthropic client that accepts either ANTHROPIC_API_KEY or the
    Claude Code OAuth token (CLAUDE_CODE_OAUTH_TOKEN)."""
    import anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY") or None
    oauth = os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or None
    if api_key:
        return anthropic.Anthropic(api_key=api_key)
    if oauth:
        return anthropic.Anthropic(auth_token=oauth)
    raise RuntimeError(
        "Neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set"
    )


def run(db_path: str, limit: Optional[int], workers: int, refresh: bool,
        sample_diverse: bool, verbose_sample: bool,
        program_like: Optional[str] = None) -> None:
    client = _make_client()

    db = Database(db_path)
    _ensure_schema(db)
    rows = _fetch_targets(db, limit, refresh, sample_diverse, program_like)
    logger.info("extraction: %d rows to process (workers=%d)", len(rows), workers)
    if not rows:
        return

    t0 = time.monotonic()
    done = 0
    ok = 0
    results_for_print: List[Tuple[dict, dict]] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_extract_one, client, r): r for r in rows}
        for fut in as_completed(futs):
            row = futs[fut]
            try:
                data = fut.result()
            except Exception as e:  # noqa: BLE001
                data = {"error": f"crash:{type(e).__name__}"}
            _save(db, row["id"], data)
            done += 1
            if not data.get("error"):
                ok += 1
                if verbose_sample:
                    results_for_print.append((row, data))
            if done % 25 == 0 or done == len(rows):
                dt = time.monotonic() - t0
                logger.info("extraction: %d/%d done (%d ok, %d err) in %.1fs",
                            done, len(rows), ok, done - ok, dt)

    if verbose_sample:
        _print_sample(results_for_print)
    print_report(db)
    db.conn.close()


def _print_sample(results: List[Tuple[dict, dict]]) -> None:
    print("\n" + "=" * 78)
    print(" SAMPLE OUTPUT")
    print("=" * 78)
    for row, data in results:
        print(f"\n── {row['program']}  ·  {row['company']}  "
              f"({row.get('batch') or row.get('year_founded') or '-'})")
        if row.get("one_liner"):
            print(f"   input one-liner: {row['one_liner'][:140]}")
        print(f"   problem:   {data.get('problem_statement')}")
        print(f"   customer:  {', '.join(data.get('customer_verticals') or [])}  "
              f"[{data.get('customer_size')}]")
        print(f"   MECH:      {', '.join(data.get('mechanism_tags') or [])}")
        print(f"   SECTOR:    {', '.join(data.get('sector_tags') or [])}")
        print(f"   model:     {data.get('business_model')}  ·  "
              f"moat: {data.get('moat_type')}  ·  niche: "
              f"{data.get('niche_specificity')}")
        solo = "YES" if data.get("solo_buildable") else "NO"
        ai_first = "YES" if data.get("ai_first_advantage") else "NO"
        print(f"   solo_buildable={solo} — {data.get('solo_buildable_reasoning')}")
        print(f"   ai_first={ai_first} — {data.get('ai_first_reasoning')}")


def print_report(db: Database) -> None:
    cur = db.conn.cursor()
    tot = cur.execute("SELECT COUNT(*) AS n FROM idea_extraction").fetchone()["n"]
    errs = cur.execute(
        "SELECT COUNT(*) AS n FROM idea_extraction WHERE error IS NOT NULL"
    ).fetchone()["n"]
    print(f"\n=== idea_extraction — {tot} rows ({errs} errors) ===")
    if tot == 0:
        return
    print("  business_model distribution:")
    for r in cur.execute(
        "SELECT business_model, COUNT(*) AS n FROM idea_extraction "
        "WHERE error IS NULL GROUP BY business_model ORDER BY n DESC"
    ):
        print(f"    {r['business_model']:<15} {r['n']:>5}")
    print("  solo_buildable × ai_first matrix:")
    for r in cur.execute(
        """
        SELECT solo_buildable, ai_first_advantage, COUNT(*) AS n
          FROM idea_extraction WHERE error IS NULL
         GROUP BY solo_buildable, ai_first_advantage
         ORDER BY solo_buildable DESC, ai_first_advantage DESC
        """
    ):
        print(f"    solo={r['solo_buildable']}  ai_first={r['ai_first_advantage']}  n={r['n']}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--refresh", action="store_true",
                   help="Re-extract rows even if already present")
    p.add_argument("--sample-diverse", action="store_true",
                   help="Stratified sample across programs (for --limit runs)")
    p.add_argument("--verbose-sample", action="store_true",
                   help="Print each extraction result to stdout")
    p.add_argument("--program-like", default=None,
                   help="Only process programs matching this SQL LIKE "
                        "pattern (e.g. 'Reddit r/%%' or 'Show HN')")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.report_only:
        db = Database(args.db)
        _ensure_schema(db)
        print_report(db)
        db.conn.close()
        return

    if not (os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
        sys.exit("Neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set")

    run(args.db, args.limit, args.workers, args.refresh,
        args.sample_diverse, args.verbose_sample,
        program_like=args.program_like)


if __name__ == "__main__":
    main()
