"""
/signals — multi-signal stealth tracker UI + API.

Replaces the single-source /founders scoreboard with a view that ranks
entities by composite_score across LinkedIn, Handelsregister, news,
crt.sh, GitHub — whatever the joiner has resolved.

Routes:
    GET /signals                — HTML dashboard (filters + table)
    GET /signals/entity/{id}    — HTML detail (signal timeline)
    GET /signals/api/schema.json — agent-friendly contract
    GET /signals/api/entities.json — ranked entity list
    GET /signals/api/entity/{id}.json — single entity + signal history
"""

from __future__ import annotations

import json
import math
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from signals.common import SIGNAL_TYPES, SIGNAL_WEIGHTS
from web.dependencies import get_db, templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def _build_filters(
    *,
    q: Optional[str] = None,
    sector: Optional[str] = None,
    signal_type: Optional[str] = None,
    min_score: Optional[int] = None,
    min_signals: Optional[int] = None,
    starred: Optional[str] = None,
    geo: Optional[str] = None,
) -> tuple[str, list]:
    conds, params = [], []
    if q:
        conds.append("(primary_name LIKE ? OR primary_domain LIKE ? OR primary_github LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if sector:
        # sector_tags is stored as JSON — simple LIKE is good enough for
        # filtering by one tag at a time (we have no FTS on this field)
        conds.append("sector_tags LIKE ?")
        params.append(f'%"{sector}"%')
    if signal_type:
        conds.append("signal_types LIKE ?")
        params.append(f'%"{signal_type}"%')
    if min_score is not None:
        conds.append("composite_score >= ?")
        params.append(min_score)
    if min_signals is not None:
        conds.append("signal_count >= ?")
        params.append(min_signals)
    if starred == "yes":
        conds.append("starred = 1")
    elif starred == "no":
        conds.append("(starred = 0 OR starred IS NULL)")
    if geo:
        conds.append("geo_region = ?")
        params.append(geo)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------

def _clean_display_name(raw: Optional[str]) -> str:
    """LinkedIn snippets sometimes contain multiple people/roles concatenated
    with ' – ' or ' | '. For display, cut at the first such separator so the
    table shows just the primary person name, not the whole blob.
    """
    if not raw:
        return "(unknown)"
    for sep in (" – ", " - ", " | ", " · ", "..."):
        if sep in raw:
            return raw.split(sep, 1)[0].strip()
    return raw.strip()


@router.get("/signals", response_class=HTMLResponse)
async def signals_index(
    request: Request,
    q: Optional[str] = None,
    sector: Optional[str] = None,
    signal_type: Optional[str] = None,
    min_score: Optional[int] = 30,
    min_signals: Optional[int] = 2,
    starred: Optional[str] = None,
    geo: Optional[str] = None,
    sort: str = "score",       # score | last | signals
    page: int = 1,
    per_page: int = 50,
):
    db = get_db()
    try:
        where, params = _build_filters(
            q=q, sector=sector, signal_type=signal_type,
            min_score=min_score, min_signals=min_signals,
            starred=starred, geo=geo,
        )

        sort_sql = {
            "score":    "composite_score DESC, signal_count DESC",
            "last":     "last_signal_at DESC",
            "signals":  "signal_count DESC, composite_score DESC",
        }.get(sort, "composite_score DESC")

        total = db.conn.execute(
            f"SELECT COUNT(*) FROM stealth_entities{where}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.conn.execute(
            f"""SELECT * FROM stealth_entities{where}
                 ORDER BY {sort_sql}
                 LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
        entities = []
        for r in rows:
            d = dict(r)
            d["signal_types_list"] = json.loads(d.get("signal_types") or "[]")
            d["sector_tags_list"]  = json.loads(d.get("sector_tags")  or "[]")
            d["display_name"]      = _clean_display_name(d.get("primary_name"))
            entities.append(d)

        # Summary stats, for the sidebar/header
        stats = {}
        for key, sql in {
            "total":       "SELECT COUNT(*) FROM stealth_entities",
            "multi":       "SELECT COUNT(*) FROM stealth_entities WHERE signal_count >= 2",
            "triple":      "SELECT COUNT(*) FROM stealth_entities WHERE signal_count >= 3",
            "last_7d":     "SELECT COUNT(*) FROM stealth_entities WHERE last_signal_at >= date('now','-7 days')",
            "last_30d":    "SELECT COUNT(*) FROM stealth_entities WHERE last_signal_at >= date('now','-30 days')",
        }.items():
            stats[key] = db.conn.execute(sql).fetchone()[0]

        # Available sector/signal filter options derived from the data
        sectors = []
        for r in db.conn.execute(
            "SELECT sector_tags FROM stealth_entities WHERE sector_tags IS NOT NULL"
        ):
            try:
                for t in json.loads(r["sector_tags"] or "[]"):
                    if isinstance(t, str) and t not in sectors:
                        sectors.append(t)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        sectors.sort()

        total_pages = max(1, math.ceil(total / per_page))

        return templates.TemplateResponse(
            "stealth/index.html",
            {
                "request": request,
                "entities": entities,
                "total": total,
                "stats": stats,
                "sectors": sectors,
                "signal_types": list(SIGNAL_TYPES),
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "filters": {
                    "q": q, "sector": sector, "signal_type": signal_type,
                    "min_score": min_score, "min_signals": min_signals,
                    "starred": starred, "geo": geo, "sort": sort,
                },
            },
        )
    finally:
        db.close()


@router.get("/signals/people", response_class=HTMLResponse)
async def signals_people(
    request: Request,
    q: Optional[str] = None,
    sector: Optional[str] = None,
    min_founder_conf: Optional[int] = 40,
    min_signals: Optional[int] = 1,
    role_only: Optional[str] = None,  # 'yes' = require person_role not NULL
    sort: str = "founder",            # founder | score | last
    page: int = 1,
    per_page: int = 50,
):
    """Person-centric view — one row per founder, not per entity.

    Filtered to is_person=1. Default rank is founder_confidence × composite_score
    so people with the strongest 'actually a founder' signal float to the top.
    """
    db = get_db()
    try:
        conds = ["is_person = 1"]
        params: list = []
        if q:
            conds.append("(primary_name LIKE ? OR primary_linkedin LIKE ? OR person_role LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if sector:
            conds.append("sector_tags LIKE ?")
            params.append(f'%"{sector}"%')
        if min_founder_conf is not None:
            conds.append("founder_confidence >= ?")
            params.append(min_founder_conf)
        if min_signals is not None:
            conds.append("signal_count >= ?")
            params.append(min_signals)
        if role_only == "yes":
            conds.append("person_role IS NOT NULL")
        where = " WHERE " + " AND ".join(conds)

        sort_sql = {
            "founder": "founder_confidence DESC, composite_score DESC, signal_count DESC",
            "score":   "composite_score DESC, founder_confidence DESC",
            "last":    "last_signal_at DESC",
        }.get(sort, "founder_confidence DESC")

        total = db.conn.execute(
            f"SELECT COUNT(*) FROM stealth_entities{where}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.conn.execute(
            f"""SELECT * FROM stealth_entities{where}
                 ORDER BY {sort_sql}
                 LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
        people = []
        for r in rows:
            d = dict(r)
            d["signal_types_list"] = json.loads(d.get("signal_types") or "[]")
            d["sector_tags_list"]  = json.loads(d.get("sector_tags")  or "[]")
            d["display_name"]      = _clean_display_name(d.get("primary_name"))
            people.append(d)

        # Available sectors derived from the data
        sectors = []
        for r in db.conn.execute(
            "SELECT sector_tags FROM stealth_entities "
            "WHERE sector_tags IS NOT NULL AND is_person = 1"
        ):
            try:
                for t in json.loads(r["sector_tags"] or "[]"):
                    if isinstance(t, str) and t not in sectors:
                        sectors.append(t)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        sectors.sort()

        stats = {
            "total_people":    db.conn.execute("SELECT COUNT(*) FROM stealth_entities WHERE is_person = 1").fetchone()[0],
            "high_conf":       db.conn.execute("SELECT COUNT(*) FROM stealth_entities WHERE is_person = 1 AND founder_confidence >= 60").fetchone()[0],
            "has_role":        db.conn.execute("SELECT COUNT(*) FROM stealth_entities WHERE is_person = 1 AND person_role IS NOT NULL").fetchone()[0],
            "has_company":     db.conn.execute("SELECT COUNT(*) FROM stealth_entities WHERE is_person = 1 AND signal_types LIKE '%handelsregister_new%'").fetchone()[0],
        }
        total_pages = max(1, math.ceil(total / per_page))

        return templates.TemplateResponse(
            "stealth/people.html",
            {
                "request": request, "people": people, "total": total,
                "stats": stats, "sectors": sectors,
                "page": page, "per_page": per_page, "total_pages": total_pages,
                "filters": {
                    "q": q, "sector": sector,
                    "min_founder_conf": min_founder_conf,
                    "min_signals": min_signals,
                    "role_only": role_only, "sort": sort,
                },
            },
        )
    finally:
        db.close()


@router.get("/signals/entity/{entity_id}", response_class=HTMLResponse)
async def signals_entity_detail(request: Request, entity_id: int):
    db = get_db()
    try:
        entity = db.conn.execute(
            "SELECT * FROM stealth_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not entity:
            raise HTTPException(status_code=404, detail="Entity not found")
        ent = dict(entity)
        ent["signal_types_list"] = json.loads(ent.get("signal_types") or "[]")
        ent["sector_tags_list"]  = json.loads(ent.get("sector_tags")  or "[]")

        signals = []
        for r in db.conn.execute(
            "SELECT * FROM signals WHERE stealth_entity_id = ? ORDER BY observed_at DESC",
            (entity_id,),
        ):
            d = dict(r)
            try:
                d["sector_tags_list"] = json.loads(d.get("sector_tags") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                d["sector_tags_list"] = []
            try:
                d["raw_payload_pretty"] = json.dumps(
                    json.loads(d["raw_payload"]), indent=2
                ) if d.get("raw_payload") else None
            except (TypeError, ValueError, json.JSONDecodeError):
                d["raw_payload_pretty"] = d.get("raw_payload")
            signals.append(d)

        return templates.TemplateResponse(
            "stealth/detail.html",
            {"request": request, "entity": ent, "signals": signals,
             "signal_weights": SIGNAL_WEIGHTS},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# JSON API (agent-friendly)
# ---------------------------------------------------------------------------

@router.get("/signals/api/schema.json")
async def signals_api_schema():
    """Self-describing contract — agents call this first."""
    return {
        "resource": "signals",
        "description": (
            "Multi-signal stealth-company tracker. Entities aggregated across "
            "LinkedIn stealth bios, Handelsregister new entity registrations, "
            "news mentions, crt.sh new domains, and GitHub new orgs."
        ),
        "endpoints": {
            "/signals/api/entities.json": {
                "method": "GET",
                "description": "Ranked list of stealth entities.",
                "params": {
                    "q":            "Substring search over name/domain/github",
                    "sector":       "Filter by sector tag (robotics|ai|climate|hardware|…)",
                    "signal_type":  f"Filter by signal type ({'|'.join(SIGNAL_TYPES)})",
                    "min_score":    "Minimum composite_score (0-100). Default 30.",
                    "min_signals":  "Minimum distinct signal count. Default 2.",
                    "geo":          "Geo region filter",
                    "sort":         "score|last|signals (default: score)",
                    "page":         "1-indexed",
                    "per_page":     "Default 50, max 500",
                },
            },
            "/signals/api/entity/{id}.json": {
                "method": "GET",
                "description": "Full signal history for one entity.",
            },
        },
        "signal_types":   list(SIGNAL_TYPES),
        "signal_weights": SIGNAL_WEIGHTS,
        "scoring": {
            "formula": "sum(weight for each type) + multi-signal bonus * recency decay",
            "multi_bonus": {"2+types": 15, "3+types": 15, "4+types": 10},
            "recency_decay": {"<=30d": 1.0, "<=90d": 0.85, "<=180d": 0.65,
                              "<=365d": 0.45, ">365d": 0.25},
        },
    }


@router.get("/signals/api/entities.json")
async def signals_api_entities(
    q: Optional[str] = None,
    sector: Optional[str] = None,
    signal_type: Optional[str] = None,
    min_score: Optional[int] = 30,
    min_signals: Optional[int] = 2,
    starred: Optional[str] = None,
    geo: Optional[str] = None,
    sort: str = "score",
    page: int = 1,
    per_page: int = 50,
):
    per_page = max(1, min(per_page, 500))
    db = get_db()
    try:
        where, params = _build_filters(
            q=q, sector=sector, signal_type=signal_type,
            min_score=min_score, min_signals=min_signals,
            starred=starred, geo=geo,
        )
        sort_sql = {
            "score":   "composite_score DESC, signal_count DESC",
            "last":    "last_signal_at DESC",
            "signals": "signal_count DESC, composite_score DESC",
        }.get(sort, "composite_score DESC")

        total = db.conn.execute(
            f"SELECT COUNT(*) FROM stealth_entities{where}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.conn.execute(
            f"""SELECT id, primary_name, primary_domain, primary_github, primary_linkedin,
                       signal_count, signal_types, first_signal_at, last_signal_at,
                       sector_tags, geo_region, composite_score, starred, contacted
                  FROM stealth_entities{where}
                 ORDER BY {sort_sql}
                 LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
        entities = []
        for r in rows:
            d = dict(r)
            d["signal_types"] = json.loads(d.get("signal_types") or "[]")
            d["sector_tags"]  = json.loads(d.get("sector_tags")  or "[]")
            entities.append(d)
        return JSONResponse({
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, math.ceil(total / per_page)),
            "entities": entities,
        })
    finally:
        db.close()


@router.get("/signals/api/people.json")
async def signals_api_people(
    q: Optional[str] = None,
    sector: Optional[str] = None,
    min_founder_conf: Optional[int] = 40,
    min_signals: Optional[int] = 1,
    role_only: Optional[str] = None,
    sort: str = "founder",
    page: int = 1,
    per_page: int = 50,
):
    """Person-centric JSON — mirrors the HTML /signals/people view."""
    per_page = max(1, min(per_page, 500))
    db = get_db()
    try:
        conds = ["is_person = 1"]
        params: list = []
        if q:
            conds.append("(primary_name LIKE ? OR primary_linkedin LIKE ? OR person_role LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if sector:
            conds.append("sector_tags LIKE ?")
            params.append(f'%"{sector}"%')
        if min_founder_conf is not None:
            conds.append("founder_confidence >= ?")
            params.append(min_founder_conf)
        if min_signals is not None:
            conds.append("signal_count >= ?")
            params.append(min_signals)
        if role_only == "yes":
            conds.append("person_role IS NOT NULL")
        where = " WHERE " + " AND ".join(conds)
        sort_sql = {
            "founder": "founder_confidence DESC, composite_score DESC, signal_count DESC",
            "score":   "composite_score DESC, founder_confidence DESC",
            "last":    "last_signal_at DESC",
        }.get(sort, "founder_confidence DESC")

        total = db.conn.execute(f"SELECT COUNT(*) FROM stealth_entities{where}", params).fetchone()[0]
        offset = (page - 1) * per_page
        rows = db.conn.execute(
            f"""SELECT id, primary_name, primary_linkedin, person_role,
                       signal_count, signal_types, first_signal_at, last_signal_at,
                       sector_tags, geo_region, composite_score, founder_confidence
                  FROM stealth_entities{where}
                 ORDER BY {sort_sql}
                 LIMIT ? OFFSET ?""",
            params + [per_page, offset],
        ).fetchall()
        people = []
        for r in rows:
            d = dict(r)
            d["signal_types"] = json.loads(d.get("signal_types") or "[]")
            d["sector_tags"]  = json.loads(d.get("sector_tags")  or "[]")
            d["display_name"] = _clean_display_name(d.get("primary_name"))
            people.append(d)
        return JSONResponse({
            "total": total, "page": page, "per_page": per_page,
            "total_pages": max(1, math.ceil(total / per_page)),
            "people": people,
        })
    finally:
        db.close()


@router.get("/signals/api/entity/{entity_id}.json")
async def signals_api_entity(entity_id: int):
    db = get_db()
    try:
        row = db.conn.execute(
            "SELECT * FROM stealth_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found")
        ent = dict(row)
        ent["signal_types"] = json.loads(ent.get("signal_types") or "[]")
        ent["sector_tags"]  = json.loads(ent.get("sector_tags")  or "[]")

        signals = []
        for s in db.conn.execute(
            "SELECT id, signal_type, observed_at, source_url, "
            "       entity_name, entity_domain, entity_github, "
            "       person_name, person_linkedin, sector_tags, "
            "       geo_region, confidence, raw_payload "
            "  FROM signals "
            " WHERE stealth_entity_id = ? "
            " ORDER BY observed_at DESC",
            (entity_id,),
        ):
            d = dict(s)
            try:
                d["sector_tags"] = json.loads(d.get("sector_tags") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                d["sector_tags"] = []
            try:
                d["raw_payload"] = json.loads(d["raw_payload"]) if d.get("raw_payload") else None
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            signals.append(d)
        return JSONResponse({"entity": ent, "signals": signals})
    finally:
        db.close()
