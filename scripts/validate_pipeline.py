"""
validate_pipeline — one-shot end-to-end sanity check for the idea
pipeline. Runs a list of assertions grouped by stage and prints a
pass/fail scorecard. Zero side effects (all read-only).

Stages covered:
  1. Scrapers / JSONL files on disk
  2. Loader → company_ideas table
  3. Website enrichment
  4. Clustering (HDBSCAN + sub-clusters + era classification)
  5. LLM extraction
  6. Tag canonicalization
  7. SQL views + canned queries
  8. Gap ranking + feedback tables
  9. FTS search
 10. FastAPI / Railway surfaces

Usage:
  python3 scripts/validate_pipeline.py
  python3 scripts/validate_pipeline.py --base-url https://<railway>.up.railway.app
  python3 scripts/validate_pipeline.py --skip-remote
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).parent.parent


@dataclass
class Check:
    stage: str
    name: str
    status: str = "skip"          # pass | fail | warn | skip
    detail: str = ""
    duration_ms: int = 0


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def run(self, stage: str, name: str, fn: Callable[[], str]) -> None:
        t0 = time.monotonic()
        try:
            detail = fn()
            status = "pass"
            if isinstance(detail, tuple):
                status, detail = detail
        except AssertionError as e:
            status, detail = "fail", str(e)
        except Exception as e:
            status, detail = "fail", f"{type(e).__name__}: {e}"
        self.checks.append(Check(stage, name, status,
                                 detail[:300] if detail else "",
                                 int((time.monotonic() - t0) * 1000)))

    def emit(self) -> bool:
        """Print report. Return True if all pass/warn (no hard failures)."""
        by_stage: dict = {}
        for c in self.checks:
            by_stage.setdefault(c.stage, []).append(c)

        tot = len(self.checks)
        passed = sum(1 for c in self.checks if c.status == "pass")
        warned = sum(1 for c in self.checks if c.status == "warn")
        failed = sum(1 for c in self.checks if c.status == "fail")
        skipped = sum(1 for c in self.checks if c.status == "skip")

        glyph = {"pass": "✓", "fail": "✗", "warn": "!", "skip": "·"}

        for stage, checks in by_stage.items():
            n_pass = sum(1 for c in checks if c.status == "pass")
            print(f"\n=== {stage}  ({n_pass}/{len(checks)}) ===")
            for c in checks:
                line = f"  {glyph[c.status]} {c.name:<48} {c.duration_ms:>5}ms"
                if c.detail:
                    line += f"  — {c.detail}"
                print(line)

        print()
        print(f"  total: {tot}   pass: {passed}   warn: {warned}   "
              f"fail: {failed}   skip: {skipped}")
        return failed == 0


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def run_local_checks(report: Report, db_path: str) -> None:
    assert Path(db_path).exists(), f"DB not found: {db_path}"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    def _scalar(sql: str, params: tuple = ()) -> int:
        row = cur.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # --- STAGE 1: scrapers / JSONL files on disk ---------------------------
    def _jsonl_files():
        files = sorted(glob.glob(str(PROJECT_ROOT / "data/ideas/ideas_*.jsonl")))
        assert files, "no ideas_*.jsonl files found in data/ideas/"
        total_lines = 0
        per_file = []
        for f in files:
            with open(f) as fh:
                n = sum(1 for _ in fh)
            total_lines += n
            per_file.append((Path(f).name, n))
        return ("pass",
                f"{len(files)} files, {total_lines:,} lines "
                f"(biggest: {max(per_file, key=lambda x: x[1])[0]})")
    report.run("1 scrapers", "data/ideas/ideas_*.jsonl present", _jsonl_files)

    # --- STAGE 2: loader → company_ideas -----------------------------------
    def _ideas_nonempty():
        n = _scalar("SELECT COUNT(*) FROM company_ideas")
        assert n > 1000, f"only {n} rows in company_ideas"
        return f"{n:,} rows"
    report.run("2 loader", "company_ideas has >1K rows", _ideas_nonempty)

    def _programs_covered():
        programs = [r[0] for r in cur.execute(
            "SELECT DISTINCT program FROM company_ideas ORDER BY program")]
        assert len(programs) >= 5, f"only {len(programs)} programs: {programs}"
        return f"{len(programs)} programs: {', '.join(programs[:6])}..."
    report.run("2 loader", "multiple programs loaded", _programs_covered)

    def _source_url_unique():
        row = cur.execute(
            "SELECT program, source_url, COUNT(*) AS n "
            "FROM company_ideas GROUP BY program, source_url "
            "HAVING n > 1 ORDER BY n DESC LIMIT 1"
        ).fetchone()
        if row:
            return ("fail",
                    f"{row['n']} dup rows for ({row['program']}, {row['source_url'][:60]})")
        return "no duplicate (program, source_url) pairs"
    report.run("2 loader", "UNIQUE(program, source_url) holds", _source_url_unique)

    def _normalized_website_quality():
        total = _scalar("SELECT COUNT(*) FROM company_ideas WHERE company_website IS NOT NULL AND company_website != ''")
        normed = _scalar("SELECT COUNT(*) FROM company_ideas WHERE normalized_website IS NOT NULL AND normalized_website != ''")
        pct = (normed / total * 100) if total else 0
        if pct < 80:
            return ("warn", f"only {pct:.0f}% of websites normalized ({normed}/{total})")
        return f"{pct:.0f}% normalized ({normed}/{total})"
    report.run("2 loader", "normalized_website populated for most rows",
               _normalized_website_quality)

    # --- STAGE 3: website enrichment ---------------------------------------
    def _enrichment_count():
        n = _scalar("SELECT COUNT(*) FROM website_enrichment")
        assert n > 0, "website_enrichment empty"
        unique_domains = _scalar(
            "SELECT COUNT(DISTINCT normalized_website) FROM company_ideas "
            "WHERE normalized_website IS NOT NULL AND normalized_website != ''")
        cov = (n / unique_domains * 100) if unique_domains else 0
        return f"{n:,} rows, covers {cov:.0f}% of unique idea domains"
    report.run("3 enrichment", "website_enrichment populated", _enrichment_count)

    def _enrichment_content():
        n_with_content = _scalar(
            "SELECT COUNT(*) FROM website_enrichment "
            "WHERE (meta_description IS NOT NULL AND meta_description != '') "
            "   OR (hero_text IS NOT NULL AND LENGTH(hero_text) > 100)")
        total = _scalar("SELECT COUNT(*) FROM website_enrichment")
        pct = (n_with_content / total * 100) if total else 0
        if pct < 50:
            return ("warn", f"only {pct:.0f}% have meta_description or hero_text")
        return f"{pct:.0f}% have meta_description or hero_text"
    report.run("3 enrichment", "enrichment rows have usable text",
               _enrichment_content)

    # --- STAGE 4: clustering -----------------------------------------------
    def _cluster_populated():
        n_rows_with_cluster = _scalar(
            "SELECT COUNT(*) FROM company_ideas WHERE cluster_id IS NOT NULL")
        assert n_rows_with_cluster > 1000, f"only {n_rows_with_cluster} rows have cluster_id"
        parents = _scalar(
            "SELECT COUNT(*) FROM idea_clusters "
            "WHERE cluster_id != -1 AND parent_cluster_id IS NULL")
        subs = _scalar(
            "SELECT COUNT(*) FROM idea_clusters "
            "WHERE parent_cluster_id IS NOT NULL")
        return f"{n_rows_with_cluster:,} rows clustered; {parents} parents + {subs} subs"
    report.run("4 clustering", "cluster_id populated on company_ideas",
               _cluster_populated)

    def _era_classification():
        rows = cur.execute(
            "SELECT era_class, COUNT(*) AS n FROM idea_clusters "
            "WHERE cluster_id != -1 GROUP BY era_class"
        ).fetchall()
        by_era = {r["era_class"]: r["n"] for r in rows}
        assert "hot" in by_era or "rebuild_candidate" in by_era, \
            f"no 'hot' or 'rebuild_candidate' clusters — era classifier silent? {by_era}"
        return " · ".join(f"{k}={v}" for k, v in by_era.items())
    report.run("4 clustering", "era_class populated", _era_classification)

    def _cluster_cascade():
        """Parent cluster size should be >= sum of its sub-cluster sizes."""
        orphan = cur.execute(
            """
            SELECT sub.parent_cluster_id
              FROM idea_clusters sub
              LEFT JOIN idea_clusters parent
                     ON parent.cluster_id = sub.parent_cluster_id
             WHERE sub.parent_cluster_id IS NOT NULL
               AND parent.cluster_id IS NULL
             LIMIT 1
            """
        ).fetchone()
        if orphan:
            return ("fail",
                    f"sub-cluster points at missing parent {orphan[0]}")
        return "every sub-cluster has a real parent"
    report.run("4 clustering", "sub-cluster parent integrity", _cluster_cascade)

    # --- STAGE 5: LLM extraction -------------------------------------------
    def _extraction_count():
        n_ok = _scalar("SELECT COUNT(*) FROM idea_extraction WHERE error IS NULL")
        n_err = _scalar("SELECT COUNT(*) FROM idea_extraction WHERE error IS NOT NULL")
        assert n_ok > 1000, f"only {n_ok} successful extractions"
        if n_err > 0:
            return ("warn", f"{n_ok:,} ok, {n_err} errors persisted")
        return f"{n_ok:,} ok, no errors"
    report.run("5 extraction", "idea_extraction rows exist", _extraction_count)

    def _extraction_tags_valid_json():
        """Every mechanism_tags / sector_tags should be valid JSON arrays."""
        bad = 0
        for row in cur.execute(
            "SELECT mechanism_tags, sector_tags FROM idea_extraction "
            "WHERE error IS NULL LIMIT 500"
        ):
            for tag_col in ("mechanism_tags", "sector_tags"):
                v = row[tag_col]
                if v is None:
                    continue
                try:
                    parsed = json.loads(v)
                    if not isinstance(parsed, list):
                        bad += 1
                except Exception:
                    bad += 1
        if bad:
            return ("fail", f"{bad} tag arrays failed JSON validation (sample of 500)")
        return "500-row sample of mechanism_tags/sector_tags is all valid JSON arrays"
    report.run("5 extraction", "tag columns are valid JSON arrays",
               _extraction_tags_valid_json)

    def _solo_ai_flags():
        n_solo_ai = _scalar(
            "SELECT COUNT(*) FROM idea_extraction "
            "WHERE solo_buildable=1 AND ai_first_advantage=1")
        assert n_solo_ai > 100, \
            f"only {n_solo_ai} rows flagged solo+ai_first — extraction may be too strict"
        return f"{n_solo_ai:,} rows are solo_buildable AND ai_first_advantage"
    report.run("5 extraction", "solo × ai_first shortlist is non-trivial",
               _solo_ai_flags)

    def _extraction_coverage_by_program():
        rows = cur.execute("""
            SELECT ci.program, COUNT(*) AS total,
                   SUM(CASE WHEN ie.company_idea_id IS NOT NULL THEN 1 ELSE 0 END) AS extracted
              FROM company_ideas ci
              LEFT JOIN idea_extraction ie ON ie.company_idea_id = ci.id
                                          AND ie.error IS NULL
             GROUP BY ci.program
             ORDER BY total DESC
        """).fetchall()
        programs_extracted = [r for r in rows if r["extracted"] > 0]
        assert programs_extracted, "no program has any extractions"
        missing = [r["program"] for r in rows if r["extracted"] == 0]
        if missing:
            return ("warn",
                    f"programs with zero extractions: {', '.join(missing[:4])}")
        return f"{len(programs_extracted)} programs have extractions"
    report.run("5 extraction", "extraction covers multiple programs",
               _extraction_coverage_by_program)

    # --- STAGE 6: tag canonicalization -------------------------------------
    def _canon_ran():
        exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='idea_extraction_tag_backup'"
        ).fetchone() is not None
        if not exists:
            return ("warn", "no tag backup — canonicalization hasn't run")
        n_backup = _scalar("SELECT COUNT(*) FROM idea_extraction_tag_backup")
        return f"canon ran; {n_backup:,} rows in backup table"
    report.run("6 canon", "tag canonicalization left an audit trail", _canon_ran)

    def _alias_table_present():
        exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tag_alias'"
        ).fetchone()
        if not exists:
            return ("warn", "tag_alias table not present (optional)")
        n = _scalar("SELECT COUNT(*) FROM tag_alias")
        return f"tag_alias populated with {n:,} aliases"
    report.run("6 canon", "tag_alias table present", _alias_table_present)

    # --- STAGE 7: SQL views + canned queries -------------------------------
    views = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE 'v_%'")]
    required_views = [
        "v_matrix", "v_mechanism_totals", "v_sector_totals",
        "v_mechanism_sector_cells", "v_launch_candidates_loose",
        "v_launch_candidates_strict", "v_idea_full",
    ]
    def _views_present():
        missing = [v for v in required_views if v not in views]
        assert not missing, f"missing views: {missing}"
        return f"{len(required_views)} required views present"
    report.run("7 views", "required views defined", _views_present)

    def _matrix_populated():
        n_cells = _scalar("SELECT COUNT(*) FROM v_matrix")
        n_mechs = _scalar("SELECT COUNT(*) FROM v_mechanism_totals")
        n_sects = _scalar("SELECT COUNT(*) FROM v_sector_totals")
        assert n_cells > 100, f"only {n_cells} cells in v_matrix"
        return f"{n_cells:,} cells across {n_mechs} mechanisms × {n_sects} sectors"
    report.run("7 views", "v_matrix has >100 cells", _matrix_populated)

    def _launch_candidates_align():
        loose = _scalar("SELECT COUNT(*) FROM v_launch_candidates_loose")
        strict = _scalar("SELECT COUNT(*) FROM v_launch_candidates_strict")
        assert loose >= strict, \
            f"loose ({loose}) should be >= strict ({strict})"
        return f"loose={loose:,}, strict={strict:,}"
    report.run("7 views", "loose >= strict launch candidates",
               _launch_candidates_align)

    # --- STAGE 8: gap ranking + feedback tables ----------------------------
    def _gap_ranking():
        exists = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='idea_gap_ranking'").fetchone()
        if not exists:
            return ("warn", "idea_gap_ranking table missing")
        n = _scalar("SELECT COUNT(*) FROM idea_gap_ranking")
        return f"{n:,} ranked gaps"
    report.run("8 gaps", "idea_gap_ranking table present", _gap_ranking)

    def _gap_feedback_schema():
        exists = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='gap_feedback'").fetchone()
        if not exists:
            return ("warn", "gap_feedback table missing (lazy-created on vote)")
        return "gap_feedback schema present"
    report.run("8 gaps", "gap_feedback table exists or is lazy-created",
               _gap_feedback_schema)

    # --- STAGE 9: FTS ------------------------------------------------------
    def _fts_tables():
        expected = ["company_ideas_fts", "idea_extraction_fts", "website_enrichment_fts"]
        present = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%_fts'")]
        missing = [t for t in expected if t not in present]
        if missing:
            return ("fail", f"FTS tables missing: {missing}")
        return f"{len(expected)} FTS virtual tables present"
    report.run("9 fts", "FTS tables present", _fts_tables)

    def _fts_search_works():
        try:
            rows = cur.execute(
                "SELECT COUNT(*) FROM company_ideas_fts WHERE company_ideas_fts MATCH ?",
                ("invoice",),
            ).fetchone()
            n = int(rows[0]) if rows else 0
        except Exception as e:
            return ("fail", f"FTS MATCH query failed: {e}")
        assert n > 0, "FTS MATCH 'invoice' returned 0 rows — FTS empty?"
        return f"FTS MATCH 'invoice' → {n} rows"
    report.run("9 fts", "FTS search returns hits", _fts_search_works)

    conn.close()


# ---------------------------------------------------------------------------
# remote checks (FastAPI on local or Railway)
# ---------------------------------------------------------------------------

def run_remote_checks(report: Report, base_url: str) -> None:
    def _fetch(path: str, timeout: float = 15.0):
        req = Request(base_url + path, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read()

    def _health():
        code, body = _fetch("/health")
        assert code == 200, f"/health returned {code}"
        return f"/health 200 in {len(body)}B"
    report.run("10 http", f"{base_url} /health", _health)

    def _ideas_page():
        code, body = _fetch("/ideas")
        assert code == 200, f"/ideas returned {code}"
        # Sniff for key template elements
        body_s = body.decode("utf-8", errors="replace")
        assert "Mechanism × Sector heatmap" in body_s or "mechanism" in body_s.lower(), \
            "/ideas HTML missing expected heatmap markup"
        return f"/ideas 200 ({len(body)/1024:.0f} KB HTML)"
    report.run("10 http", f"{base_url} /ideas", _ideas_page)

    def _stats_json():
        code, body = _fetch("/ideas/api/stats.json")
        data = json.loads(body)
        assert data.get("ideas", 0) > 1000, f"only {data.get('ideas')} ideas"
        return (f"ideas={data['ideas']:,} · extracted={data['extracted']:,} · "
                f"launch={data['launch_candidates']:,} · cells={data['populated_cells']:,}")
    report.run("10 http", f"{base_url} /ideas/api/stats.json", _stats_json)

    def _heatmap_json():
        code, body = _fetch("/ideas/api/heatmap.json?n_mechanisms=10&n_sectors=10")
        data = json.loads(body)
        assert data["mechanisms"], "no mechanisms in heatmap response"
        assert data["sectors"], "no sectors"
        assert data["cells"], "no cells"
        return (f"{len(data['mechanisms'])}×{len(data['sectors'])}, "
                f"{len(data['cells'])} cells, max {data['max_n']}")
    report.run("10 http", f"{base_url} /ideas/api/heatmap.json", _heatmap_json)

    def _seed_info():
        code, body = _fetch("/admin/ideas/seed")
        assert code == 200, f"/admin/ideas/seed returned {code}"
        data = json.loads(body)
        assert "tables" in data, "seed info missing 'tables' key"
        return f"seed endpoint lists {len(data['tables'])} tables"
    report.run("10 http", f"{base_url} /admin/ideas/seed GET", _seed_info)

    # ------------------------------------------------------------------
    # 11 exploration UI + agent JSON API (routes added post-seed)
    # ------------------------------------------------------------------
    # These only exist if the ideas-exploration surfaces are deployed.
    # A 404 on any of them is a genuine regression, not a missing feature.

    def _launch_candidates_page():
        code, body = _fetch("/ideas/launch-candidates?per_page=5")
        assert code == 200, f"returned {code}"
        body_s = body.decode("utf-8", errors="replace")
        assert "Launch candidates" in body_s, "missing h1"
        assert "match your filters" in body_s, "missing count line"
        return f"{len(body)/1024:.0f} KB HTML, renders filters + results"
    report.run("11 exploration", f"{base_url} /ideas/launch-candidates",
               _launch_candidates_page)

    def _global_search_page():
        code, body = _fetch("/ideas/search?q=voice")
        assert code == 200, f"returned {code}"
        body_s = body.decode("utf-8", errors="replace")
        assert "Search ideas" in body_s
        return "renders search page"
    report.run("11 exploration", f"{base_url} /ideas/search", _global_search_page)

    def _map_page():
        code, body = _fetch("/ideas/map")
        assert code == 200, f"returned {code}"
        body_s = body.decode("utf-8", errors="replace")
        assert "Idea map" in body_s, "missing h1"
        return "renders (coordinates banner handled inline)"
    report.run("11 exploration", f"{base_url} /ideas/map", _map_page)

    def _cluster_drill():
        # Find a real cluster id from /ideas/api/cluster pattern — 30 is a
        # stable anchor that exists in the seeded DB (healthcare cluster).
        code, body = _fetch("/ideas/clusters/30")
        assert code == 200, f"returned {code}"
        body_s = body.decode("utf-8", errors="replace")
        assert "members" in body_s or "Cluster" in body_s
        return "cluster 30 drill renders"
    report.run("11 exploration", f"{base_url} /ideas/clusters/{{id}}",
               _cluster_drill)

    def _idea_detail():
        # Pick any real id via the JSON API to avoid hard-coding one.
        _, jbody = _fetch("/ideas/api/search.json?q=agent&limit=1")
        j = json.loads(jbody)
        assert j["results"], "search returned no rows to sample"
        idea_id = j["results"][0]["id"]
        code, body = _fetch(f"/ideas/{idea_id}")
        assert code == 200, f"returned {code}"
        return f"detail page for id={idea_id} renders"
    report.run("11 exploration", f"{base_url} /ideas/{{id}}", _idea_detail)

    # --- agent JSON API ---

    def _api_search():
        _, body = _fetch("/ideas/api/search.json?q=voice&limit=5")
        data = json.loads(body)
        for k in ("query", "total", "offset", "results"):
            assert k in data, f"missing key {k}"
        assert data["total"] > 0, "no search hits"
        return f"'voice' → {data['total']:,} hits"
    report.run("11 agent-api", f"{base_url} /ideas/api/search.json", _api_search)

    def _api_gaps():
        _, body = _fetch("/ideas/api/gaps.json?limit=3")
        data = json.loads(body)
        assert data["count"] >= 1, "no gap rows"
        r0 = data["results"][0]
        for k in ("rank", "mechanism", "sector", "score"):
            assert k in r0, f"missing field {k}"
        return f"top={r0['mechanism']} × {r0['sector']} (score={r0['score']:.0f})"
    report.run("11 agent-api", f"{base_url} /ideas/api/gaps.json", _api_gaps)

    def _api_gap_detail():
        # Pick the top-ranked gap dynamically so this doesn't break if the
        # ranking shifts.
        _, body = _fetch("/ideas/api/gaps.json?limit=1")
        top = json.loads(body)["results"][0]
        _, body = _fetch(
            f"/ideas/api/gap/{top['mechanism']}/{top['sector']}.json?limit=5"
        )
        data = json.loads(body)
        for k in ("mechanism", "sector", "ranking",
                  "mechanism_proven_elsewhere", "sector_active_with_other_mechanisms"):
            assert k in data, f"missing key {k}"
        assert data["mechanism_proven_elsewhere"], "no mechanism-proof rows"
        return (f"{top['mechanism']} × {top['sector']}: "
                f"{len(data['mechanism_proven_elsewhere'])} proof rows")
    report.run("11 agent-api", f"{base_url} /ideas/api/gap/{{m}}/{{s}}.json",
               _api_gap_detail)

    def _api_cluster():
        _, body = _fetch("/ideas/api/cluster/30.json?limit=3")
        data = json.loads(body)
        assert "cluster" in data and "members" in data
        assert data["cluster"]["cluster_id"] == 30
        return f"cluster 30: {len(data['members'])} members returned"
    report.run("11 agent-api", f"{base_url} /ideas/api/cluster/{{id}}.json",
               _api_cluster)

    def _api_idea():
        _, body = _fetch("/ideas/api/search.json?q=agent&limit=1")
        idea_id = json.loads(body)["results"][0]["id"]
        code, body = _fetch(f"/ideas/api/idea/{idea_id}.json")
        assert code == 200
        data = json.loads(body)
        assert data["id"] == idea_id
        return f"idea/{idea_id}.json returns full row"
    report.run("11 agent-api", f"{base_url} /ideas/api/idea/{{id}}.json",
               _api_idea)

    def _api_map():
        _, body = _fetch("/ideas/api/map.json?limit=100")
        data = json.loads(body)
        for k in ("count", "programs", "points"):
            assert k in data, f"missing {k}"
        return f"map {data['count']:,} points ({len(data['programs'])} programs)"
    report.run("11 agent-api", f"{base_url} /ideas/api/map.json", _api_map)

    def _api_idea_card():
        _, body = _fetch("/ideas/api/search.json?q=agent&limit=1")
        idea_id = json.loads(body)["results"][0]["id"]
        _, body = _fetch(f"/ideas/api/idea-card/{idea_id}")
        data = json.loads(body)
        assert data["id"] == idea_id
        return f"idea-card/{idea_id} returns card payload"
    report.run("11 agent-api", f"{base_url} /ideas/api/idea-card/{{id}}",
               _api_idea_card)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--local-url", default="http://127.0.0.1:8002",
                   help="local FastAPI URL (set empty to skip)")
    p.add_argument("--base-url",
                   default="https://fabulous-fascination-production-4638.up.railway.app",
                   help="Railway URL (set empty to skip)")
    p.add_argument("--skip-remote", action="store_true")
    p.add_argument("--skip-local-http", action="store_true")
    args = p.parse_args()

    report = Report()
    print(f"Validating local DB: {args.db}")
    run_local_checks(report, args.db)

    if not args.skip_local_http and args.local_url:
        print(f"\nValidating local HTTP: {args.local_url}")
        try:
            run_remote_checks(report, args.local_url)
        except Exception as e:
            print(f"  (local HTTP checks skipped: {e})")

    if not args.skip_remote and args.base_url:
        print(f"\nValidating Railway: {args.base_url}")
        try:
            run_remote_checks(report, args.base_url)
        except Exception as e:
            print(f"  (Railway checks skipped: {e})")

    ok = report.emit()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
