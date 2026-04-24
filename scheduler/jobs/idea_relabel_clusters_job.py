"""
idea_relabel_clusters_job — replace the TF-IDF auto-labels on
idea_clusters with human-readable names via Claude Haiku 4.5.

The auto-generated labels (e.g. "artificial intelligence — imessage")
are ugly and sometimes misleading. For each non-noise cluster we feed
the model a compact prompt (top tags, top programs, 5 representative
companies, era + year range) and get back:

    * short_label   — 2–6 words, sector-forward ("AI voice agents for
                      customer support", "Anti-aging biotech")
    * description   — single sentence explaining what this sector is
                      about and what kind of ideas cluster here

Written to `idea_clusters.llm_label` / `.llm_description` (columns
added on first run). `idea_clusters.label` is preserved as an audit
trail.

Budget: ~1,600 clusters × ~300 cached input + 100 output tokens ≈
$2-$4 in Haiku at current pricing. Uses prompt caching.

Usage:
  python3 -m scheduler.jobs.idea_relabel_clusters_job --limit 10      # smoke
  python3 -m scheduler.jobs.idea_relabel_clusters_job                 # full
  python3 -m scheduler.jobs.idea_relabel_clusters_job --refresh       # re-run
  python3 -m scheduler.jobs.idea_relabel_clusters_job --report-only
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

from persistence.database import Database

logger = logging.getLogger(__name__)


MODEL = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 300


# --- system prompt (cached) ------------------------------------------------

SYSTEM_PROMPT = """You rename automatically-generated startup clusters with
human-readable labels. The auto-labels are brittle TF-IDF phrases like
"artificial intelligence — imessage" or "developer tools — kubernetes".
Your job is to read what's actually IN the cluster (the sample
companies, top tags, era) and produce a cleaner name.

Always call the `record_cluster_label` tool. Never return prose.

--- FIELD GUIDANCE ---

`short_label` — 2 to 6 words. Sector-forward, not adjective-first. Good:
  "AI voice agents for customer support"
  "Anti-aging longevity biotech"
  "Web3 security tooling"
  "Productized creator agencies"
