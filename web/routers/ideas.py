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

import logging
from typing import Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
        context = {
            "stats":              _query_stats(db),
            "era_distribution":   _era_distribution(db),
            "solo_ai_matrix":     _solo_ai_matrix(db),
            "launch_candidates":  _top_launch_candidates(db, limit=12),
            "rebuild_candidates": _top_rebuild_candidates(db, limit=10),
        }
        return templates.TemplateResponse(
            name="ideas/overview.html",
            request=request,
            context=context,
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
