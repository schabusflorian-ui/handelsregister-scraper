"""
Signal joiner — resolves raw `signals` rows into `stealth_entities`.

Two-pass design:

1. **Resolve** each unresolved signal to a stealth_entity. Match order:
       primary_domain == entity_domain      (strongest — unique fact)
       primary_github == entity_github
       primary_linkedin == person_linkedin
       name_similarity(primary_name, entity_name) >= NAME_SIM_THRESHOLD
   If no match, create a new stealth_entity seeded from the signal.

2. **Recompute** every affected stealth_entity's aggregate columns
   (signal_count, signal_types, first/last_signal_at, sector_tags union,
   geo_region, composite_score, primary_* promoted from best signal).

Runs idempotently — safe to call after every adapter.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Set

from .common import (
    composite_score,
    ensure_json,
    name_similarity,
    normalize_domain,
    normalize_github,
    normalize_name,
)

logger = logging.getLogger(__name__)

NAME_SIM_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def _find_entity_id(cur, signal: dict) -> Optional[int]:
    """Return existing stealth_entity id matching this signal, or None.

    Resolution order (first positive wins):
      1. exact primary_domain match
      2. exact primary_github match
      3. exact primary_linkedin match
      4. fuzzy name — strict when bridging ACROSS signal types,
         loose when the entity already has this type (dedup within source)
    """
    dom = normalize_domain(signal.get("entity_domain"))
    gh = normalize_github(signal.get("entity_github"))
    li = (signal.get("person_linkedin") or "").strip().lower() or None
    incoming_type = signal.get("signal_type")

    if dom:
        row = cur.execute(
            "SELECT id FROM stealth_entities WHERE primary_domain = ? LIMIT 1",
            (dom,),
        ).fetchone()
        if row:
            return row["id"]

    if gh:
        row = cur.execute(
            "SELECT id FROM stealth_entities WHERE primary_github = ? LIMIT 1",
            (gh,),
        ).fetchone()
        if row:
            return row["id"]

    if li:
        row = cur.execute(
            "SELECT id FROM stealth_entities WHERE primary_linkedin = ? LIMIT 1",
            (li,),
        ).fetchone()
        if row:
            return row["id"]

    name_norm = normalize_name(signal.get("entity_name"))
    if name_norm and len(name_norm) >= 3:
        # Candidate set: entities whose primary_name's first token overlaps.
        # Keeps the fuzzy pass bounded.
        first_token = name_norm.split()[0]
        candidates = cur.execute(
            "SELECT id, primary_name, signal_types FROM stealth_entities WHERE lower(primary_name) LIKE ? LIMIT 500",
            (f"%{first_token}%",),
        ).fetchall()
        best_id, best_sim, best_strict = None, 0.0, False
        for c in candidates:
            try:
                existing_types = json.loads(c["signal_types"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                existing_types = []
            # Cross-type merges use strict (require 2+ shared tokens).
            # Same-type merges stay loose (within-source dedup).
            is_cross_type = (
                incoming_type is not None and incoming_type not in existing_types and len(existing_types) > 0
            )
            sim = name_similarity(signal.get("entity_name"), c["primary_name"], strict=is_cross_type)
            if sim > best_sim:
                best_sim, best_id, best_strict = sim, c["id"], is_cross_type
        # Threshold is the same (0.75) but strict rules kick in during
        # similarity calc itself, so a cross-type false-match on a single
        # token already scores 0.0.
        if best_sim >= NAME_SIM_THRESHOLD:
            return best_id

    return None


def _create_entity(cur, signal: dict) -> int:
    """Insert a new stealth_entity seeded from this signal. Returns id.

    `signal_types` is seeded with THIS signal's type immediately rather
    than left as `[]`. Reason: the joiner's cross-type guard uses the
    existing entity's `signal_types` to decide whether to run strict
    matching. If we leave it empty, the next signal in the same joiner
    pass can't tell this is a cross-type situation and falls through to
    loose matching, which caused the "M.C Ecosystem GmbH" ↔ "Bozorg
    Kalantar Open Ecosystem" merge bug.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    initial_types = ensure_json([signal.get("signal_type")]) if signal.get("signal_type") else "[]"
    cur.execute(
        """INSERT INTO stealth_entities (
             primary_name, primary_domain, primary_github, primary_linkedin,
             signal_count, signal_types,
             first_signal_at, last_signal_at,
             sector_tags, geo_region, composite_score,
             created_at, updated_at
           ) VALUES (?, ?, ?, ?, 0, ?, NULL, NULL, NULL, ?, 0.0, ?, ?)
        """,
        (
            signal.get("entity_name") or signal.get("person_name") or "(unknown)",
            normalize_domain(signal.get("entity_domain")),
            normalize_github(signal.get("entity_github")),
            (signal.get("person_linkedin") or "").strip().lower() or None,
            initial_types,
            signal.get("geo_region"),
            now,
            now,
        ),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Aggregate recompute
# ---------------------------------------------------------------------------


def _merge_sector_tags(cur, entity_id: int) -> Optional[str]:
    """Union sector_tags across all signals for this entity."""
    rows = cur.execute(
        "SELECT sector_tags FROM signals WHERE stealth_entity_id = ? AND sector_tags IS NOT NULL",
        (entity_id,),
    ).fetchall()
    tags: Set[str] = set()
    for r in rows:
        try:
            parsed = json.loads(r["sector_tags"])
            if isinstance(parsed, list):
                tags.update(t for t in parsed if isinstance(t, str) and t)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return ensure_json(sorted(tags)) if tags else None


def _pick_primary(cur, entity_id: int, field: str, signal_col: str) -> Optional[str]:
    """Pick the first non-null value for a primary_* field, preferring the
    most recent signal that has one set."""
    row = cur.execute(
        f"SELECT {signal_col} FROM signals "
        f"WHERE stealth_entity_id = ? AND {signal_col} IS NOT NULL "
        f"ORDER BY observed_at DESC LIMIT 1",
        (entity_id,),
    ).fetchone()
    return row[signal_col] if row else None


# Signal types that pin a specific human
_PERSON_SIGNAL_TYPES = frozenset({"linkedin_stealth", "linkedin_transition", "founder_match"})

# Role keywords that qualify a person as a founder (vs. plain employee).
#
# Catalog based on Harmonic's list ("senior individual contributors,
# engineers, heads of product, repeat founders or operators") plus explicit
# DACH titles. Includes the loose "chief of staff / principal / head of"
# labels that fire at 1-5 person startups where the title inflation IS the
# signal (chief anything at a 3-person shop = founder-adjacent).
#
# False-positive risk is low because this regex only runs against profiles
# already flagged as person-type signals; the composite_score and
# founder_confidence formula apply additional cross-source corroboration.
_FOUNDER_ROLE_RX = re.compile(
    r"\b("
    # Core founder titles
    r"co[-\s]?founder|founder|founding (member|engineer|team|partner)|"
    r"solo[-\s]?founder|solopreneur|indie (hacker|maker|founder)|"
    r"serial (entrepreneur|founder)|repeat (founder|entrepreneur)|"
    # Executive C-suite (at young/small company = founder-adjacent).
    # "chief [of ]staff" / "chief product officer" / "chief ai officer" all count.
    r"ceo|cto|cfo|coo|cmo|cro|cpo|cxo|chief (of )?[a-z]{2,}|"
    # German founder titles
    r"gründer|gruender|mitgründer|mitgruender|"
    r"geschäftsführ|geschäftsleitung|geschaeftsfuehr|"
    # Ownership / sole-prop language
    r"owner|co[-\s]?owner|inhaber|inhaberin|eigentümer|"
    # Studio / venture-builder adjacent
    r"startup studio|venture builder|building (my own|a startup)|"
    r"bootstrap(ping|per)|entrepreneur\b|"
    # Early-stage title inflation (strong at 1-5 person companies).
    # "a stealth" / "an early-stage" / "the next" all count — these are
    # all "ambiguous Series-A-or-earlier" markers.
    r"head of (product|engineering|growth|design|ai)\s+(at|@)\s+"
    r"(a\s+)?(stealth|an?\s+early|an?\s+new|a\s+pre|the\s+next)|"
    r"principal (engineer|scientist|researcher) (at|@)\s+"
    r"(a\s+)?(stealth|an?\s+early|an?\s+new)"
    r")",
    re.I,
)


def _founder_role_from_signals(cur, entity_id: int) -> Optional[str]:
    """Scan signal payloads for a founder-ish role string. Returns the
    first match verbatim (so UI can display 'Co-Founder & CEO' etc.)."""
    rows = cur.execute(
        "SELECT raw_payload FROM signals WHERE stealth_entity_id = ? AND raw_payload IS NOT NULL",
        (entity_id,),
    ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["raw_payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for key in ("role", "headline", "field", "classification"):
            v = payload.get(key)
            if isinstance(v, str) and _FOUNDER_ROLE_RX.search(v):
                # Return a trimmed version (first 120 chars) for display
                return v.strip()[:120]
    return None


# DACH academic institutions whose alumni/researchers are high-signal
# deeptech founders. Matched substring-wise against bio text — "PhD ETH"
# or "Fraunhofer researcher" or "ex-MPI" all count. Kept small + curated;
# add more as we see them in real data.
_ACADEMIC_INSTITUTIONS = {
    # Germany
    "tum",
    "technische universität münchen",
    "tu münchen",
    "tu munich",
    "unternehmertum",  # TUM's venture entity — counts as adjacency
    "rwth",
    "rwth aachen",
    "kit",
    "karlsruhe institute of technology",
    "lmu",
    "ludwig maximilian",
    "humboldt university",
    "humboldt-universität",
    "tu berlin",
    "tu dresden",
    "tu darmstadt",
    "fraunhofer",
    "fraunhofer institute",
    "fraunhofer-institut",
    "max planck",
    "max-planck",
    "mpi ",
    "charité",
    "charite",
    "hpi",
    "hasso plattner",
    # Switzerland
    "eth zürich",
    "eth zurich",
    "eth",
    "epfl",
    "école polytechnique fédérale",
    "university of zurich",
    "uni zürich",
    "idiap",
    # Austria
    "ist austria",
    "institute of science and technology austria",
    "tu wien",
    "tu vienna",
    "university of vienna",
    "universität wien",
    # UK/EU research of DACH relevance
    "cern",
}

_ACADEMIC_RX = re.compile(
    r"(?:^|[^a-z])(?:" + "|".join(re.escape(x) for x in _ACADEMIC_INSTITUTIONS) + r")(?=[^a-z]|$)",
    re.I,
)


def _academic_lookup(cur, entity_id: int) -> tuple:
    """Scan signal payloads for a DACH academic affiliation.
    Returns (academic_flag, matched_institution)."""
    rows = cur.execute(
        "SELECT raw_payload FROM signals WHERE stealth_entity_id = ? AND raw_payload IS NOT NULL",
        (entity_id,),
    ).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["raw_payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for key in ("headline", "role", "summary", "description", "new", "old"):
            v = payload.get(key)
            if not isinstance(v, str) or not v:
                continue
            m = _ACADEMIC_RX.search(v)
            if m:
                return 1, m.group(0).strip()[:60]
    return 0, None


def _website_lookup(cur, domain: Optional[str]) -> tuple:
    """Look up (title, meta_description) from website_enrichment by domain.
    Returns (None, None) when no match. Zero-cost reuse of the 2,718
    already-scraped homepages.

    website_enrichment is created by website_enrichment_job (not by
    persistence.database), so a brand-new DB (tests, fresh install) may
    not have the table yet. Degrades silently rather than failing the
    joiner — the enrichment is best-effort.
    """
    if not domain:
        return None, None
    try:
        row = cur.execute(
            "SELECT title, meta_description FROM website_enrichment WHERE normalized_website = ? AND error IS NULL LIMIT 1",
            (domain,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — includes OperationalError 'no such table'
        return None, None
    if not row:
        return None, None
    return row["title"], row["meta_description"]


def _watchlist_lookup(cur, entity_id: int) -> tuple:
    """Scan the entity's signal payloads for an ex-employer match against
    the DACH watchlist. Returns (tier, employer_name) or (None, None).

    Looks at:
      - linkedin_transition payloads' `old` field (previous employer)
      - linkedin_stealth payloads' `headline` field (bios often mention
        "ex-Google" / "previously at Stripe" / "former OpenAI research")
    """
    from .watchlist import watchlist_match

    rows = cur.execute(
        "SELECT signal_type, raw_payload FROM signals WHERE stealth_entity_id = ? AND raw_payload IS NOT NULL",
        (entity_id,),
    ).fetchall()
    best_tier: Optional[int] = None
    best_employer: Optional[str] = None
    for r in rows:
        try:
            payload = json.loads(r["raw_payload"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        # Candidate text fields depending on signal type
        candidates = []
        if r["signal_type"] == "linkedin_transition":
            candidates.append(payload.get("old"))
        candidates.extend(payload.get(k) for k in ("headline", "role", "previous_companies", "ex_companies"))
        for text in candidates:
            if not isinstance(text, str) or not text:
                continue
            # watchlist_match returns the CANONICAL catalog entry name
            # ("Stripe"), not whatever surrounding noise the bio contains
            # ("ex-Stripe Jay Gokhale"). Keeps the UI badge readable.
            t, canonical = watchlist_match(text)
            if t is not None and (best_tier is None or t < best_tier):
                best_tier, best_employer = t, canonical
    return best_tier, best_employer


def _compute_founder_confidence(
    types_set: set,
    role: Optional[str],
    cur,
    entity_id: int,
    watchlist_tier_val: Optional[int] = None,
    is_academic: bool = False,
) -> float:
    """0-100 founder confidence.

    Blends seven axes:
      - LinkedIn claim present                   (+30)
      - LinkedIn transition (RECENTLY stealth)   (+15)
      - Officer-match on Handelsregister         (+40)  ← strongest person link
      - Founder/CEO/CTO role keywords in bio     (+15)
      - Product evidence (crt or github)         (+10)
      - Young company registered                 (+10)
      - Ex-employer watchlist tier               (+6 / +12 / +20)
      - DACH academic affiliation                (+10) — TUM/ETH/Fraunhofer/MPI alumni
                                                        skew heavily toward deeptech founders
    """
    fc = 0.0
    if "linkedin_stealth" in types_set:
        fc += 30
    if "linkedin_transition" in types_set:
        fc += 15
    if "founder_match" in types_set:
        fc += 40
    if role:
        fc += 15
    if "crt_domain_new" in types_set or "github_org_new" in types_set:
        fc += 10
    # Watchlist boost — Harmonic's "priority organizations" pattern
    if watchlist_tier_val is not None:
        from .watchlist import tier_boost

        fc += tier_boost(watchlist_tier_val)
    if is_academic:
        fc += 10

    # Company young / startup-shaped?
    row = cur.execute(
        """SELECT MAX(json_extract(raw_payload, '$.company_id')) AS cid
             FROM signals
            WHERE stealth_entity_id = ?
              AND signal_type IN ('handelsregister_new','founder_match')
              AND raw_payload IS NOT NULL""",
        (entity_id,),
    ).fetchone()
    cid = row["cid"] if row else None
    if cid is not None:
        c = cur.execute(
            "SELECT first_seen_date, ai_robotics_score, climate_score, "
            "       startup_classification FROM companies WHERE id = ?",
            (cid,),
        ).fetchone()
        if c:
            try:
                seen = str(c["first_seen_date"] or "")[:10]
                if seen >= (
                    datetime.now(timezone.utc)
                    .date()
                    .replace(
                        year=datetime.now(timezone.utc).year - 2
                        if datetime.now(timezone.utc).month > 1
                        else datetime.now(timezone.utc).year - 2
                    )
                    .isoformat()
                ):
                    fc += 10
            except (ValueError, TypeError):
                pass
    return min(100.0, round(fc, 1))


def _recompute_entity(cur, entity_id: int) -> None:
    agg = cur.execute(
        """SELECT COUNT(*)                           AS cnt,
                  MIN(observed_at)                   AS first_at,
                  MAX(observed_at)                   AS last_at,
                  GROUP_CONCAT(DISTINCT signal_type) AS types_csv,
                  MAX(geo_region)                    AS geo
             FROM signals
            WHERE stealth_entity_id = ?""",
        (entity_id,),
    ).fetchone()

    if agg is None or agg["cnt"] == 0:
        # No signals left pointing here — leave the row but zero its score
        cur.execute(
            "UPDATE stealth_entities SET signal_count=0, signal_types='[]', "
            "composite_score=0, is_person=0, founder_confidence=0, "
            "person_role=NULL, watchlist_tier=NULL, watchlist_employer=NULL, "
            "academic_founder=0, academic_institution=NULL, "
            "website_title=NULL, website_description=NULL, "
            "updated_at=? WHERE id=?",
            (datetime.now(timezone.utc).replace(microsecond=0).isoformat(), entity_id),
        )
        return

    types = [t for t in (agg["types_csv"] or "").split(",") if t]
    types_set = set(types)
    score = composite_score(types, agg["last_at"])

    sector_tags = _merge_sector_tags(cur, entity_id)
    best_domain = _pick_primary(cur, entity_id, "primary_domain", "entity_domain")
    best_github = _pick_primary(cur, entity_id, "primary_github", "entity_github")
    best_name = _pick_primary(cur, entity_id, "primary_name", "entity_name")
    best_li = _pick_primary(cur, entity_id, "primary_linkedin", "person_linkedin")

    is_person = int(bool(types_set & _PERSON_SIGNAL_TYPES))
    role = _founder_role_from_signals(cur, entity_id) if is_person else None
    # Watchlist lookup only for person-type entities (ex-employer context
    # only meaningful when we're tracking a person, not a company)
    if is_person:
        wl_tier, wl_employer = _watchlist_lookup(cur, entity_id)
        acad_flag, acad_inst = _academic_lookup(cur, entity_id)
    else:
        wl_tier, wl_employer = None, None
        acad_flag, acad_inst = 0, None
    founder_conf = (
        _compute_founder_confidence(
            types_set,
            role,
            cur,
            entity_id,
            watchlist_tier_val=wl_tier,
            is_academic=bool(acad_flag),
        )
        if is_person
        else 0.0
    )

    # Website enrichment join — zero-cost reuse of the 2,718 scraped
    # homepages already in website_enrichment. Only lookup when we have
    # a primary_domain; non-matches leave the columns NULL.
    web_title, web_desc = _website_lookup(cur, best_domain)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cur.execute(
        """UPDATE stealth_entities SET
             signal_count         = ?,
             signal_types         = ?,
             first_signal_at      = ?,
             last_signal_at       = ?,
             sector_tags          = ?,
             geo_region           = COALESCE(?, geo_region),
             composite_score      = ?,
             is_person            = ?,
             founder_confidence   = ?,
             person_role          = ?,
             watchlist_tier       = ?,
             watchlist_employer   = ?,
             academic_founder     = ?,
             academic_institution = ?,
             website_title        = ?,
             website_description  = ?,
             primary_domain       = COALESCE(primary_domain, ?),
             primary_github       = COALESCE(primary_github, ?),
             primary_linkedin     = COALESCE(primary_linkedin, ?),
             primary_name         = CASE WHEN primary_name='(unknown)' AND ? IS NOT NULL THEN ? ELSE primary_name END,
             updated_at           = ?
           WHERE id = ?""",
        (
            agg["cnt"],
            ensure_json(sorted(set(types))),
            agg["first_at"],
            agg["last_at"],
            sector_tags,
            agg["geo"],
            score,
            is_person,
            founder_conf,
            role,
            wl_tier,
            wl_employer,
            acad_flag,
            acad_inst,
            web_title,
            web_desc,
            best_domain,
            best_github,
            best_li,
            best_name,
            best_name,
            now,
            entity_id,
        ),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_joiner(conn, *, limit: Optional[int] = None, rebuild_all: bool = False) -> dict:
    """Resolve unresolved signals + recompute their aggregates.

    rebuild_all=True drops every signal→entity link and re-resolves from scratch.
    Use after schema changes or weight tweaks.
    """
    cur = conn.cursor()
    stats = {"resolved": 0, "created": 0, "matched": 0, "recomputed": 0}

    if rebuild_all:
        logger.info("rebuild_all=True: clearing all signal→entity links")
        cur.execute("UPDATE signals SET stealth_entity_id = NULL")
        conn.commit()

    # ---- Pass 1: resolve signals ----
    sql = "SELECT * FROM signals WHERE stealth_entity_id IS NULL ORDER BY observed_at DESC"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    unresolved = cur.execute(sql).fetchall()
    logger.info("resolving %d signals", len(unresolved))

    touched: Set[int] = set()
    for sig in unresolved:
        sig_d = dict(sig)
        entity_id = _find_entity_id(cur, sig_d)
        if entity_id is None:
            entity_id = _create_entity(cur, sig_d)
            stats["created"] += 1
        else:
            stats["matched"] += 1
        cur.execute(
            "UPDATE signals SET stealth_entity_id = ? WHERE id = ?",
            (entity_id, sig["id"]),
        )
        touched.add(entity_id)
        stats["resolved"] += 1
    conn.commit()

    # ---- Pass 2: recompute aggregates for every touched (or every, in rebuild mode) ----
    if rebuild_all:
        touched = {r["id"] for r in cur.execute("SELECT id FROM stealth_entities")}
    logger.info("recomputing %d entities", len(touched))
    for eid in touched:
        _recompute_entity(cur, eid)
        stats["recomputed"] += 1
    conn.commit()

    logger.info("joiner done: %s", stats)
    return stats


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    import argparse
    import sqlite3

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--rebuild-all", action="store_true", help="Drop all signal→entity links and re-resolve from scratch"
    )
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        stats = run_joiner(conn, limit=args.limit, rebuild_all=args.rebuild_all)
        print(json.dumps(stats, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