Bad:
  "AI tools" (too generic)
  "Various mechanisms for several sectors" (waffle)
  "Companies doing X" (don't include the word "companies")

Base the name on the MODAL pattern in the sample, not the outliers. If
the five sample companies span three sectors, go broader; if they're
tightly scoped, be specific.

`description` — ONE sentence, ≤25 words. Explains what this cluster is
really about and what kind of ideas would cluster here. Good:
  "Small business accounting and bookkeeping automation targeted at
   solo operators and sub-10-person firms."
  "AI tutors and study tools for K-12 and test prep, mobile-first."
Bad:
  "Startups in the space of X" (never start like this)
  Anything longer than one sentence."""


TOOL_SCHEMA = {
    "name": "record_cluster_label",
    "description": "Record a human-readable label for one cluster.",
    "input_schema": {
        "type": "object",
        "required": ["short_label", "description"],
        "properties": {
            "short_label": {"type": "string", "minLength": 3, "maxLength": 80},
            "description": {"type": "string", "minLength": 10, "maxLength": 220},
        },
    },
}


# --- DB helpers ------------------------------------------------------------

_LOCK = threading.Lock()


def _ensure_columns(db: Database) -> None:
    cur = db.conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(idea_clusters)")}
    if "llm_label" not in cols:
        cur.execute("ALTER TABLE idea_clusters ADD COLUMN llm_label TEXT")
    if "llm_description" not in cols:
        cur.execute("ALTER TABLE idea_clusters ADD COLUMN llm_description TEXT")
    if "llm_labeled_at" not in cols:
        cur.execute("ALTER TABLE idea_clusters ADD COLUMN llm_labeled_at TEXT")
    db.conn.commit()


def _fetch_targets(db: Database, limit: Optional[int],
                   refresh: bool) -> List[Dict]:
    cur = db.conn.cursor()
    where = (
        "WHERE cluster_id != -1 AND size >= 3"
        + ("" if refresh else " AND (llm_label IS NULL OR llm_label = '')")
    )
    rows = cur.execute(
        f"""
        SELECT cluster_id, size, label, era_class,
               min_year, median_year, max_year,
               count_pre_2015, count_2015_2022, count_2023_plus,
               top_tags, top_programs, top_terms, representative_ids,
               parent_cluster_id
          FROM idea_clusters
          {where}
         ORDER BY size DESC
        """
    ).fetchall()
    rows = [dict(r) for r in rows]
    if limit:
        rows = rows[:limit]
    return rows


def _fetch_members(db: Database, ids: List[int]) -> List[Dict]:
    if not ids:
        return []
    qmarks = ",".join("?" * len(ids))
    cur = db.conn.cursor()
    rows = cur.execute(
        f"""
        SELECT id, company, program, year_founded, one_liner,
               (SELECT problem_statement FROM idea_extraction
                 WHERE company_idea_id = ci.id AND error IS NULL) AS problem
          FROM company_ideas ci
         WHERE id IN ({qmarks})
        """,
        ids,
    ).fetchall()
    return [dict(r) for r in rows]


def _build_user_content(cluster: Dict, members: List[Dict]) -> str:
    parts = [
        f"Cluster #{cluster['cluster_id']} (auto-label: {cluster.get('label')!r})",
        f"Size: {cluster['size']} companies.",
        f"Era class: {cluster.get('era_class') or 'unknown'}",
    ]
    if cluster.get("median_year"):
        parts.append(
            f"Year range: {cluster.get('min_year')}-{cluster.get('max_year')} "
            f"(median {cluster['median_year']}). "
            f"pre-2015: {cluster.get('count_pre_2015', 0)}, "
            f"2015-22: {cluster.get('count_2015_2022', 0)}, "
            f"2023+: {cluster.get('count_2023_plus', 0)}"
        )

    # Parse the JSON columns defensively.
    def _json(s, default):
        try: return json.loads(s) if s else default
        except Exception: return default

    tags = _json(cluster.get("top_tags"), [])
    if tags:
        top_tags = ", ".join(f"{t[0]} ({t[1]})" for t in tags[:6] if isinstance(t, (list, tuple)))
        parts.append(f"Top tags: {top_tags}")

    progs = _json(cluster.get("top_programs"), {})
    if progs and isinstance(progs, dict):
        prog_str = ", ".join(f"{p}={n}" for p, n in list(progs.items())[:5])
        parts.append(f"Top source programs: {prog_str}")

    terms = _json(cluster.get("top_terms"), [])
    if terms:
        tterms = ", ".join(t[0] for t in terms[:6] if isinstance(t, (list, tuple)))
        parts.append(f"Distinguishing TF-IDF terms: {tterms}")

    if members:
        parts.append("\nRepresentative companies:")
        for m in members[:5]:
            line = f"  * {m.get('company')}"
            if m.get("year_founded"):
                line += f" ({m['year_founded']})"
            if m.get("program"):
                line += f" [{m['program']}]"
            blurb = m.get("problem") or m.get("one_liner")
            if blurb:
                line += f" — {blurb[:220]}"
            parts.append(line)

    return "\n".join(parts)


def _call_claude(client, user_content: str,
                 max_retries: int = 4) -> Dict[str, object]:
    import anthropic

    attempts = 0
    while True:
        attempts += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=[{"type": "text", "text": SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "record_cluster_label"},
                messages=[{"role": "user", "content": user_content}],
            )
            tcs = [b for b in resp.content if b.type == "tool_use"]
            if not tcs:
                return {"error": f"no_tool_use (stop={resp.stop_reason})"}
            return {
                "short_label": tcs[0].input.get("short_label"),
                "description": tcs[0].input.get("description"),
                "_tokens": {
                    "input": getattr(resp.usage, "input_tokens", None),
                    "output": getattr(resp.usage, "output_tokens", None),
                    "cache_read": getattr(resp.usage, "cache_read_input_tokens", None),
                },
            }
        except anthropic.APIStatusError as e:
            if attempts >= max_retries:
                return {"error": f"api_{e.status_code}"}
            time.sleep(min(2 ** attempts + random.random(), 30))
        except Exception as e:  # noqa: BLE001
            if attempts >= max_retries:
                return {"error": f"exc:{type(e).__name__}"}
            time.sleep(min(2 ** attempts + random.random(), 30))


def _save(db: Database, cluster_id: int, data: Dict) -> None:
    with _LOCK:
        cur = db.conn.cursor()
        cur.execute(
            "UPDATE idea_clusters SET llm_label = ?, llm_description = ?, "
            "llm_labeled_at = ? WHERE cluster_id = ?",
            (data.get("short_label"), data.get("description"),
             datetime.now().isoformat(), cluster_id),
        )
        db.conn.commit()


def _make_client():
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY") or None
    oauth = os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or None
    if key:
        return anthropic.Anthropic(api_key=key)
    if oauth:
        return anthropic.Anthropic(auth_token=oauth)
    raise RuntimeError("Neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN set")


# --- main ------------------------------------------------------------------

def run(db_path: str, limit: Optional[int], workers: int, refresh: bool,
        verbose: bool) -> None:
    client = _make_client()
    db = Database(db_path)
    _ensure_columns(db)
    targets = _fetch_targets(db, limit, refresh)
    logger.info("relabel: %d clusters to process (workers=%d)",
                len(targets), workers)
    if not targets:
        return

    # Collect all unique member ids we'll need up front.
    all_rep_ids: List[int] = []
    per_cluster_ids: Dict[int, List[int]] = {}
    for c in targets:
        try:
            ids = json.loads(c.get("representative_ids") or "[]")
        except Exception:
            ids = []
        per_cluster_ids[c["cluster_id"]] = ids[:5]
        all_rep_ids.extend(ids[:5])
    members_by_id: Dict[int, Dict] = {}
    for m in _fetch_members(db, list(set(all_rep_ids))):
        members_by_id[m["id"]] = m

    done = 0
    ok = 0
    t0 = time.monotonic()

    def _process(c: Dict) -> Tuple[Dict, Dict]:
        ids = per_cluster_ids.get(c["cluster_id"], [])
        members = [members_by_id[i] for i in ids if i in members_by_id]
        user = _build_user_content(c, members)
        data = _call_claude(client, user)
        return c, data

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_process, c): c for c in targets}
        for fut in as_completed(futs):
            c, data = fut.result()
            if not data.get("error"):
                _save(db, c["cluster_id"], data)
                ok += 1
                if verbose:
                    print(f"  #{c['cluster_id']:>5} (n={c['size']:>4}): "
                          f"{c.get('label')[:40]:<42} -> {data['short_label']}")
            else:
                logger.warning("cluster %s: %s", c["cluster_id"], data["error"])
            done += 1
            if done % 50 == 0 or done == len(targets):
                dt = time.monotonic() - t0
                logger.info("relabel: %d/%d (%d ok) in %.1fs (%.1f/s)",
                            done, len(targets), ok, dt,
                            done / dt if dt > 0 else 0)
    db.conn.close()


def report(db_path: str) -> None:
    db = Database(db_path)
    _ensure_columns(db)
    cur = db.conn.cursor()
    tot = cur.execute(
        "SELECT COUNT(*) FROM idea_clusters WHERE cluster_id != -1"
    ).fetchone()[0]
    labeled = cur.execute(
        "SELECT COUNT(*) FROM idea_clusters "
        "WHERE cluster_id != -1 AND llm_label IS NOT NULL AND llm_label != ''"
    ).fetchone()[0]
    print(f"\n=== idea_clusters labels ===")
    print(f"  {labeled} / {tot} clusters relabeled "
          f"({labeled/tot*100:.0f}%)" if tot else "  none")
    for r in cur.execute(
        "SELECT cluster_id, size, label, llm_label FROM idea_clusters "
        "WHERE llm_label IS NOT NULL AND llm_label != '' "
        "ORDER BY size DESC LIMIT 10"
    ):
        print(f"  #{r['cluster_id']:>5}  n={r['size']:>4}  "
              f"{(r['label'] or '')[:40]:<42} -> {r['llm_label']}")
    db.conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.report_only:
        report(args.db)
        return

    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
        sys.exit("Neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set")

    run(args.db, args.limit, args.workers, args.refresh, args.verbose)


if __name__ == "__main__":
    main()
