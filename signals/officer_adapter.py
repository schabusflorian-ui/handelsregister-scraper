"""
Officer cross-reference adapter.

Emits a `founder_match` signal when a stealth-founder's LinkedIn name
exactly matches a Handelsregister officer. This is the HIGH-confidence
LinkedIn ↔ company bridge the fuzzy name joiner can't reliably produce
on its own.

False-match guards:
  - Full name must have ≥ 2 tokens, each ≥ 3 chars (block "A B")
  - Name must be reasonably rare — if it matches ≥ `MAX_OFFICER_HITS`
    distinct officer rows, we skip (it's a Thomas-Schmidt-grade common
    name and the match carries no signal)
  - The matched company must look like a plausible startup — young
    (first_seen in last 24 months) OR flagged with any sector score ≥ 1

Writes one signal per confirmed (founder, company) edge. The joiner then
collapses founder's LinkedIn signals and the company's handelsregister
signal into one stealth_entity with 3+ sources.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from .common import ensure_json, iso_now, normalize_name

logger = logging.getLogger(__name__)

MAX_OFFICER_HITS = 2        # names shared by > this many officers = ambiguous → skip
MIN_TOKENS       = 2
MIN_TOKEN_LEN    = 3


_DIRTY_NAME_RX = re.compile(r"[–—|:/\\(){}\[\]]|  +|\.\.\.|\d{3,}")
_LEGAL_ENTITY_RX = re.compile(
    r"\b(gmbh|ug|ag|kg|mbh|se|ohg|gbr|limited|ltd|inc|llc|co\.|corp|"
    r"haftungsbeschränkt|haftungsbeschrankt|holding|holdings|group|labs)\b",
    re.I,
)


def _clean_person_name(name: Optional[str]) -> Optional[tuple]:
    """Return (first, last) tokens if the input looks like a plausible
    person name — ≤ 5 tokens, no obvious snippet-pollution markers,
    not a legal entity. Returns None otherwise.

    Scraped LinkedIn snippets often contain multiple people concatenated
    with ' – ' separators; filter those out here rather than at match time.
    """
    if not name:
        return None
    s = name.strip()
    if not s:
        return None
    if _DIRTY_NAME_RX.search(s):
        return None
    if _LEGAL_ENTITY_RX.search(s):
        return None
    toks = [t for t in re.split(r"\s+", s) if len(t) >= 2]
    # Drop single-letter initials
    toks = [t for t in toks if not (len(t) == 2 and t.endswith("."))]
    if len(toks) < MIN_TOKENS or len(toks) > 5:
        return None
    # Each token should have at least one letter
    if not all(any(c.isalpha() for c in t) for t in toks):
        return None
    first, last = toks[0], toks[-1]
    if len(first) < MIN_TOKEN_LEN or len(last) < MIN_TOKEN_LEN:
        return None
    return (first.lower(), last.lower())


def _is_distinctive_name(name: Optional[str]) -> bool:
    return _clean_person_name(name) is not None


def _common_name_keys(cur) -> set:
    """Pre-compute (first,last) keys that appear across many officer rows —
    common-name blacklist. Keys are lowercase (first_token, last_token)
    tuples using the SAME normalization as _clean_person_name."""
    counts: dict = {}
    for r in cur.execute(
        "SELECT name FROM officers WHERE name IS NOT NULL"
    ):
        k = _clean_person_name(r["name"])
        if k is None:
            continue
        counts[k] = counts.get(k, 0) + 1
    return {k for k, n in counts.items() if n > MAX_OFFICER_HITS}


def _common_officer_names(cur) -> set:
    """Kept as a thin shim for callers that want the raw full-string set."""
    rows = cur.execute(
        """SELECT lower(trim(name)) AS n, COUNT(DISTINCT company_id) AS c
             FROM officers
            WHERE name IS NOT NULL AND trim(name) != ''
            GROUP BY lower(trim(name))
           HAVING c > ?""",
        (MAX_OFFICER_HITS,),
    ).fetchall()
    return {r["n"] for r in rows}


def _is_startup_shaped(row) -> bool:
    """Keep only officers at young/sector-flagged/startup-classified companies.
    Filters out the bulk of regular Mittelstand companies where an exact
    name match carries no signal."""
    try:
        seen = str(row["first_seen_date"] or "")[:10]
        young = seen >= (datetime.now(timezone.utc).date().replace(
            year=datetime.now(timezone.utc).year - 2).isoformat())
    except (ValueError, TypeError):
        young = False
    return bool(
        young
        or (row["ai_robotics_score"] or 0) >= 1
        or (row["climate_score"] or 0) >= 1
        or row["startup_classification"]
    )


def adapt_officers(conn, *, min_founder_confidence: float = 0.3) -> dict:
    cur = conn.cursor()
    common_keys = _common_name_keys(cur)
    logger.info("officer adapter: %d common (first,last) keys blacklisted",
                len(common_keys))

    # Build a (first, last) → [officer rows] index once. Cheaper than N
    # fuzzy SQL lookups when we have thousands of founders to probe.
    officer_index: dict = {}
    for r in cur.execute(
        """SELECT o.id, o.company_id, o.name AS officer_name, o.role, o.start_date,
                  c.name AS company_name, c.website, c.first_seen_date,
                  c.ai_robotics_score, c.climate_score,
                  c.startup_classification, c.tech_categories
             FROM officers o
             JOIN companies c ON c.id = o.company_id"""
    ):
        k = _clean_person_name(r["officer_name"])
        if k is None or k in common_keys:
            continue
        officer_index.setdefault(k, []).append(r)
    logger.info("officer adapter: index built for %d distinct names",
                len(officer_index))

    founders = cur.execute(
        """SELECT id, name, linkedin_url, headline, confidence_score,
                  geo_region, sector_tags
             FROM stealth_founders
            WHERE confidence_score >= ? AND name IS NOT NULL""",
        (min_founder_confidence,),
    ).fetchall()

    inserted = skipped_ambiguous = skipped_undistinctive = skipped_no_match = 0
    for sf in founders:
        key = _clean_person_name(sf["name"])
        if key is None:
            skipped_undistinctive += 1
            continue
        if key in common_keys:
            skipped_ambiguous += 1
            continue
        officer_rows = officer_index.get(key, [])
        # Second-pass startup filter here (we didn't want to filter at
        # index-build time — a non-startup officer still goes in the index
        # so we can see it's not startup-shaped before emitting).
        matches = [m for m in officer_rows if _is_startup_shaped(m)]
        if not matches:
            skipped_no_match += 1
            continue
        # Final ambiguity guard: a clean name matching >3 startup-shaped
        # companies is more likely a common name slipping through the
        # blacklist than one serial founder — skip to stay conservative.
        if len(matches) > 3:
            skipped_ambiguous += 1
            continue
        # Normalize the person_name to the reconstructed (first, last)
        # form — avoids writing the polluted snippet into the signal.
        clean_person = f"{key[0].title()} {key[1].title()}"

        for m in matches:
            tags: List[str] = []
            try:
                for t in json.loads(m["tech_categories"] or "[]"):
                    if isinstance(t, str):
                        tags.append(t)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            try:
                for t in json.loads(sf["sector_tags"] or "[]"):
                    if isinstance(t, str) and t not in tags:
                        tags.append(t)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

            # Confidence: role-boosted. A "Geschäftsführer" / "CEO" / founder
            # title in the officer row is nearly definitive; a generic
            # "Prokurist" is weaker.
            role = (m["role"] or "").lower()
            if any(kw in role for kw in ("geschäftsführ", "ceo", "gründer", "founder")):
                conf = 90.0
            elif "vorstand" in role or "director" in role or "managing" in role:
                conf = 80.0
            else:
                conf = 70.0

            res = cur.execute(
                """INSERT OR IGNORE INTO signals (
                     signal_type, observed_at, source_url,
                     entity_name, entity_domain,
                     person_name, person_linkedin,
                     sector_tags, geo_region, raw_payload, confidence
                   ) VALUES ('founder_match', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["first_seen_date"] or iso_now(),
                    f"officer_match://founder_{sf['id']}/company_{m['company_id']}",
                    m["company_name"],
                    None,  # domain filled in by companies adapter already
                    clean_person,
                    sf["linkedin_url"],
                    ensure_json(tags),
                    sf["geo_region"] or "DE",
                    ensure_json({
                        "founder_id": sf["id"],
                        "company_id": m["company_id"],
                        "role": m["role"],
                        "start_date": m["start_date"],
                        "headline": sf["headline"],
                    }),
                    conf,
                ),
            )
            if res.rowcount:
                inserted += 1
    conn.commit()
    stats = {
        "inserted": inserted,
        "skipped_undistinctive": skipped_undistinctive,
        "skipped_ambiguous": skipped_ambiguous,
        "skipped_no_match": skipped_no_match,
    }
    logger.info("officer adapter: %s", stats)
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
        print(json.dumps(adapt_officers(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
