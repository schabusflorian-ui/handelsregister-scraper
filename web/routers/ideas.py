"""
Ideas router — frontend for the idea-discovery pipeline.

Mirrors the pattern used by /companies etc. (FastAPI + Jinja + HTMX +
ECharts). All data comes from company_ideas / idea_extraction /
idea_clusters / website_enrichment and the SQL views built by
scheduler.jobs.idea_gap_queries.

Routes:
    GET /ideas                          overview page
    GET /ideas/api/heatmap.json         mechanism × sector heatmap data
    GET /ideas/api/stats.json           headline stats (HTMX refresh)
"""

from __future__ import annotations

import gzip
import logging
import os
import time
from typing import Dict, List, Optional

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from web.dependencies import get_db, templates

logger = logging.getLogger(__name__)

router = APIRouter()


# --- helpers ---------------------------------------------------------------

def _ideas_tables_exist(db) -> bool:
    """Cheap check: are the idea-pipeline tables present at all? On a
    fresh Railway deploy they won't be until scrapers + jobs have run."""
    row = db.conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name IN ('company_ideas', 'idea_extraction')"
    ).fetchone()
    return bool(row and row[0] >= 2)


def _ensure_views(db) -> None:
    """Create the gap-query views if a prior run hasn't, and only if the
    backing tables exist — the views themselves reference tables that
    would otherwise error at query time."""
    if not _ideas_tables_exist(db):
        return
    try:
        from scheduler.jobs.idea_gap_queries import setup_views
        setup_views(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to auto-create views: %s", e)


def _safe_query(db, sql: str, params: tuple = ()) -> list:
    """Run a query, returning [] on any error (missing tables, etc.)."""
    try:
        return db.conn.execute(sql, params).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("query skipped (%s): %s", e, sql.split("FROM")[0].strip()[:80])
        return []


def _query_stats(db) -> Dict[str, int]:
    cur = db.conn.cursor()
    def _one(sql: str) -> int:
        try:
            row = cur.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except Exception:  # noqa: BLE001
            return 0
    return {
        "ideas":                _one("SELECT COUNT(*) FROM company_ideas"),
        "extracted":            _one("SELECT COUNT(*) FROM idea_extraction WHERE error IS NULL"),
        "clusters":             _one("SELECT COUNT(*) FROM idea_clusters WHERE cluster_id != -1 AND parent_cluster_id IS NULL"),
        "subclusters":          _one("SELECT COUNT(*) FROM idea_clusters WHERE parent_cluster_id IS NOT NULL"),
        "launch_candidates":    _one("SELECT COUNT(*) FROM v_launch_candidates_loose"),
        "launch_strict":        _one("SELECT COUNT(*) FROM v_launch_candidates_strict"),
        "rebuild_candidates":   _one("SELECT COUNT(*) FROM idea_clusters WHERE era_class='rebuild_candidate'"),
        "hot_clusters":         _one("SELECT COUNT(*) FROM idea_clusters WHERE era_class='hot'"),
        "mechanisms":           _one("SELECT COUNT(*) FROM v_mechanism_totals"),
        "sectors":              _one("SELECT COUNT(*) FROM v_sector_totals"),
        "populated_cells":      _one("SELECT COUNT(*) FROM v_matrix"),
    }


def _heatmap_data(db, n_mechanisms: int = 25, n_sectors: int = 20) -> Dict:
    """Return ECharts-ready heatmap payload.

    Returns:
      {
        "mechanisms": [...],            # y-axis (top N by frequency)
        "sectors":    [...],            # x-axis (top M by frequency)
        "cells":      [[xi, yi, n], ...],
        "max_n":      maximum cell count (for color scale)
      }
    """
    mechs: List[str] = [r[0] for r in _safe_query(
        db,
        "SELECT mechanism FROM v_mechanism_totals ORDER BY n DESC LIMIT ?",
        (n_mechanisms,),
    )]
    sectors: List[str] = [r[0] for r in _safe_query(
        db,
        "SELECT sector FROM v_sector_totals ORDER BY n DESC LIMIT ?",
        (n_sectors,),
    )]
    if not mechs or not sectors:
        return {"mechanisms": [], "sectors": [], "cells": [], "max_n": 0}

    mech_idx = {m: i for i, m in enumerate(mechs)}
    sector_idx = {s: i for i, s in enumerate(sectors)}

    qmarks_m = ",".join("?" * len(mechs))
    qmarks_s = ",".join("?" * len(sectors))
    rows = _safe_query(
        db,
        f"SELECT mechanism, sector, n FROM v_matrix "
        f"WHERE mechanism IN ({qmarks_m}) AND sector IN ({qmarks_s})",
        (*mechs, *sectors),
    )

    cells: List[List[int]] = []
    max_n = 0
    for mech, sect, n in rows:
        cells.append([sector_idx[sect], mech_idx[mech], int(n)])
        if n > max_n:
            max_n = int(n)

    return {"mechanisms": mechs, "sectors": sectors, "cells": cells, "max_n": max_n}


def _top_launch_candidates(db, limit: int = 12) -> List[Dict]:
    rows = _safe_query(db, """
        SELECT ci.id, ci.program, ci.company, ci.year_founded, ci.company_website,
               ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
               ie.customer_size, ie.business_model,
               ie.niche_specificity, ie.moat_type
          FROM idea_extraction ie
          JOIN company_ideas ci ON ci.id = ie.company_idea_id
         WHERE ie.error IS NULL
           AND ie.solo_buildable = 1
           AND ie.ai_first_advantage = 1
           AND ie.moat_type NOT IN ('regulatory', 'capital')
           AND ci.company IS NOT NULL AND ci.company != ''
         ORDER BY ci.year_founded DESC, ci.id DESC
         LIMIT ?
        """, (limit,))
    return [dict(r) for r in rows]


def _top_rebuild_candidates(db, limit: int = 10) -> List[Dict]:
    rows = _safe_query(db, """
        SELECT cluster_id, label, size, median_year, count_2023_plus,
               count_pre_2015, count_2015_2022,
               CASE WHEN parent_cluster_id IS NULL THEN 'parent' ELSE 'sub' END AS level
          FROM idea_clusters
         WHERE era_class = 'rebuild_candidate'
         ORDER BY size DESC
         LIMIT ?
        """, (limit,))
    return [dict(r) for r in rows]


def _era_distribution(db) -> List[Dict]:
    rows = _safe_query(db, """
        SELECT era_class, COUNT(*) AS n, SUM(size) AS companies
          FROM idea_clusters
         WHERE cluster_id != -1
         GROUP BY era_class
         ORDER BY n DESC
        """)
    return [dict(r) for r in rows]


_GAP_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS gap_feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mechanism    TEXT NOT NULL,
    sector       TEXT NOT NULL,
    vote         INTEGER NOT NULL,       -- 1 = interesting, -1 = not interesting
    note         TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gap_fb_pair ON gap_feedback(mechanism, sector);
CREATE INDEX IF NOT EXISTS idx_gap_fb_vote ON gap_feedback(vote);
"""


def _ensure_gap_feedback(db) -> None:
    try:
        db.conn.executescript(_GAP_FEEDBACK_DDL)
        db.conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to ensure gap_feedback: %s", e)


def _top_ranked_gaps(db, limit: int = 20) -> List[Dict]:
    """Top recombination gaps from the scored ranking table, enriched with
    2–3 sample companies that illustrate the mechanism in other sectors and
    the sector in other mechanisms.

    Returns [] if idea_gap_ranking doesn't exist yet (run
    scripts.idea_gap_rank first).
    """
    cur = db.conn.cursor()
    try:
        gaps = cur.execute(
            """
            SELECT rank, mechanism, sector,
                   actual_count, mech_uses, sector_uses,
                   expected_count, gap_size,
                   solo_fraction, ai_fraction, score
              FROM idea_gap_ranking
             ORDER BY rank ASC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("ranking table missing (%s)", e)
        return []

    # Aggregate votes per (mechanism, sector) so the UI can show a tally.
    try:
        vote_rows = cur.execute(
            """
            SELECT mechanism, sector,
                   SUM(CASE WHEN vote =  1 THEN 1 ELSE 0 END) AS up,
                   SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) AS down
              FROM gap_feedback
             GROUP BY mechanism, sector
            """
        ).fetchall()
        votes = {(r["mechanism"], r["sector"]): {"up": r["up"], "down": r["down"]}
                 for r in vote_rows}
    except Exception:  # noqa: BLE001
        votes = {}

    out: List[Dict] = []
    for g in gaps:
        gd = dict(g)
        gd["votes"] = votes.get((gd["mechanism"], gd["sector"]),
                                {"up": 0, "down": 0})
        # Pull 3 example companies that use this mechanism *elsewhere* (in
        # any sector other than the gap sector) — concrete proof it works.
        try:
            row = cur.execute(
                """
                SELECT GROUP_CONCAT(DISTINCT company) AS names
                  FROM (
                    SELECT DISTINCT company
                      FROM v_mechanism_sector_cells
                     WHERE mechanism = ? AND sector != ? AND company != ''
                     LIMIT 3
                  )
                """,
                (gd["mechanism"], gd["sector"]),
            ).fetchone()
            gd["mech_examples"] = (row["names"] or "") if row else ""
        except Exception:  # noqa: BLE001
            gd["mech_examples"] = ""
        out.append(gd)
    return out


