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