def _solo_ai_matrix(db) -> List[Dict]:
    rows = _safe_query(db, """
        SELECT solo_buildable AS solo, ai_first_advantage AS ai_first,
               COUNT(*) AS n
          FROM idea_extraction
         WHERE error IS NULL AND solo_buildable IS NOT NULL AND ai_first_advantage IS NOT NULL
         GROUP BY solo_buildable, ai_first_advantage
        """)
    return [dict(r) for r in rows]


# --- routes ----------------------------------------------------------------

@router.get("/ideas", response_class=HTMLResponse)
async def ideas_overview(request: Request):
    """Idea-discovery overview: stats + heatmap + top picks."""
    db = get_db()
    try:
        _ensure_views(db)
        _ensure_gap_feedback(db)
        context = {
            "stats":              _query_stats(db),
            "era_distribution":   _era_distribution(db),
            "solo_ai_matrix":     _solo_ai_matrix(db),
            "launch_candidates":  _top_launch_candidates(db, limit=12),
            "rebuild_candidates": _top_rebuild_candidates(db, limit=10),
            "top_gaps":           _top_ranked_gaps(db, limit=20),
        }
        return templates.TemplateResponse(
            name="ideas/overview.html",
            request=request,
            context=context,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Launch candidates listing  — /ideas/launch-candidates
# ---------------------------------------------------------------------------

_LAUNCH_FACETS = (
    ("program",        "ci.program"),
    ("customer_size",  "ie.customer_size"),
    ("business_model", "ie.business_model"),
    ("moat_type",      "ie.moat_type"),
    ("niche",          "ie.niche_specificity"),
)


def _facet_counts(db, where_sql: str, params: tuple) -> Dict[str, List[Dict]]:
    """Return counts per facet under the current filter set. Used to render
    the sidebar facet groups with live counts."""
    out: Dict[str, List[Dict]] = {}
    cur = db.conn.cursor()
    for key, col in _LAUNCH_FACETS:
        rows = cur.execute(
            f"""
            SELECT {col} AS v, COUNT(*) AS n
              FROM idea_extraction ie
              JOIN company_ideas   ci ON ci.id = ie.company_idea_id
             WHERE {where_sql}
               AND {col} IS NOT NULL AND {col} != ''
             GROUP BY {col}
             ORDER BY n DESC
             LIMIT 20
            """,
            params,
        ).fetchall()
        out[key] = [{"value": r["v"], "count": r["n"]} for r in rows]
    return out


def _build_launch_filter(
    q: Optional[str],
    program: Optional[str],
    customer_size: Optional[str],
    business_model: Optional[str],
    moat: Optional[str],
    niche: Optional[str],
    solo: Optional[str],
    ai_first: Optional[str],
    strict_only: bool,
) -> tuple[str, list]:
    """Build the WHERE clause + params. Returns (sql, params) as a tuple."""
    clauses = [
        "ie.error IS NULL",
        "ie.solo_buildable = 1",
        "ie.ai_first_advantage = 1",
        # Loose default: exclude regulatory / capital-heavy moats since those
        # aren't microbusiness-shape. strict_only narrows further to moat=none.
    ]
    params: list = []
    if strict_only:
        clauses.append("ie.moat_type = 'none'")
    else:
        clauses.append("ie.moat_type NOT IN ('regulatory', 'capital')")

    if program:
        clauses.append("ci.program = ?")
        params.append(program)
    if customer_size:
        clauses.append("ie.customer_size = ?")
        params.append(customer_size)
    if business_model:
        clauses.append("ie.business_model = ?")
        params.append(business_model)
    if moat:
        clauses.append("ie.moat_type = ?")
        params.append(moat)
    if niche:
        clauses.append("ie.niche_specificity = ?")
        params.append(niche)
    if solo in ("0", "1"):
        clauses.append("ie.solo_buildable = ?")
        params.append(int(solo))
    if ai_first in ("0", "1"):
        clauses.append("ie.ai_first_advantage = ?")
        params.append(int(ai_first))
    # FTS search across company_ideas_fts + idea_extraction_fts. We union the
    # rowids into a subquery and intersect with the main filter.
    if q:
        # Use a prefix-enabled query so "health" matches "healthcare" etc.
        fts_q = " ".join(f"{w}*" for w in q.split() if w.strip())
        clauses.append(
            "ci.id IN ("
            "  SELECT rowid FROM company_ideas_fts   WHERE company_ideas_fts   MATCH ?"
            "  UNION"
            "  SELECT rowid FROM idea_extraction_fts WHERE idea_extraction_fts MATCH ?"
            ")"
        )
        params.extend([fts_q, fts_q])
    return " AND ".join(clauses), params


@router.get("/ideas/shortlist")
async def ideas_shortlist():
    """Quick alias for the top-scoring launch candidates. Redirects to
    /ideas/launch-candidates sorted by opportunity_score, page size 50.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(
        "/ideas/launch-candidates?sort=score&per_page=50",
        status_code=302,
    )


@router.get("/ideas/launch-candidates", response_class=HTMLResponse)
async def ideas_launch_candidates(
    request: Request,
    q: Optional[str] = None,
    program: Optional[str] = None,
    customer_size: Optional[str] = None,
    business_model: Optional[str] = None,
    moat: Optional[str] = None,
    niche: Optional[str] = None,
    solo: Optional[str] = None,
    ai_first: Optional[str] = None,
    strict: Optional[str] = None,
    sort: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Browse launch candidates with filters + FTS search + pagination.

    Loose default (moat != regulatory/capital) matches v_launch_candidates;
    pass strict=1 to narrow to moat=none.
    """
    db = get_db()
    try:
        _ensure_views(db)
        strict_only = strict == "1"
        where_sql, params = _build_launch_filter(
            q, program, customer_size, business_model, moat, niche,
            solo, ai_first, strict_only,
        )

        # Sort options: score (default — newly added), recent, name.
        sort_sql = {
            "score":  "ci.opportunity_score DESC NULLS LAST, ci.year_founded DESC NULLS LAST, ci.id DESC",
            "name":   "ci.company ASC",
            "recent": "ci.year_founded DESC NULLS LAST, ci.id DESC",
        }.get(sort or "score", "ci.opportunity_score DESC NULLS LAST, ci.year_founded DESC NULLS LAST, ci.id DESC")

        cur = db.conn.cursor()
        total_row = cur.execute(
            f"SELECT COUNT(*) FROM idea_extraction ie "
            f"JOIN company_ideas ci ON ci.id = ie.company_idea_id "
            f"WHERE {where_sql}",
            params,
        ).fetchone()
        total = int(total_row[0]) if total_row else 0

        offset = max(0, (page - 1) * per_page)
        rows = cur.execute(
            f"""
            SELECT ci.id, ci.program, ci.company, ci.year_founded,
                   ci.company_website, ci.cluster_id,
                   ci.opportunity_score, ci.opportunity_breakdown,
                   ie.problem_statement, ie.customer_verticals,
                   ie.mechanism_tags, ie.sector_tags,
                   ie.customer_size, ie.business_model,
                   ie.moat_type, ie.niche_specificity,
                   ie.solo_buildable_reasoning, ie.ai_first_reasoning
              FROM idea_extraction ie
              JOIN company_ideas   ci ON ci.id = ie.company_idea_id
             WHERE {where_sql}
             ORDER BY {sort_sql}
             LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        ).fetchall()

        total_pages = max(1, (total + per_page - 1) // per_page)
        facets = _facet_counts(db, where_sql, tuple(params))

        # Rebuild canonical querystring (no &page) so the pagination partial
        # can preserve filters.
        from urllib.parse import urlencode
        qs_parts: list[tuple[str, str]] = []
        for k, v in [
            ("q", q), ("program", program), ("customer_size", customer_size),
            ("business_model", business_model), ("moat", moat),
            ("niche", niche), ("solo", solo), ("ai_first", ai_first),
            ("strict", "1" if strict_only else None), ("sort", sort),
            ("per_page", str(per_page) if per_page != 25 else None),
        ]:
            if v:
                qs_parts.append((k, v))
        base_qs = urlencode(qs_parts)

        return templates.TemplateResponse(
            name="ideas/launch_candidates.html",
            request=request,
            context={
                "total": total,
                "rows": [dict(r) for r in rows],
                "facets": facets,
                "filters": {
                    "q": q or "",
                    "program": program or "",
                    "customer_size": customer_size or "",
                    "business_model": business_model or "",
                    "moat": moat or "",
                    "niche": niche or "",
                    "solo": solo or "",
                    "ai_first": ai_first or "",
                    "strict": "1" if strict_only else "",
                    "sort": sort or "recent",
                },
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "base_qs": base_qs,
            },
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Agent-friendly JSON API
# ---------------------------------------------------------------------------
#
# These are the structured read endpoints designed for programmatic use
# (LLM agents, scripts, dashboards). Stable field names, no HTML, bounded
# payloads via explicit limit. FastAPI also exposes a machine-readable
# contract at /openapi.json.


def _idea_row_dict(row) -> Dict:
    """Common JSON shape for a single idea row, used by multiple endpoints."""
    import json as _j
    d = dict(row)
    for k in ("mechanism_tags", "sector_tags", "customer_verticals"):
        v = d.get(k)
        if v:
            try:
                d[k] = _j.loads(v)
            except Exception:  # noqa: BLE001
                d[k] = []
        else:
            d[k] = []
    return d


@router.get("/ideas/api/search.json")
async def ideas_api_search(
    q: str,
    limit: int = 50,
    offset: int = 0,
):
    """FTS5 search, JSON. Prefix-matching enabled.

    Response:  { "query": str, "total": int, "offset": int, "results": [...] }
    """
    db = get_db()
    try:
        _ensure_views(db)
        fts_q = " ".join(f"{w}*" for w in q.split() if w.strip())
        if not fts_q:
            return JSONResponse({"query": q, "total": 0, "offset": offset, "results": []})
        cur = db.conn.cursor()
        ids_sql = """
            SELECT ci.id FROM company_ideas ci
             WHERE ci.id IN (
               SELECT rowid FROM company_ideas_fts
                WHERE company_ideas_fts MATCH ?
               UNION
               SELECT rowid FROM idea_extraction_fts
                WHERE idea_extraction_fts MATCH ?
             )
        """
        total = cur.execute(f"SELECT COUNT(*) FROM ({ids_sql})",
                            (fts_q, fts_q)).fetchone()[0]
        rows = cur.execute(
            f"""
            SELECT ci.id, ci.program, ci.company, ci.year_founded,
                   ci.one_liner, ci.company_website, ci.cluster_id,
                   ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
                   ie.solo_buildable, ie.ai_first_advantage,
                   ie.business_model, ie.moat_type, ie.niche_specificity
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
             WHERE ci.id IN ({ids_sql})
             ORDER BY ci.year_founded DESC NULLS LAST, ci.id DESC
             LIMIT ? OFFSET ?
            """,
            (fts_q, fts_q, min(limit, 200), offset),
        ).fetchall()
        return JSONResponse({
            "query": q,
            "total": int(total),
            "offset": offset,
            "results": [_idea_row_dict(r) for r in rows],
        })
    finally:
        db.close()


@router.get("/ideas/api/cluster/{cluster_id}.json")
async def ideas_api_cluster(cluster_id: int, limit: int = 50):
    """Cluster metadata + first N members as JSON."""
    db = get_db()
    try:
        _ensure_views(db)
        c = _load_cluster(db, cluster_id)
        if c is None:
            raise HTTPException(status_code=404, detail="cluster not found")
        cur = db.conn.cursor()
        members = [_idea_row_dict(r) for r in cur.execute(
            """
            SELECT ci.id, ci.program, ci.company, ci.year_founded,
                   ci.one_liner, ci.company_website, ci.cluster_id,
                   ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
                   ie.solo_buildable, ie.ai_first_advantage,
                   ie.business_model, ie.moat_type
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
             WHERE ci.cluster_id = ?
             ORDER BY ci.year_founded DESC NULLS LAST, ci.id DESC
             LIMIT ?
            """,
            (cluster_id, min(limit, 200)),
        ).fetchall()]
        return JSONResponse({"cluster": c, "members": members})
    finally:
        db.close()


@router.get("/ideas/api/gap/{mechanism}/{sector}.json")
async def ideas_api_gap(mechanism: str, sector: str, limit: int = 20):
    """Gap drill: the ranking row + proof examples.

    'Proof' = rows that use the mechanism in OTHER sectors (showing it works)
    and rows that use the sector with OTHER mechanisms (showing demand).
    """
    db = get_db()
    try:
        _ensure_views(db)
        cur = db.conn.cursor()
        ranking = cur.execute(
            "SELECT * FROM idea_gap_ranking WHERE mechanism = ? AND sector = ?",
            (mechanism, sector),
        ).fetchone()

        def _members(clause: str, params: tuple, limit: int) -> List[Dict]:
            sql = f"""
                SELECT DISTINCT ci.id, ci.company, ci.program, ci.year_founded,
                       ci.one_liner, ci.company_website
                  FROM v_mechanism_sector_cells v
                  JOIN company_ideas ci ON ci.id = v.company_id
                 WHERE {clause}
                   AND ci.company IS NOT NULL AND ci.company != ''
                 ORDER BY ci.year_founded DESC NULLS LAST
                 LIMIT ?
            """
            try:
                return [dict(r) for r in cur.execute(sql, (*params, limit)).fetchall()]
            except Exception:  # noqa: BLE001
                return []

        mech_elsewhere = _members(
            "v.mechanism = ? AND v.sector != ?",
            (mechanism, sector),
            min(limit, 50),
        )
        sector_elsewhere = _members(
            "v.sector = ? AND v.mechanism != ?",
            (sector, mechanism),
            min(limit, 50),
        )
        in_cell = _members(
            "v.mechanism = ? AND v.sector = ?",
            (mechanism, sector),
            min(limit, 50),
        )

        return JSONResponse({
            "mechanism": mechanism,
            "sector": sector,
            "ranking": dict(ranking) if ranking else None,
            "in_cell": in_cell,
            "mechanism_proven_elsewhere": mech_elsewhere,
            "sector_active_with_other_mechanisms": sector_elsewhere,
        })
    finally:
        db.close()


@router.get("/ideas/api/gaps.json")
async def ideas_api_gaps(limit: int = 50):
    """Top N recombination gaps as JSON."""
    db = get_db()
    try:
        cur = db.conn.cursor()
        try:
            rows = cur.execute(
                "SELECT * FROM idea_gap_ranking ORDER BY rank ASC LIMIT ?",
                (min(limit, 500),),
            ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        return JSONResponse({"count": len(rows), "results": [dict(r) for r in rows]})
    finally:
        db.close()


@router.get("/ideas/api/idea/{idea_id}.json")
async def ideas_api_idea(idea_id: int):
    """Full idea row + extraction + website snapshot as JSON."""
    db = get_db()
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            """
            SELECT ci.*, ie.problem_statement, ie.customer_verticals,
                   ie.mechanism_tags, ie.sector_tags, ie.customer_size,
                   ie.business_model, ie.solo_buildable,
                   ie.solo_buildable_reasoning, ie.ai_first_advantage,
                   ie.ai_first_reasoning, ie.moat_type, ie.niche_specificity,
                   we.meta_description AS web_meta, we.hero_h1 AS web_h1,
                   we.hero_text AS web_hero, we.final_url AS web_final_url
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
         LEFT JOIN website_enrichment we ON we.normalized_website = ci.normalized_website
             WHERE ci.id = ?
            """,
            (idea_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse(_idea_row_dict(row))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Map  — /ideas/map  (2-D UMAP scatter, populated by scripts.compute_idea_map)
# ---------------------------------------------------------------------------

@router.get("/ideas/map", response_class=HTMLResponse)
async def ideas_map(request: Request):
    db = get_db()
    try:
        _ensure_views(db)
        # Quick sanity: how many rows have umap coords? The map page guides
        # the user to run compute_idea_map if not populated.
        cur = db.conn.cursor()
        try:
            n_coords = cur.execute(
                "SELECT COUNT(*) FROM company_ideas "
                "WHERE umap_x IS NOT NULL AND umap_y IS NOT NULL"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            n_coords = 0
        return templates.TemplateResponse(
            name="ideas/map.html",
            request=request,
            context={"n_coords": int(n_coords or 0)},
        )
    finally:
        db.close()


@router.get("/ideas/api/map.json")
async def ideas_api_map(
    mechanism: Optional[str] = None,
    sector: Optional[str] = None,
    program: Optional[str] = None,
    solo: Optional[str] = None,
    ai_first: Optional[str] = None,
    limit: int = 25000,
):
    """Map scatter data. One point per idea with umap coords.

    Returns a compact row format (array-of-arrays) to keep payload small
    for ECharts. Each point is [x, y, cluster_id, idea_id].

    Filters are SQL-side and cumulative — all optional.
    """
    db = get_db()
    try:
        clauses = [
            "ci.umap_x IS NOT NULL",
            "ci.umap_y IS NOT NULL",
        ]
        params: list = []
        joined = ""
        if mechanism or sector or solo in ("0", "1") or ai_first in ("0", "1"):
            joined = "LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id"
        if program:
            clauses.append("ci.program = ?")
            params.append(program)
        if solo in ("0", "1"):
            clauses.append("ie.solo_buildable = ?")
            params.append(int(solo))
        if ai_first in ("0", "1"):
            clauses.append("ie.ai_first_advantage = ?")
            params.append(int(ai_first))
        if mechanism:
            clauses.append("ie.mechanism_tags LIKE ?")
            params.append(f"%\"{mechanism}\"%")
        if sector:
            clauses.append("ie.sector_tags LIKE ?")
            params.append(f"%\"{sector}\"%")
        where_sql = " AND ".join(clauses)
        sql = f"""
            SELECT ci.id, ci.umap_x, ci.umap_y, ci.cluster_id,
                   ci.program, ci.company
              FROM company_ideas ci
              {joined}
             WHERE {where_sql}
             LIMIT ?
        """
        params.append(int(limit))
        rows = db.conn.execute(sql, params).fetchall()

        # Programs for the filter select. Static across filters so compute
        # once.
        programs = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT program FROM company_ideas ORDER BY program"
        )]

        points = [
            [float(r["umap_x"]), float(r["umap_y"]),
             int(r["cluster_id"]) if r["cluster_id"] is not None else -9999,
             int(r["id"]), r["program"],
             (r["company"] or "")[:60]]
            for r in rows
        ]
        return JSONResponse({
            "count": len(points),
            "programs": programs,
            "points": points,
        })
    finally:
        db.close()


@router.get("/ideas/api/idea-card/{idea_id}")
async def ideas_api_idea_card(idea_id: int):
    """Small card payload for the map tooltip / side panel — enough to
    decide whether to drill into the full /ideas/{id} page without a
    round-trip.
    """
    db = get_db()
    try:
        cur = db.conn.cursor()
        row = cur.execute(
            """
            SELECT ci.id, ci.company, ci.program, ci.year_founded,
                   ci.one_liner, ci.company_website, ci.cluster_id,
                   ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
                   ie.solo_buildable, ie.ai_first_advantage,
                   ie.business_model, ie.moat_type,
                   c.label AS cluster_label
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
         LEFT JOIN idea_clusters c ON c.cluster_id = ci.cluster_id
             WHERE ci.id = ?
            """,
            (idea_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        import json as _j
        d = dict(row)
        for k in ("mechanism_tags", "sector_tags"):
            try:
                d[k] = _j.loads(d[k]) if d[k] else []
            except Exception:  # noqa: BLE001
                d[k] = []
        return JSONResponse(d)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Global search  — /ideas/search
# ---------------------------------------------------------------------------

@router.get("/ideas/search", response_class=HTMLResponse)
async def ideas_search(
    request: Request,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """FTS5 search across company_ideas + idea_extraction for ALL ideas
    (not just launch candidates). Single search box, simple result list.
    """
    db = get_db()
    try:
        _ensure_views(db)
        rows: List[Dict] = []
        total = 0
        if q and q.strip():
            fts_q = " ".join(f"{w}*" for w in q.split() if w.strip())
            cur = db.conn.cursor()
            # Union rowids from both FTS tables, dedupe to company_idea.id.
            # Rank by BM25 if available; fall back to insertion order.
            ids_sql = """
                SELECT ci.id
                  FROM company_ideas ci
                 WHERE ci.id IN (
                   SELECT rowid FROM company_ideas_fts
                    WHERE company_ideas_fts MATCH ?
                   UNION
                   SELECT rowid FROM idea_extraction_fts
                    WHERE idea_extraction_fts MATCH ?
                 )
            """
            total_row = cur.execute(
                f"SELECT COUNT(*) FROM ({ids_sql})",
                (fts_q, fts_q),
            ).fetchone()
            total = int(total_row[0]) if total_row else 0

            offset = max(0, (page - 1) * per_page)
            rows = [dict(r) for r in cur.execute(
                f"""
                SELECT ci.id, ci.program, ci.company, ci.year_founded,
                       ci.one_liner, ci.company_website, ci.cluster_id,
                       ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
                       ie.solo_buildable, ie.ai_first_advantage,
                       ie.business_model, ie.moat_type
                  FROM company_ideas ci
             LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
                 WHERE ci.id IN ({ids_sql})
                 ORDER BY ci.year_founded DESC NULLS LAST, ci.id DESC
                 LIMIT ? OFFSET ?
                """,
                (fts_q, fts_q, per_page, offset),
            )]

        total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
        from urllib.parse import urlencode
        base_qs = urlencode([("q", q or "")])

        return templates.TemplateResponse(
            name="ideas/search.html",
            request=request,
            context={
                "q": q or "",
                "rows": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "base_qs": base_qs,
            },
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Idea detail  — /ideas/{id}
# ---------------------------------------------------------------------------

@router.get("/ideas/{idea_id:int}", response_class=HTMLResponse)
async def ideas_detail(request: Request, idea_id: int):
    """Full detail for one idea: problem + extraction + website + cluster."""
    db = get_db()
    try:
        _ensure_views(db)
        cur = db.conn.cursor()
        row = cur.execute(
            """
            SELECT ci.*,
                   ie.problem_statement, ie.customer_verticals,
                   ie.mechanism_tags, ie.sector_tags,
                   ie.customer_size, ie.business_model,
                   ie.solo_buildable, ie.solo_buildable_reasoning,
                   ie.ai_first_advantage, ie.ai_first_reasoning,
                   ie.moat_type, ie.niche_specificity, ie.error AS ie_error,
                   we.meta_description AS web_meta,
                   we.hero_h1 AS web_h1, we.hero_text AS web_hero,
                   we.title AS web_title, we.final_url AS web_final_url,
                   c.label AS cluster_label, c.era_class AS cluster_era
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
         LEFT JOIN website_enrichment we ON we.normalized_website = ci.normalized_website
         LEFT JOIN idea_clusters c ON c.cluster_id = ci.cluster_id
             WHERE ci.id = ?
            """,
            (idea_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"idea {idea_id} not found")

        # Neighbors in the same cluster — cheap "similar" view.
        neighbors: List[Dict] = []
        if row["cluster_id"] is not None and row["cluster_id"] != -1:
            neighbors = [dict(r) for r in cur.execute(
                """
                SELECT id, company, program, year_founded, one_liner
                  FROM company_ideas
                 WHERE cluster_id = ? AND id != ?
                 ORDER BY year_founded DESC NULLS LAST
                 LIMIT 8
                """,
                (row["cluster_id"], idea_id),
            )]

        # Semantic "similar to this" — from pre-computed idea_nearest,
        # spans the whole corpus (not restricted to one cluster).
        similar: List[Dict] = [dict(r) for r in cur.execute(
            """
            SELECT n.rank, n.similarity,
                   ci.id, ci.company, ci.program, ci.year_founded,
                   ci.one_liner, ci.opportunity_score,
                   c.label AS cluster_label, c.llm_label,
                   ci.cluster_id
              FROM idea_nearest n
              JOIN company_ideas ci ON ci.id = n.neighbor_id
         LEFT JOIN idea_clusters  c ON c.cluster_id = ci.cluster_id
             WHERE n.company_idea_id = ?
             ORDER BY n.rank
             LIMIT 10
            """,
            (idea_id,),
        )]

        return templates.TemplateResponse(
            name="ideas/detail.html",
            request=request,
            context={"r": dict(row), "neighbors": neighbors, "similar": similar},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cluster drill-down  — /ideas/clusters/{cluster_id}
# ---------------------------------------------------------------------------

def _load_cluster(db, cluster_id: int) -> Optional[Dict]:
    """Full cluster row with JSON columns parsed + parent's label hydrated.

    Single source of truth for cluster metadata across the drill page, the
    sunburst endpoint, and the agent JSON API. Supersedes the earlier
    `_cluster_header` helper so there's one canonical cluster fetch path.
    """
    row = db.conn.execute(
        """
        SELECT c.cluster_id, c.size, c.label, c.top_terms, c.top_tags,
               c.top_programs, c.representative_ids,
               c.min_year, c.median_year, c.max_year,
               c.count_pre_2015, c.count_2015_2022, c.count_2023_plus,
               c.year_coverage_pct, c.era_class, c.parent_cluster_id,
               p.label AS parent_label, p.era_class AS parent_era
          FROM idea_clusters c
     LEFT JOIN idea_clusters p ON p.cluster_id = c.parent_cluster_id
         WHERE c.cluster_id = ?
        """,
        (cluster_id,),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    import json as _j
    for f in ("top_terms", "top_tags", "representative_ids"):
        try:
            d[f] = _j.loads(d[f]) if d[f] else []
        except Exception:  # noqa: BLE001
            d[f] = []
    try:
        d["top_programs"] = _j.loads(d["top_programs"]) if d["top_programs"] else {}
    except Exception:  # noqa: BLE001
        d["top_programs"] = {}
    return d


@router.get("/ideas/clusters/{cluster_id}", response_class=HTMLResponse)
async def ideas_cluster_detail(
    request: Request,
    cluster_id: int,
    q: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Cluster drill page: label, era, top terms/tags/programs, members."""
    db = get_db()
    try:
        _ensure_views(db)
        c = _load_cluster(db, cluster_id)
        if c is None:
            raise HTTPException(status_code=404, detail=f"cluster {cluster_id} not found")

        cur = db.conn.cursor()

        # Sub-clusters if this is a parent. The clustering job stores
        # parent_cluster_id on children — pick them up here so navigation
        # works in both directions.
        subs = cur.execute(
            """
            SELECT cluster_id, label, size, median_year, era_class
              FROM idea_clusters
             WHERE parent_cluster_id = ?
             ORDER BY size DESC
            """,
            (cluster_id,),
        ).fetchall()

        parent = None
        siblings: List[Dict] = []
        if c["parent_cluster_id"] is not None:
            parent = _load_cluster(db, c["parent_cluster_id"])
            siblings = _cluster_siblings(db, c["parent_cluster_id"], cluster_id)

        # Cluster-internal top-mechanism / top-sector breakdown — computed
        # from the cluster's actual extractions rather than the stored
        # top_tags column (which was summarised at clustering time and may
        # lag new rows). Used for inline bar charts on the drill page.
        breakdown = _cluster_mechanism_sector_breakdown(db, cluster_id)

        # Members — with optional FTS search restricted to this cluster.
        clauses = ["ci.cluster_id = ?"]
        params: list = [cluster_id]
        if q:
            fts_q = " ".join(f"{w}*" for w in q.split() if w.strip())
            clauses.append(
                "ci.id IN ("
                "  SELECT rowid FROM company_ideas_fts   WHERE company_ideas_fts   MATCH ?"
                "  UNION"
                "  SELECT rowid FROM idea_extraction_fts WHERE idea_extraction_fts MATCH ?"
                ")"
            )
            params.extend([fts_q, fts_q])
        where_sql = " AND ".join(clauses)

        total = cur.execute(
            f"SELECT COUNT(*) FROM company_ideas ci WHERE {where_sql}",
            params,
        ).fetchone()[0]

        offset = max(0, (page - 1) * per_page)
        members = cur.execute(
            f"""
            SELECT ci.id, ci.program, ci.company, ci.year_founded,
                   ci.company_website, ci.one_liner,
                   ie.problem_statement, ie.mechanism_tags, ie.sector_tags,
                   ie.business_model, ie.moat_type, ie.niche_specificity,
                   ie.solo_buildable, ie.ai_first_advantage
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
             WHERE {where_sql}
             ORDER BY ci.year_founded DESC NULLS LAST, ci.id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        ).fetchall()

        total_pages = max(1, (total + per_page - 1) // per_page)

        # Hydrate representative ideas (full row each) so the sidebar has
        # concrete examples, not just raw IDs.
        rep_ids = c.get("representative_ids") or []
        reps: List[Dict] = []
        if rep_ids:
            placeholders = ",".join("?" * len(rep_ids))
            reps = [dict(r) for r in cur.execute(
                f"""
                SELECT id, company, program, year_founded, one_liner,
                       company_website
                  FROM company_ideas
                 WHERE id IN ({placeholders})
                 LIMIT 8
                """,
                rep_ids,
            )]

        from urllib.parse import urlencode
        qs_parts = []
        if q:
            qs_parts.append(("q", q))
        if per_page != 25:
            qs_parts.append(("per_page", str(per_page)))
        base_qs = urlencode(qs_parts)

        return templates.TemplateResponse(
            name="ideas/cluster_detail.html",
            request=request,
            context={
                "cluster": c,
                "parent": parent,
                "subclusters": [dict(r) for r in subs],
                "siblings": siblings,
                "breakdown": breakdown,
                "members": [dict(r) for r in members],
                "representatives": reps,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "q": q or "",
                "base_qs": base_qs,
            },
        )
    finally:
        db.close()


@router.get("/ideas/api/heatmap.json")
async def ideas_api_heatmap(n_mechanisms: int = 25, n_sectors: int = 20):
    """Mechanism × sector heatmap data for the overview chart."""
    db = get_db()
    try:
        _ensure_views(db)
        return JSONResponse(_heatmap_data(db, n_mechanisms, n_sectors))
    finally:
        db.close()


@router.get("/ideas/api/stats.json")
async def ideas_api_stats():
    db = get_db()
    try:
        _ensure_views(db)
        return JSONResponse(_query_stats(db))
    finally:
        db.close()


class GapVote(BaseModel):
    mechanism: str = Field(..., min_length=1, max_length=120)
    sector:    str = Field(..., min_length=1, max_length=120)
    # 1 = interesting / keep surfacing; -1 = not interesting; 0 = clear
    vote:      int = Field(..., ge=-1, le=1)
    note:      Optional[str] = Field(default=None, max_length=500)


@router.post("/ideas/api/gap-feedback")
async def ideas_gap_feedback(payload: GapVote):
    """Record a thumbs up/down on a (mechanism × sector) gap. Posting
    vote=0 clears prior votes for the pair (a soft "undo").
    """
    db = get_db()
    try:
        _ensure_gap_feedback(db)
        cur = db.conn.cursor()
        # Clear any prior vote for this pair so up+down can't both be stored
        # at once. Single-user semantics: the latest vote wins.
        cur.execute(
            "DELETE FROM gap_feedback WHERE mechanism = ? AND sector = ?",
            (payload.mechanism, payload.sector),
        )
        if payload.vote != 0:
            cur.execute(
                "INSERT INTO gap_feedback (mechanism, sector, vote, note) "
                "VALUES (?, ?, ?, ?)",
                (payload.mechanism, payload.sector,
                 payload.vote, payload.note),
            )
        db.conn.commit()
        row = cur.execute(
            """
            SELECT
              SUM(CASE WHEN vote =  1 THEN 1 ELSE 0 END) AS up,
              SUM(CASE WHEN vote = -1 THEN 1 ELSE 0 END) AS down
              FROM gap_feedback
             WHERE mechanism = ? AND sector = ?
            """,
            (payload.mechanism, payload.sector),
        ).fetchone()
        return JSONResponse({
            "ok": True,
            "up":   int(row["up"] or 0) if row else 0,
            "down": int(row["down"] or 0) if row else 0,
        })
    finally:
        db.close()


# --- seed endpoint ---------------------------------------------------------

SEED_TABLES = [
    "company_ideas",
    "idea_extraction",
    "idea_extraction_tag_backup",
    "idea_clusters",
    "website_enrichment",
    # Added for the gap-ranking + tag-normalization pipeline additions.
    # tag_alias and idea_gap_ranking are both rebuildable locally but it's
    # cheaper to push them than to re-run tag_normalize + idea_gap_rank on
    # the server. gap_feedback is deliberately excluded — Railway is the
    # authoritative source for user thumbs-up/down.
    "tag_alias",
    "idea_gap_ranking",
    # Pre-computed top-10 nearest-neighbour table for the
    # /ideas/api/similar endpoint — built by idea_similar_job locally,
    # shipped so Railway doesn't need ML deps to answer similarity.
    "idea_nearest",
]

# Max upload body; raise if your seed grows beyond this (currently ~13MB).
SEED_MAX_BYTES = 64 * 1024 * 1024


def _seed_token_required() -> Optional[str]:
    """If IDEAS_SEED_TOKEN is set in env, callers must supply it via the
    X-Seed-Token header. If not set, endpoint is open (matches the
    existing /admin/* pattern on this codebase)."""
    tok = os.environ.get("IDEAS_SEED_TOKEN")
    return tok or None


@router.post("/admin/ideas/seed")
async def admin_ideas_seed(request: Request, file: UploadFile = File(...)):
    """Accept a .sql.gz produced by scripts/dump_idea_tables.py and load
    it into the DB, replacing the idea-pipeline tables."""
    required = _seed_token_required()
    if required and request.headers.get("x-seed-token") != required:
        raise HTTPException(status_code=401, detail="invalid or missing X-Seed-Token")

    t0 = time.monotonic()
    raw = await file.read()
    if len(raw) > SEED_MAX_BYTES:
        raise HTTPException(413, detail=f"upload too large ({len(raw)} > {SEED_MAX_BYTES})")

    try:
        if file.filename and file.filename.endswith(".gz"):
            sql = gzip.decompress(raw).decode("utf-8")
        else:
            sql = raw.decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, detail=f"failed to decode upload: {e}")

    db = get_db()
    try:
        cur = db.conn.cursor()
        # Foreign keys off during dump restore; sqlite3 .dump also adds its own
        # PRAGMA statements which executescript tolerates.
        try:
            cur.executescript(sql)
            db.conn.commit()
        except Exception as e:  # noqa: BLE001
            db.conn.rollback()
            raise HTTPException(500, detail=f"restore failed: {e}")

        counts = {}
        for tbl in SEED_TABLES:
            try:
                counts[tbl] = cur.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except Exception:  # noqa: BLE001
                counts[tbl] = None

        # Rebuild downstream artifacts: FTS tables + gap-query views.
        rebuilt = {}
        try:
            from scheduler.jobs.idea_fts_setup_job import run as run_fts
            run_fts(os.environ.get("DATABASE_PATH", "/data/handelsregister.db"))
            rebuilt["fts"] = "ok"
        except Exception as e:  # noqa: BLE001
            rebuilt["fts"] = f"skipped: {e}"
        try:
            from scheduler.jobs.idea_gap_queries import setup_views
            setup_views(db)
            rebuilt["views"] = "ok"
        except Exception as e:  # noqa: BLE001
            rebuilt["views"] = f"skipped: {e}"

        return JSONResponse({
            "ok": True,
            "filename": file.filename,
            "upload_bytes": len(raw),
            "decompressed_bytes": len(sql),
            "duration_s": round(time.monotonic() - t0, 1),
            "counts": counts,
            "rebuilt": rebuilt,
        })
    finally:
        db.close()


@router.get("/admin/ideas/seed")
async def admin_ideas_seed_info():
    """Small self-describing GET so the endpoint is discoverable."""
    return {
        "hint": "POST a .sql.gz produced by `python3 scripts/dump_idea_tables.py` "
                "with multipart field name 'file'.",
        "tables": SEED_TABLES,
        "max_bytes": SEED_MAX_BYTES,
        "token_required": _seed_token_required() is not None,
    }


# ===========================================================================
# /ideas/api/scatter.json — UMAP 2D map of idea space
# ===========================================================================

@router.get("/ideas/api/scatter.json")
async def ideas_api_scatter():
    """Return a compact payload for the UMAP scatter on /ideas.

    Shape:
      {
        "rows": [[x, y, cluster_id, era_rank, program_rank, id, company, short_desc], ...],
        "era_map":     {0: "hot", 1: "steady", ...},       # era_class <-> numeric idx
        "program_map": {0: "Y Combinator", ...},           # program <-> numeric idx
        "cluster_map": {cluster_id: "label"}               # resolved on hover
      }

    x/y rounded to 3 decimals; short_desc truncated to 140 chars.
    Response is ~2-3 MB raw, ~400-700 KB gzipped.
    """
    db = get_db()
    try:
        cur = db.conn.cursor()
        # Normalize era_class + program into small integers so each row is
        # 8 fields of simple primitives — keeps the JSON compact.
        eras = [r[0] for r in cur.execute(
            "SELECT DISTINCT era_class FROM idea_clusters "
            "WHERE era_class IS NOT NULL ORDER BY era_class").fetchall()]
        programs = [r[0] for r in cur.execute(
            "SELECT DISTINCT program FROM company_ideas "
            "WHERE program IS NOT NULL ORDER BY program").fetchall()]
        era_idx = {e: i for i, e in enumerate(eras)}
        prog_idx = {p: i for i, p in enumerate(programs)}

        rows_raw = cur.execute("""
            SELECT ci.id, ci.company, ci.one_liner, ci.program, ci.umap_x, ci.umap_y,
                   ci.cluster_id, c.era_class
              FROM company_ideas ci
         LEFT JOIN idea_clusters c ON c.cluster_id = ci.cluster_id
             WHERE ci.umap_x IS NOT NULL
               AND ci.umap_y IS NOT NULL
               AND ci.company IS NOT NULL AND ci.company != ''
        """).fetchall()

        rows = []
        for r in rows_raw:
            desc = (r["one_liner"] or "")[:140]
            rows.append([
                round(float(r["umap_x"]), 3),
                round(float(r["umap_y"]), 3),
                int(r["cluster_id"]) if r["cluster_id"] is not None else -1,
                era_idx.get(r["era_class"], -1),
                prog_idx.get(r["program"], -1),
                int(r["id"]),
                r["company"][:60] if r["company"] else "",
                desc,
            ])

        # Cluster labels for hover — only include the ones present in rows.
        cids = sorted({row[2] for row in rows if row[2] != -1})
        cluster_map: Dict[int, str] = {}
        if cids:
            qmarks = ",".join("?" * len(cids))
            for r in cur.execute(
                f"SELECT cluster_id, label FROM idea_clusters WHERE cluster_id IN ({qmarks})",
                cids,
            ):
                cluster_map[int(r["cluster_id"])] = r["label"] or f"cluster {r['cluster_id']}"

        return JSONResponse({
            "rows":        rows,
            "era_map":     {i: e for i, e in enumerate(eras)},
            "program_map": {i: p for i, p in enumerate(programs)},
            "cluster_map": {str(k): v for k, v in cluster_map.items()},
        })
    finally:
        db.close()


# ===========================================================================
# /ideas/clusters/{cluster_id} — drill into one cluster
# ===========================================================================

def _cluster_children(db, cluster_id: int) -> List[Dict]:
    rows = db.conn.execute("""
        SELECT cluster_id, label, size, era_class,
               median_year, count_2023_plus, count_pre_2015
          FROM idea_clusters
         WHERE parent_cluster_id = ?
         ORDER BY size DESC
    """, (cluster_id,)).fetchall()
    return [dict(r) for r in rows]


def _cluster_siblings(db, parent_id: int, current_id: int) -> List[Dict]:
    rows = db.conn.execute("""
        SELECT cluster_id, label, size, era_class, median_year, count_2023_plus
          FROM idea_clusters
         WHERE parent_cluster_id = ? AND cluster_id != ?
         ORDER BY size DESC
         LIMIT 10
    """, (parent_id, current_id)).fetchall()
    return [dict(r) for r in rows]


def _cluster_mechanism_sector_breakdown(db, cluster_id: int) -> Dict[str, List]:
    """Top mechanisms + sectors within this cluster's members."""
    mechs = db.conn.execute("""
        SELECT c.mechanism AS tag, COUNT(DISTINCT c.company_id) AS n
          FROM v_mechanism_sector_cells c
          JOIN company_ideas ci ON ci.id = c.company_id
         WHERE ci.cluster_id = ?
         GROUP BY c.mechanism
         ORDER BY n DESC
         LIMIT 12
    """, (cluster_id,)).fetchall()
    sectors = db.conn.execute("""
        SELECT c.sector AS tag, COUNT(DISTINCT c.company_id) AS n
          FROM v_mechanism_sector_cells c
          JOIN company_ideas ci ON ci.id = c.company_id
         WHERE ci.cluster_id = ?
         GROUP BY c.sector
         ORDER BY n DESC
         LIMIT 12
    """, (cluster_id,)).fetchall()
    return {
        "mechanisms": [dict(r) for r in mechs],
        "sectors":    [dict(r) for r in sectors],
    }


@router.get("/ideas/api/cluster/{cluster_id}/sunburst.json")
async def ideas_api_cluster_sunburst(cluster_id: int):
    """Data for the sunburst on the cluster drill page: this cluster as
    root (if parent) or its parent as root (if sub), with children sized."""
    db = get_db()
    try:
        header = _load_cluster(db, cluster_id)
        if not header:
            return JSONResponse({"error": "not found"}, status_code=404)

        root_id = header.get("parent_cluster_id") or cluster_id
        root = _load_cluster(db, root_id) if root_id != cluster_id else header
        children = _cluster_children(db, root_id)

        # ECharts sunburst expects nested {name, value, children?}
        data = [{
            "name": root["label"] or f"cluster {root_id}",
            "cluster_id": root_id,
            "era": root.get("era_class"),
            "value": root["size"],
            "children": [
                {
                    "name": c["label"] or f"cluster {c['cluster_id']}",
                    "cluster_id": c["cluster_id"],
                    "era": c.get("era_class"),
                    "value": c["size"],
                    "current": c["cluster_id"] == cluster_id,
                }
                for c in children
            ],
        }]
        return JSONResponse({"data": data, "current_cluster_id": cluster_id})
    finally:
        db.close()


# ===========================================================================
# Agent-facing discovery endpoints
# ===========================================================================
#
# These two routes exist so another agent (or anyone landing cold) can
# orient themselves without reading source. /ideas/api/index.json is the
# API sitemap; /ideas/api/schema.json is the data dictionary.

# Hand-maintained description of every /ideas/api/* route. Keeping this
# inline rather than auto-deriving from FastAPI so each endpoint gets a
# one-sentence task-oriented description, not a routing summary.
_API_CATALOG = [
    # ----- core JSON ---------------------------------------------------------
    {"method": "GET",  "path": "/ideas/api/stats.json",
     "desc": "Pipeline totals (ideas, extracted, clusters, launch_candidates).",
     "example": "/ideas/api/stats.json",
     "returns": "object with scalar counts"},
    {"method": "GET",  "path": "/ideas/api/schema.json",
     "desc": "Data dictionary: tables, enum domains, top tag vocabularies, "
             "column descriptions. Hit this FIRST to orient.",
     "example": "/ideas/api/schema.json",
     "returns": "object with 'tables', 'enums', 'vocab', 'columns'"},
    {"method": "GET",  "path": "/ideas/api/index.json",
     "desc": "This endpoint. Lists every /ideas/api/* route with examples.",
     "example": "/ideas/api/index.json",
     "returns": "object with 'endpoints' array"},

    # ----- full-text + drill --------------------------------------------------
    {"method": "GET",  "path": "/ideas/api/search.json",
     "desc": "Full-text search across company, problem_statement, tags. "
             "Hit this to find ideas about a specific topic.",
     "params": {"q": "required search string", "limit": "default 50"},
     "example": "/ideas/api/search.json?q=invoice+reconciliation",
     "returns": "array of hit objects (company, program, year, preview)"},
    {"method": "GET",  "path": "/ideas/api/idea/{idea_id}.json",
     "desc": "Full joined row for one company (company_ideas + extraction + "
             "enrichment + cluster).",
     "example": "/ideas/api/idea/1820.json",
     "returns": "single object"},
    {"method": "GET",  "path": "/ideas/api/idea-card/{idea_id}",
     "desc": "Compact JSON card for UI preview (subset of idea/{id}).",
     "example": "/ideas/api/idea-card/1820",
     "returns": "single object"},
    {"method": "GET",  "path": "/ideas/api/similar/{idea_id}.json",
     "desc": "Top-k semantic neighbours for one company across the whole "
             "corpus (pre-computed, cosine similarity over MiniLM). Use "
             "this to pivot from any company to closely-related ideas.",
     "params": {"k": "1-25, default 10"},
     "example": "/ideas/api/similar/1820.json?k=10",
     "returns": "object with 'source' + 'neighbors' array"},

    # ----- clustering --------------------------------------------------------
    {"method": "GET",  "path": "/ideas/api/cluster/{cluster_id}.json",
     "desc": "Cluster metadata + sample members.",
     "example": "/ideas/api/cluster/30.json",
     "returns": "object with cluster header + members array"},
    {"method": "GET",  "path": "/ideas/api/cluster/{cluster_id}/sunburst.json",
     "desc": "Hierarchical data for an ECharts sunburst (parent + children).",
     "example": "/ideas/api/cluster/30/sunburst.json",
     "returns": "object with 'data' (nested tree)"},

    # ----- mechanism × sector matrix + gaps -----------------------------------
    {"method": "GET",  "path": "/ideas/api/heatmap.json",
     "desc": "Mechanism × sector heatmap cells for top N of each axis.",
     "params": {"n_mechanisms": "default 25", "n_sectors": "default 20"},
     "example": "/ideas/api/heatmap.json?n_mechanisms=30&n_sectors=25",
     "returns": "object with 'mechanisms', 'sectors', 'cells' (x, y, n)"},
    {"method": "GET",  "path": "/ideas/api/gaps.json",
     "desc": "Top scored recombination gaps from idea_gap_ranking.",
     "example": "/ideas/api/gaps.json",
     "returns": "array of {mechanism, sector, score, ...}"},
    {"method": "GET",  "path": "/ideas/api/gap/{mechanism}/{sector}.json",
     "desc": "Proof rows for a specific (mechanism, sector) gap — companies "
             "adjacent to the gap in related sectors.",
     "example": "/ideas/api/gap/llm-copilot/legal.json",
     "returns": "object with proof rows + neighbours"},

    # ----- maps + scatter ----------------------------------------------------
    {"method": "GET",  "path": "/ideas/api/scatter.json",
     "desc": "Full UMAP 2D scatter — every row with (x, y, cluster_id, era_idx, "
             "program_idx, id, company, desc). ~20K points, ~2MB.",
     "example": "/ideas/api/scatter.json",
     "returns": "object with 'rows', 'era_map', 'program_map', 'cluster_map'"},
    {"method": "GET",  "path": "/ideas/api/map.json",
     "desc": "Smaller sampled map (default 100 points, optional program filter).",
     "example": "/ideas/api/map.json?program=Y+Combinator",
     "returns": "array of points"},

    # ----- feedback (writeable) ----------------------------------------------
    {"method": "POST", "path": "/ideas/api/gap-feedback",
     "desc": "Record an up/down vote on a (mechanism, sector) gap.",
     "body": "{mechanism, sector, vote: 1 | -1, note?}",
     "example": "POST /ideas/api/gap-feedback",
     "returns": "object with current up/down counts"},

    # ----- admin (auth-gated) ------------------------------------------------
    {"method": "POST", "path": "/admin/ideas/seed",
     "desc": "Restore the idea tables from a gzipped SQL dump. Requires "
             "X-Seed-Token header.",
     "example": "curl -H 'X-Seed-Token: ...' -F file=@dump.sql.gz ...",
     "returns": "object with per-table row counts"},
    {"method": "GET",  "path": "/admin/ideas/seed",
     "desc": "Self-describing hint for the POST above. Shows whether the "
             "token is required.",
     "example": "/admin/ideas/seed",
     "returns": "object with 'tables', 'token_required'"},
]


# Field-level docs. Kept terse — an agent glances at this rather than
# parsing DDL. When a new column is added to idea_extraction /
# company_ideas / idea_clusters, add a line here.
_FIELD_DOCS = {
    "company_ideas.id":                  "Integer primary key. Use in /ideas/{id} and /ideas/api/idea/{id}.json.",
    "company_ideas.program":             "Source program (YC, GC, Lux, Playground Global, Speedrun, Sequoia Arc, Show HN, Reddit *).",
    "company_ideas.company":             "Company name; may be empty for Reddit/HN rows where no brand was parsed.",
    "company_ideas.one_liner":           "Short hand-written pitch from the source scrape.",
    "company_ideas.long_description":    "Longer paragraph from the source scrape.",
    "company_ideas.tags_json":           "JSON array of raw source tags.",
    "company_ideas.company_website":     "As-scraped website URL (not normalized).",
    "company_ideas.normalized_website":  "Host-only, lowercase, no www/trailing slash. Use for joins.",
    "company_ideas.batch":               "Cohort label (W24, SR003, Arc Europe 2022, 2026-01 for HN monthly, ...).",
    "company_ideas.year_founded":        "Integer year. Backfilled from batch label or raw_json where possible.",
    "company_ideas.country":             "ISO-ish country name.",
    "company_ideas.cluster_id":          "Leaf cluster id. Noise rows have -1.",
    "company_ideas.umap_x":              "2D UMAP coordinate (float).",
    "company_ideas.umap_y":              "2D UMAP coordinate (float).",
    "company_ideas.opportunity_score":   "0-100 composite launch score. 90+ = top, 75-89 = strong, 60-74 = worth looking, <40 = skip. NULL means no extraction.",
    "company_ideas.opportunity_breakdown": "JSON {niche, era, moat, shape, recency} — per-signal contributions to opportunity_score.",
    "idea_extraction.problem_statement": "One-sentence concrete pain the startup solves.",
    "idea_extraction.customer_verticals":"JSON array of 1-3 specific sub-segments.",
    "idea_extraction.mechanism_tags":    "JSON array of 2-5 primitives (what it DOES; seed vocab + invented).",
    "idea_extraction.sector_tags":       "JSON array of 1-3 sectors (WHO / WHERE it applies).",
    "idea_extraction.customer_size":     "Enum, see schema.enums.customer_size.",
    "idea_extraction.business_model":    "Enum, see schema.enums.business_model.",
    "idea_extraction.solo_buildable":    "0/1: could 1-3 people ship this in 12 months?",
    "idea_extraction.ai_first_advantage":"0/1: would this be meaningfully better built AI-first than pre-2022 approach?",
    "idea_extraction.moat_type":         "Enum. 'regulatory' and 'capital' disqualify microbusiness shape.",
    "idea_extraction.niche_specificity": "Enum. Narrower is generally more launch-feasible.",
    "idea_clusters.cluster_id":          "Integer PK. -1 is the noise bucket.",
    "idea_clusters.parent_cluster_id":   "NULL for top-level clusters; otherwise points at a parent row.",
    "idea_clusters.size":                "Number of company_ideas rows in this cluster.",
    "idea_clusters.label":               "Auto-generated TF-IDF label. Prefer llm_label if populated.",
    "idea_clusters.llm_label":           "Claude-generated human-readable short label (may be NULL if relabel job not yet run).",
    "idea_clusters.llm_description":     "Claude-generated one-sentence description (may be NULL).",
    "idea_clusters.era_class":           "Enum: hot | steady | rebuild_candidate | legacy | unknown.",
    "idea_clusters.median_year":         "Median founding year of members.",
    "website_enrichment.normalized_website": "PK. Joins to company_ideas.normalized_website.",
    "website_enrichment.meta_description":   "Homepage <meta name='description'>.",
    "website_enrichment.hero_h1":            "Homepage <h1>.",
    "website_enrichment.hero_text":          "First ~2000 chars of cleaned body text.",
    "idea_nearest.company_idea_id":          "Source company; PK with rank.",
    "idea_nearest.rank":                     "1-10. 1 = closest neighbour.",
    "idea_nearest.neighbor_id":              "Neighbour company_idea. Joins back to company_ideas.id.",
    "idea_nearest.similarity":               "Cosine similarity 0..1 (higher = more similar).",
}


_ENUM_DOMAINS = {
    "era_class": {
        "values": ["hot", "steady", "rebuild_candidate", "legacy", "unknown"],
        "notes": {
            "hot": "≥50% of members founded 2023+",
            "steady": "mixed-era, no dominant cohort",
            "rebuild_candidate": "median founding year ≤ 2018 with few recent entries — AI-first rewrite plausible",
            "legacy": "dead/dominated space, very few 2023+ entries",
            "unknown": "<40% year coverage; too thin to classify",
        },
    },
    "customer_size": {
        "values": ["consumer", "prosumer", "smb", "mid_market", "enterprise", "developer"],
        "notes": {"developer": "Primary buyer is a dev (APIs, infra, tooling)"},
    },
    "business_model": {
        "values": ["saas", "service", "marketplace", "api", "hardware",
                   "agency", "course", "community", "consumer_app", "hybrid"],
    },
    "moat_type": {
        "values": ["none", "brand", "integration", "domain_expertise",
                   "data", "network", "regulatory", "capital"],
        "notes": {
            "none": "No structural moat (often correct for microbusinesses — space is contestable)",
            "regulatory": "Licensing / compliance barrier — disqualifies microbusiness shape",
            "capital": "Large upfront capital required — also disqualifies",
        },
    },
    "niche_specificity": {
        "values": ["narrow", "medium", "broad"],
        "notes": {
            "narrow": "Specific sub-segment within an industry (e.g. 'solo landlords with 1-4 units')",
            "medium": "One industry OR one role",
            "broad": "Horizontal across many industries",
        },
    },
    "solo_buildable": {"values": [0, 1], "notes": {"1": "A 1-3 person team could ship + operate in 12 months"}},
    "ai_first_advantage": {"values": [0, 1], "notes": {"1": "Meaningfully better with modern AI than pre-2022 approach"}},
}


@router.get("/ideas/api/index.json")
async def ideas_api_index():
    """Machine-readable catalog of every /ideas/api/* endpoint."""
    return {
        "name": "StartupRadar — Ideas API",
        "description": "Idea discovery pipeline over accelerator cohorts, "
                       "VC portfolios and microbusiness communities.",
        "base": "https://fabulous-fascination-production-4638.up.railway.app",
        "openapi": "/openapi.json",
        "data_dictionary": "/ideas/api/schema.json",
        "ui": {
            "overview":         "/ideas",
            "launch_shortlist": "/ideas/shortlist",
            "search":           "/ideas/search",
            "2d_map":           "/ideas/map",
            "cluster_drill":    "/ideas/clusters/{cluster_id}",
            "company_detail":   "/ideas/{idea_id}",
        },
        "endpoints": _API_CATALOG,
    }


@router.get("/ideas/api/schema.json")
async def ideas_api_schema():
    """Data dictionary — tables, enums, tag vocabulary, field docs.

    Hit this endpoint first if you're an agent arriving cold. One fetch
    tells you what's in the DB and what the column values mean."""
    db = get_db()
    try:
        _ensure_views(db)
        cur = db.conn.cursor()

        # Table row counts (only the tables an agent cares about).
        tables_of_interest = [
            ("company_ideas", "One row per scraped idea. Source of truth."),
            ("idea_extraction", "LLM-extracted structured fields per idea (Haiku 4.5 tool-use). May be absent for rows not yet extracted."),
            ("idea_clusters", "HDBSCAN clusters + sub-clusters. cluster_id=-1 is noise."),
            ("website_enrichment", "Homepage meta / hero text per unique domain."),
            ("idea_gap_ranking", "Scored (mechanism × sector) recombination gaps."),
            ("idea_nearest", "Pre-computed top-10 semantic neighbours per row (260K). Feeds /ideas/api/similar/{id}.json."),
            ("tag_alias", "variant → canonical tag mapping from canonicalization."),
            ("companies", "Handelsregister entities (German company registry). DACH match to company_ideas not yet populated."),
        ]
        tables = []
        for name, desc in tables_of_interest:
            try:
                n = int(cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] or 0)
            except Exception:
                n = 0
            tables.append({"name": name, "rows": n, "description": desc})

        # Top N mechanism + sector vocabularies (what tags actually exist).
        top_mechanisms = [
            {"tag": r[0], "count": int(r[1])}
            for r in _safe_query(
                db, "SELECT mechanism, n FROM v_mechanism_totals "
                    "ORDER BY n DESC LIMIT 100")
        ]
        top_sectors = [
            {"tag": r[0], "count": int(r[1])}
            for r in _safe_query(
                db, "SELECT sector, n FROM v_sector_totals "
                    "ORDER BY n DESC LIMIT 100")
        ]

        # Opportunity score bucket counts.
        score_buckets: Dict[str, int] = {}
        for r in _safe_query(db, """
            SELECT CASE
                WHEN opportunity_score >= 90 THEN '90-100 (top)'
                WHEN opportunity_score >= 75 THEN '75-89 (strong)'
                WHEN opportunity_score >= 60 THEN '60-74 (worth looking)'
                WHEN opportunity_score >= 40 THEN '40-59 (meh)'
                WHEN opportunity_score IS NULL THEN 'NULL (no extraction)'
                ELSE '0-39 (skip)'
            END AS bucket, COUNT(*) AS n
            FROM company_ideas GROUP BY bucket
        """):
            score_buckets[r[0]] = int(r[1])

        # Program coverage — which sources have LLM extractions.
        programs = []
        for r in _safe_query(db, """
            SELECT ci.program, COUNT(*) AS total,
                   SUM(CASE WHEN ie.company_idea_id IS NOT NULL
                            AND ie.error IS NULL THEN 1 ELSE 0 END) AS extracted
              FROM company_ideas ci
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
             GROUP BY ci.program ORDER BY total DESC
        """):
            programs.append({
                "program": r[0], "rows": int(r[1]),
                "extracted": int(r[2] or 0),
            })

        return JSONResponse({
            "tables":      tables,
            "programs":    programs,
            "enums":       _ENUM_DOMAINS,
            "vocab": {
                "mechanism_tags": top_mechanisms,
                "sector_tags":    top_sectors,
            },
            "columns":     _FIELD_DOCS,
            "opportunity_score_buckets": score_buckets,
            "canonical_queries": {
                "top_launch_candidates":
                    "/ideas/api/search.json (or /ideas/launch-candidates?sort=score)",
                "drill_cluster":
                    "/ideas/api/cluster/{cluster_id}.json",
                "specific_gap":
                    "/ideas/api/gap/{mechanism}/{sector}.json",
                "ideas_about_a_topic":
                    "/ideas/api/search.json?q=<your+query>",
                "similar_to_this_idea":
                    "/ideas/api/similar/{id}.json  (pre-computed cosine neighbours)",
                "2d_map_data":
                    "/ideas/api/scatter.json",
                "heatmap":
                    "/ideas/api/heatmap.json",
            },
            "how_to_auth": {
                "read":  "All GET endpoints are public.",
                "write": "POST /admin/ideas/seed requires X-Seed-Token header matching the IDEAS_SEED_TOKEN env var on the server.",
            },
        })
    finally:
        db.close()


# ===========================================================================
# Similar-to-this (pre-computed nearest neighbours)
# ===========================================================================

@router.get("/ideas/api/similar/{idea_id}.json")
async def ideas_api_similar(idea_id: int, k: int = 10):
    """Return the top-k semantic neighbours for one company_ideas row.

    Uses the pre-computed `idea_nearest` table — populated locally by
    `scheduler.jobs.idea_similar_job` and shipped to Railway via the
    seed dump. Instant lookup on the server (no ML deps).

    Returns:
        {"source": {...}, "neighbors": [{rank, similarity, id, company,
         program, year_founded, cluster_id, cluster_label, one_liner,
         mechanism_tags, sector_tags, opportunity_score}, ...]}
    """
    if k < 1 or k > 25:
        raise HTTPException(400, "k must be in [1, 25]")
    db = get_db()
    try:
        src = db.conn.execute(
            "SELECT id, company, program, year_founded, one_liner, cluster_id "
            "FROM company_ideas WHERE id = ?",
            (idea_id,),
        ).fetchone()
        if src is None:
            raise HTTPException(404, f"idea {idea_id} not found")

        rows = db.conn.execute(
            """
            SELECT n.rank, n.similarity,
                   ci.id, ci.company, ci.program, ci.year_founded,
                   ci.one_liner, ci.cluster_id, ci.opportunity_score,
                   c.label AS cluster_label, c.llm_label,
                   ie.mechanism_tags, ie.sector_tags, ie.problem_statement
              FROM idea_nearest n
              JOIN company_ideas ci ON ci.id = n.neighbor_id
         LEFT JOIN idea_clusters  c ON c.cluster_id = ci.cluster_id
         LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
                                      AND ie.error IS NULL
             WHERE n.company_idea_id = ?
             ORDER BY n.rank
             LIMIT ?
            """,
            (idea_id, k),
        ).fetchall()

        return JSONResponse({
            "source": {
                "id": src["id"], "company": src["company"],
                "program": src["program"], "year_founded": src["year_founded"],
                "one_liner": src["one_liner"], "cluster_id": src["cluster_id"],
            },
            "neighbors": [
                {
                    "rank": r["rank"],
                    "similarity": round(float(r["similarity"]), 3),
                    "id": r["id"],
                    "company": r["company"],
                    "program": r["program"],
                    "year_founded": r["year_founded"],
                    "cluster_id": r["cluster_id"],
                    "cluster_label": r["llm_label"] or r["cluster_label"],
                    "one_liner": r["one_liner"],
                    "problem_statement": r["problem_statement"],
                    "mechanism_tags": r["mechanism_tags"],
                    "sector_tags": r["sector_tags"],
                    "opportunity_score": r["opportunity_score"],
                }
                for r in rows
            ],
        })
    finally:
        db.close()
