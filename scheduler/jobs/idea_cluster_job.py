"""
idea_cluster_job — embed every `company_ideas` row and cluster them into
emergent sectors with HDBSCAN.

Text assembly per row (in order, first non-empty used; later fields
appended for richness):
    one_liner, long_description, tags (joined), website meta_description,
    website hero_h1, website hero_text (first 500 chars).

Embedding:
    sentence-transformers/all-MiniLM-L6-v2 — 384-dim, ~1 min for 4K rows on
    CPU. Vectors are L2-normalized so euclidean distance == cosine distance.

Dimensionality reduction:
    UMAP → 15 dims (n_neighbors=15, min_dist=0.0). HDBSCAN is unreliable
    directly on 384-dim semantic vectors — density estimation gets noisy in
    high-dim. This is the standard BERTopic recipe.

Clustering:
    sklearn.cluster.HDBSCAN on the UMAP-reduced vectors, min_cluster_size=8,
    min_samples=3. Noise points get cluster_id = -1.

Persists:
    company_ideas.cluster_id (new column)
    idea_clusters (new table): cluster_id, size, label, top_terms (JSON),
                  top_tags (JSON), top_programs (JSON), representatives (JSON)

Report:
    Top 30 clusters printed to stdout — size, auto-label, top tags,
    program distribution, 3 representative companies.

Usage:
    python3 -m scheduler.jobs.idea_cluster_job
    python3 -m scheduler.jobs.idea_cluster_job --min-cluster-size 20
    python3 -m scheduler.jobs.idea_cluster_job --report-only
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from persistence.database import Database

logger = logging.getLogger(__name__)


DDL_ADD_COLUMN = "ALTER TABLE company_ideas ADD COLUMN cluster_id INTEGER"

DDL_CLUSTERS = """
CREATE TABLE IF NOT EXISTS idea_clusters (
    cluster_id          INTEGER PRIMARY KEY,
    size                INTEGER NOT NULL,
    label               TEXT,
    top_terms           TEXT,      -- JSON list of (term, weight)
    top_tags            TEXT,      -- JSON list of (tag, count)
    top_programs        TEXT,      -- JSON dict of program -> count
    representative_ids  TEXT,      -- JSON list of company_ideas.id

    -- era metrics (see compute_era_metrics)
    min_year            INTEGER,
    median_year         INTEGER,
    max_year            INTEGER,
    count_pre_2015      INTEGER,
    count_2015_2022     INTEGER,
    count_2023_plus     INTEGER,
    era_class           TEXT,      -- 'hot' | 'steady' | 'rebuild_candidate'
                                   -- | 'legacy' | 'unknown'
    year_coverage_pct   REAL,      -- share of rows with a year in this cluster

    parent_cluster_id   INTEGER,   -- NULL for top-level clusters
    embedding_model     TEXT,
    created_at          TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cluster_size   ON idea_clusters(size DESC);
CREATE INDEX IF NOT EXISTS idx_cluster_era    ON idea_clusters(era_class);
CREATE INDEX IF NOT EXISTS idx_cluster_parent ON idea_clusters(parent_cluster_id);
"""

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HERO_TEXT_SNIPPET = 500   # chars of hero_text included in embedding input


# --- year backfill ---------------------------------------------------------
#
# Each program labels cohort/founding year differently. This routine
# populates `company_ideas.year_founded` wherever we can derive it from
# `batch` or `raw_json`.

_YC_SHORT_RE = re.compile(r"^[A-Za-z]+(\d{2})$")          # W09, S25, F24, P26
_YEAR_ANY_RE = re.compile(r"(19|20)\d{2}")                # any 4-digit year


def _parse_batch_year(batch: Optional[str], program: str) -> Optional[int]:
    """Best-effort year extraction from a batch label."""
    if not batch:
        return None
    b = batch.strip()
    m = _YEAR_ANY_RE.search(b)
    if m:
        return int(m.group())
    m = _YC_SHORT_RE.match(b)
    if m:
        yy = int(m.group(1))
        return 2000 + yy if yy < 60 else 1900 + yy
    return None


# Speedrun cohort -> approximate year. SR001 launched 2023 H1; ~2 per year.
_SPEEDRUN_COHORT_YEAR = {
    "SR001": 2023, "SR002": 2023, "SR003": 2024,
    "SR004": 2024, "SR005": 2025, "SR006": 2026,
}


def backfill_year_founded(db: Database) -> int:
    """Populate year_founded where NULL from batch or raw_json. Returns
    count of newly filled rows."""
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        SELECT id, program, batch, raw_json
          FROM company_ideas
         WHERE year_founded IS NULL OR year_founded = 0
        """
    ).fetchall()
    filled = 0
    for r in rows:
        y: Optional[int] = None
        # 1) Speedrun cohort → year
        if r["program"] == "a16z Speedrun" and r["batch"] in _SPEEDRUN_COHORT_YEAR:
            y = _SPEEDRUN_COHORT_YEAR[r["batch"]]
        # 2) Parse batch label
        if y is None:
            y = _parse_batch_year(r["batch"], r["program"])
        # 3) Raw JSON keys
        if y is None and r["raw_json"]:
            try:
                raw = json.loads(r["raw_json"])
                for key in ("year_founded", "founded_year",
                            "investment_year_or_founding"):
                    v = raw.get(key)
                    if isinstance(v, int) and 1970 <= v <= 2035:
                        y = v; break
                    if isinstance(v, str):
                        m = _YEAR_ANY_RE.search(v)
                        if m:
                            y = int(m.group()); break
            except Exception:  # noqa: BLE001
                pass
        if y:
            cur.execute(
                "UPDATE company_ideas SET year_founded = ? WHERE id = ?",
                (y, r["id"]),
            )
            filled += 1
    db.conn.commit()
    return filled


# --- era metrics -----------------------------------------------------------

def compute_era_metrics(years: List[int]) -> Dict[str, object]:
    """Classify a cluster's founding-year distribution.

    Classes (heuristic; re-tune as we learn):
      - 'unknown'           — <40% of rows have a year
      - 'hot'               — ≥50% founded 2023+ (recent wave, possibly
                              crowded, or actually newly viable)
      - 'legacy'            — median ≤ 2014 AND <10% founded 2023+
                              (space exists but no current entrants —
                              may be dead or dominated by incumbents)
      - 'rebuild_candidate' — median ≤ 2018 AND <20% founded 2023+ AND
                              cluster size ≥ 5 (established space with
                              little recent activity — AI-first rebuilds
                              plausible)
      - 'steady'            — everything else (mature mix of old/new)
    """
    out = {
        "min_year": None, "median_year": None, "max_year": None,
        "count_pre_2015": 0, "count_2015_2022": 0, "count_2023_plus": 0,
        "era_class": "unknown", "year_coverage_pct": 0.0,
    }
    valid = [y for y in years if y and 1970 <= y <= 2035]
    if not years:
        return out
    out["year_coverage_pct"] = round(len(valid) / len(years), 2)
    if not valid:
        return out
    valid.sort()
    out["min_year"] = valid[0]
    out["max_year"] = valid[-1]
    out["median_year"] = valid[len(valid) // 2]
    for y in valid:
        if y < 2015: out["count_pre_2015"] += 1
        elif y < 2023: out["count_2015_2022"] += 1
        else: out["count_2023_plus"] += 1
    recent_ratio = out["count_2023_plus"] / len(valid)
    if out["year_coverage_pct"] < 0.4:
        out["era_class"] = "unknown"
    elif recent_ratio >= 0.5:
        out["era_class"] = "hot"
    elif out["median_year"] <= 2014 and recent_ratio < 0.1 and len(valid) >= 5:
        out["era_class"] = "legacy"
    elif out["median_year"] <= 2018 and recent_ratio < 0.2 and len(valid) >= 5:
        out["era_class"] = "rebuild_candidate"
    else:
        out["era_class"] = "steady"
    return out


# --- text assembly ---------------------------------------------------------

def _clean(t: Optional[str]) -> str:
    if not t:
        return ""
    return re.sub(r"\s+", " ", t).strip()


def build_text(row) -> str:
    """Return the text we feed the embedding model for this row.

    Concatenates the most informative fields, separated by " | ". The
    embedding model handles the separator token fine and the order puts
    the strongest signal (hand-written one-liners) first.
    """
    parts: List[str] = []
    for key in ("one_liner", "long_description"):
        v = _clean(row[key])
        if v:
            parts.append(v)
    tags_raw = row["tags_normalized"] or row["tags_json"]
    if tags_raw:
        try:
            tag_list = json.loads(tags_raw)
            tag_text = ", ".join(tag_list[:10])
            if tag_text:
                parts.append(tag_text)
        except Exception:  # noqa: BLE001
            pass
    for key in ("meta_description", "hero_h1"):
        v = _clean(row[key] if row[key] is not None else "")
        if v:
            parts.append(v)
    hero = _clean(row["hero_text"] if row["hero_text"] is not None else "")
    if hero:
        parts.append(hero[:HERO_TEXT_SNIPPET])
    # Deduplicate parts to avoid triple-counting the same paragraph in the
    # embedding input (e.g. when meta == one_liner exactly).
    seen = set()
    uniq = []
    for p in parts:
        key = p[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return " | ".join(uniq).strip()


def fetch_rows(db: Database) -> List[dict]:
    cur = db.conn.cursor()
    rows = cur.execute("""
        SELECT ci.id, ci.program, ci.company, ci.batch,
               ci.one_liner, ci.long_description,
               ci.tags_json, ci.tags_normalized,
               ci.company_website, ci.normalized_website,
               ci.country, ci.status, ci.year_founded,
               we.meta_description, we.hero_h1, we.hero_text
          FROM company_ideas ci
     LEFT JOIN website_enrichment we
            ON we.normalized_website = ci.normalized_website
    """).fetchall()
    return [dict(r) for r in rows]


# --- labelling helpers -----------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "with", "in", "on",
    "by", "at", "from", "is", "are", "was", "were", "be", "been", "it",
    "that", "this", "we", "our", "their", "they", "you", "your", "us",
    "as", "but", "so", "do", "does", "has", "have", "had", "not",
    "new", "next", "platform", "company", "startup", "use", "using",
    "based", "world", "people", "make", "makes", "making", "build",
    "building", "team", "users", "user", "more", "than", "first",
    "every", "all", "work", "works", "helping", "help", "helps", "one",
    "any", "about", "also", "only", "just",
}


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z][a-zA-Z-]{1,}", text.lower())
            if len(t) > 2 and t not in _STOPWORDS]


def top_terms_per_cluster(
    labels: np.ndarray,
    texts: List[str],
    max_terms: int = 8,
) -> Dict[int, List[Tuple[str, float]]]:
    """TF-IDF at the *cluster* level: each cluster is one document."""
    from sklearn.feature_extraction.text import TfidfVectorizer

    uniq = sorted(set(labels.tolist()))
    bucket = {c: [] for c in uniq}
    for lbl, txt in zip(labels, texts):
        bucket[int(lbl)].append(txt)

    order = [c for c in uniq if c != -1]  # ignore noise in TF-IDF fit
    if not order:
        return {}
    docs = [" ".join(bucket[c]) for c in order]
    # Auto-tune min_df/max_df so sub-clusters with few docs don't raise.
    n = len(docs)
    min_df = 2 if n >= 5 else 1
    max_df = 0.6 if n >= 5 else 1.0
    vec = TfidfVectorizer(
        tokenizer=_tokenize,
        token_pattern=None,  # silence warning — we supply a tokenizer
        min_df=min_df,
        max_df=max_df,
        ngram_range=(1, 2),
    )
    try:
        X = vec.fit_transform(docs)
    except ValueError:
        # Still too few features after auto-tune — skip TF-IDF, return empty.
        return {c: [] for c in order}
    vocab = np.array(vec.get_feature_names_out())
    out: Dict[int, List[Tuple[str, float]]] = {}
    for i, c in enumerate(order):
        row = X[i].toarray().ravel()
        if row.max() == 0:
            out[c] = []
            continue
        top_idx = row.argsort()[::-1][:max_terms]
        out[c] = [(vocab[j], float(row[j])) for j in top_idx if row[j] > 0]
    return out


def top_tags_per_cluster(
    labels: np.ndarray,
    rows: List[dict],
    max_tags: int = 6,
) -> Dict[int, List[Tuple[str, int]]]:
    out: Dict[int, Counter] = {}
    for lbl, r in zip(labels, rows):
        lbl = int(lbl)
        out.setdefault(lbl, Counter())
        raw = r.get("tags_normalized") or r.get("tags_json")
        if not raw:
            continue
        try:
            for t in json.loads(raw):
                if t:
                    out[lbl][t] += 1
        except Exception:  # noqa: BLE001
            continue
    return {c: cnt.most_common(max_tags) for c, cnt in out.items()}


def program_distribution(labels: np.ndarray, rows: List[dict]) -> Dict[int, Dict[str, int]]:
    out: Dict[int, Counter] = {}
    for lbl, r in zip(labels, rows):
        lbl = int(lbl)
        out.setdefault(lbl, Counter())
        out[lbl][r["program"]] += 1
    return {c: dict(cnt.most_common()) for c, cnt in out.items()}


def representative_indices(
    X: np.ndarray,
    labels: np.ndarray,
    k: int = 5,
) -> Dict[int, List[int]]:
    """For each cluster, return indices of the k rows closest to the mean."""
    reps: Dict[int, List[int]] = {}
    for c in sorted(set(labels.tolist())):
        if c == -1:
            continue
        idx = np.where(labels == c)[0]
        if len(idx) == 0:
            continue
        centroid = X[idx].mean(axis=0)
        # Closest by cosine — vectors are L2-normalized already.
        sims = X[idx] @ centroid
        best = idx[sims.argsort()[::-1][:k]]
        reps[int(c)] = best.tolist()
    return reps


def make_label(top_terms: List[Tuple[str, float]],
               top_tags: List[Tuple[str, int]]) -> str:
    """Auto-label: prefer the top tag if it's clearly dominant, else join
    the two strongest TF-IDF terms."""
    tag = top_tags[0][0] if top_tags else None
    tag2 = top_tags[1][0] if len(top_tags) > 1 else None
    term_bits = [t for t, _ in top_terms[:2]]
    # If the top tag is distinctive and not just 'ai' or 'saas', lead with it.
    if tag and tag not in {"ai", "b2b", "saas", "enterprise", "consumer"}:
        if term_bits:
            return f"{tag} — {term_bits[0]}"
        return tag
    if term_bits:
        return " + ".join(term_bits)
    return tag or "(no label)"


# --- persistence -----------------------------------------------------------

_IDEA_CLUSTER_COLUMNS_V2 = [
    ("min_year",          "INTEGER"),
    ("median_year",       "INTEGER"),
    ("max_year",          "INTEGER"),
    ("count_pre_2015",    "INTEGER"),
    ("count_2015_2022",   "INTEGER"),
    ("count_2023_plus",   "INTEGER"),
    ("era_class",         "TEXT"),
    ("year_coverage_pct", "REAL"),
    ("parent_cluster_id", "INTEGER"),   # NULL for top-level, else points at parent
]


def _ensure_schema(db: Database) -> None:
    cur = db.conn.cursor()
    # company_ideas.cluster_id (added v1)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(company_ideas)")}
    if "cluster_id" not in cols:
        cur.execute(DDL_ADD_COLUMN)
    # idea_clusters: create-if-new; otherwise add v2 columns in-place
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='idea_clusters'"
    )
    exists = cur.fetchone() is not None
    if not exists:
        for stmt in DDL_CLUSTERS.strip().split(";"):
            if stmt.strip():
                cur.execute(stmt)
    else:
        existing = {r[1] for r in cur.execute("PRAGMA table_info(idea_clusters)")}
        for col, ctype in _IDEA_CLUSTER_COLUMNS_V2:
            if col not in existing:
                cur.execute(f"ALTER TABLE idea_clusters ADD COLUMN {col} {ctype}")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cluster_size ON idea_clusters(size DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cluster_era ON idea_clusters(era_class)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ideas_cluster ON company_ideas(cluster_id)")
    db.conn.commit()


def persist_clusters(
    db: Database,
    rows: List[dict],
    labels: np.ndarray,
    terms: Dict[int, List[Tuple[str, float]]],
    tags: Dict[int, List[Tuple[str, int]]],
    progs: Dict[int, Dict[str, int]],
    reps: Dict[int, List[int]],
) -> None:
    cur = db.conn.cursor()
    # update company_ideas.cluster_id
    cur.execute("UPDATE company_ideas SET cluster_id = NULL")
    for r, lbl in zip(rows, labels):
        cur.execute("UPDATE company_ideas SET cluster_id = ? WHERE id = ?",
                    (int(lbl), r["id"]))
    # Group years per cluster for era metrics
    years_by_cluster: Dict[int, List[int]] = {}
    for r, lbl in zip(rows, labels):
        y = r.get("year_founded")
        years_by_cluster.setdefault(int(lbl), []).append(y if y else 0)

    # refresh idea_clusters
    cur.execute("DELETE FROM idea_clusters")
    sizes = Counter(int(l) for l in labels)
    for c in sorted(set(int(l) for l in labels)):
        rep_indices = reps.get(c, [])
        rep_ids = [rows[i]["id"] for i in rep_indices]
        label = make_label(terms.get(c, []), tags.get(c, [])) if c != -1 else "(noise)"
        era = compute_era_metrics(years_by_cluster.get(c, []))
        cur.execute(
            """
            INSERT INTO idea_clusters
            (cluster_id, size, label, top_terms, top_tags, top_programs,
             representative_ids,
             min_year, median_year, max_year,
             count_pre_2015, count_2015_2022, count_2023_plus,
             era_class, year_coverage_pct,
             embedding_model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (c, sizes[c], label,
             json.dumps(terms.get(c, [])),
             json.dumps(tags.get(c, [])),
             json.dumps(progs.get(c, {})),
             json.dumps(rep_ids),
             era["min_year"], era["median_year"], era["max_year"],
             era["count_pre_2015"], era["count_2015_2022"], era["count_2023_plus"],
             era["era_class"], era["year_coverage_pct"],
             EMBED_MODEL),
        )
    db.conn.commit()


# --- report ----------------------------------------------------------------

def _render_cluster_row(r, cur) -> str:
    tags = json.loads(r["top_tags"])
    progs = json.loads(r["top_programs"])
    rep_ids = json.loads(r["representative_ids"])
    reps = cur.execute(
        f"SELECT company FROM company_ideas "
        f"WHERE id IN ({','.join('?'*len(rep_ids))})",
        rep_ids,
    ).fetchall() if rep_ids else []
    prog_str = ", ".join(f"{p}:{n}" for p, n in list(progs.items())[:4])
    tag_str = ", ".join(f"{t}({n})" for t, n in tags[:4])
    rep_str = " | ".join(f"{row['company']}" for row in reps[:3])
    era_str = f"{r['era_class']:<18}"
    years_str = ""
    if r["median_year"] is not None:
        years_str = (f" med={r['median_year']}  "
                     f"<2015:{r['count_pre_2015']}  "
                     f"15-22:{r['count_2015_2022']}  "
                     f"23+:{r['count_2023_plus']}  "
                     f"cov={int(r['year_coverage_pct']*100)}%")
    out = []
    out.append(f"\n  [#{r['cluster_id']:>3}  n={r['size']:>4}  {era_str}]  {r['label']}")
    out.append(f"      era:      {years_str.strip() or 'no years'}")
    out.append(f"      tags:     {tag_str}")
    out.append(f"      programs: {prog_str}")
    out.append(f"      reps:     {rep_str}")
    return "\n".join(out)


def print_report(db: Database, top_n: int = 30) -> None:
    cur = db.conn.cursor()
    stats = cur.execute(
        """
        SELECT COUNT(*)                                    AS total,
               COUNT(CASE WHEN cluster_id = -1 THEN 1 END) AS noise,
               COUNT(DISTINCT cluster_id)                  AS clusters
          FROM company_ideas WHERE cluster_id IS NOT NULL
        """
    ).fetchone()
    year_stats = cur.execute(
        "SELECT COUNT(year_founded) AS with_year, COUNT(*) AS total "
        "FROM company_ideas WHERE cluster_id IS NOT NULL"
    ).fetchone()
    era_mix = cur.execute(
        "SELECT era_class, COUNT(*) AS n FROM idea_clusters "
        "WHERE cluster_id != -1 GROUP BY era_class ORDER BY n DESC"
    ).fetchall()
    print(f"\n=== Clustering summary ===")
    print(f"  rows clustered : {stats['total']}")
    print(f"  clusters       : {stats['clusters'] - 1}  (plus 1 noise bucket)")
    print(f"  noise (-1)     : {stats['noise']} ({stats['noise']/stats['total']*100:.0f}%)")
    print(f"  year coverage  : {year_stats['with_year']}/{year_stats['total']} "
          f"({year_stats['with_year']/year_stats['total']*100:.0f}%)")
    print(f"  era mix        : " +
          ", ".join(f"{r['era_class']}:{r['n']}" for r in era_mix))

    # Top-N parents by size — show each with its sub-clusters indented
    parent_rows = cur.execute(
        """
        SELECT * FROM idea_clusters
         WHERE cluster_id != -1 AND parent_cluster_id IS NULL
         ORDER BY size DESC
         LIMIT ?
        """, (top_n,)
    ).fetchall()
    print(f"\n=== Top {len(parent_rows)} parent clusters by size ===")
    for r in parent_rows:
        print(_render_cluster_row(r, cur))
        subs = cur.execute(
            """
            SELECT * FROM idea_clusters
             WHERE parent_cluster_id = ?
             ORDER BY size DESC
            """, (r["cluster_id"],)
        ).fetchall()
        for s in subs:
            print("      └─ " + _render_cluster_row(s, cur)
                  .lstrip("\n").replace("\n      ", "\n         "))

    # AI-rebuild candidates — most interesting for the microbusiness use case
    rebuild = cur.execute(
        """
        SELECT * FROM idea_clusters
         WHERE era_class = 'rebuild_candidate'
         ORDER BY size DESC
        """
    ).fetchall()
    if rebuild:
        print(f"\n\n=== AI-rebuild candidates "
              f"(old-skewed clusters, low recent activity) ===")
        for r in rebuild:
            print(_render_cluster_row(r, cur))

    # Hot clusters — where everyone is showing up right now
    hot = cur.execute(
        """
        SELECT * FROM idea_clusters
         WHERE era_class = 'hot' AND size >= 10
         ORDER BY size DESC
         LIMIT 15
        """
    ).fetchall()
    if hot:
        print(f"\n\n=== Hot clusters (≥50% founded 2023+, n≥10) ===")
        for r in hot:
            print(_render_cluster_row(r, cur))


# --- main ------------------------------------------------------------------

def run(db_path: str, min_cluster_size: int, min_samples: int,
        subcluster_min_parent_size: int = 25,
        sub_min_cluster_size: int = 3,
        sub_min_samples: int = 2) -> None:
    db = Database(db_path)
    _ensure_schema(db)

    filled = backfill_year_founded(db)
    logger.info("year_founded: backfilled %d rows", filled)

    rows = fetch_rows(db)
    logger.info("fetched %d company_ideas rows", len(rows))
    texts = [build_text(r) for r in rows]
    # Drop rows with empty text — they can't be embedded usefully.
    keep_idx = [i for i, t in enumerate(texts) if len(t) >= 20]
    dropped = len(rows) - len(keep_idx)
    rows = [rows[i] for i in keep_idx]
    texts = [texts[i] for i in keep_idx]
    logger.info("dropped %d empty-text rows, %d remaining", dropped, len(rows))

    from sentence_transformers import SentenceTransformer
    logger.info("loading model %s", EMBED_MODEL)
    model = SentenceTransformer(EMBED_MODEL)
    logger.info("embedding %d texts", len(texts))
    X = model.encode(texts, batch_size=64, show_progress_bar=True,
                     normalize_embeddings=True)
    X = np.asarray(X)

    import umap
    logger.info("UMAP reducing %d x %d -> n_components=15", X.shape[0], X.shape[1])
    reducer = umap.UMAP(n_components=15, n_neighbors=15, min_dist=0.0,
                        metric="cosine", random_state=42)
    X_red = reducer.fit_transform(X)

    from sklearn.cluster import HDBSCAN
    logger.info("clustering reduced (min_cluster_size=%d, min_samples=%d)",
                min_cluster_size, min_samples)
    clf = HDBSCAN(min_cluster_size=min_cluster_size,
                  min_samples=min_samples,
                  metric="euclidean",
                  n_jobs=-1)
    labels = clf.fit_predict(X_red)

    terms = top_terms_per_cluster(labels, texts)
    tags = top_tags_per_cluster(labels, rows)
    progs = program_distribution(labels, rows)
    # Representatives computed on ORIGINAL embedding space (finer than UMAP'd).
    reps = representative_indices(X, labels)

    logger.info("persisting top-level cluster_id + idea_clusters rows")
    persist_clusters(db, rows, labels, terms, tags, progs, reps)

    if subcluster_min_parent_size:
        recursive_subcluster(db, rows, texts, X, X_red, labels,
                             min_parent_size=subcluster_min_parent_size,
                             sub_min_cluster_size=sub_min_cluster_size,
                             sub_min_samples=sub_min_samples)

    print_report(db)
    db.conn.close()


# --- recursive sub-clustering ---------------------------------------------

def recursive_subcluster(
    db: Database,
    rows: List[dict],
    texts: List[str],
    X: np.ndarray,
    X_red: np.ndarray,
    top_labels: np.ndarray,
    min_parent_size: int = 25,
    sub_min_cluster_size: int = 3,
    sub_min_samples: int = 2,
) -> None:
    """Run HDBSCAN again inside each top-level cluster of size >=
    `min_parent_size`. Rows that land in a sub-cluster get their
    `company_ideas.cluster_id` reassigned to the new leaf id; rows that
    stay noise (-1) keep their top-level id."""
    from sklearn.cluster import HDBSCAN

    cur = db.conn.cursor()

    # Allocate new cluster ids above the current max so no collisions.
    max_id = cur.execute(
        "SELECT COALESCE(MAX(cluster_id), -1) AS m FROM idea_clusters"
    ).fetchone()["m"]
    next_id = int(max_id) + 1

    # Group row indices by their current top-level label.
    by_parent: Dict[int, List[int]] = {}
    for i, lbl in enumerate(top_labels):
        by_parent.setdefault(int(lbl), []).append(i)

    parents_to_split = [
        p for p, idxs in by_parent.items()
        if p != -1 and len(idxs) >= min_parent_size
    ]
    logger.info("subcluster: %d parents with size >= %d to split",
                len(parents_to_split), min_parent_size)

    n_sub_inserted = 0
    n_rows_reassigned = 0
    for parent_id in parents_to_split:
        idx = np.array(by_parent[parent_id])
        X_sub = X_red[idx]
        sub_clf = HDBSCAN(min_cluster_size=sub_min_cluster_size,
                          min_samples=sub_min_samples,
                          metric="euclidean", n_jobs=-1)
        sub_labels_local = sub_clf.fit_predict(X_sub)
        uniq = [c for c in sorted(set(int(l) for l in sub_labels_local))
                if c != -1]
        if not uniq:
            continue  # nothing finer found; parent stays as-is

        # Assemble sub-cluster data for this parent only.
        sub_labels_global: List[int] = [-1] * len(sub_labels_local)
        id_map: Dict[int, int] = {}
        for local_id in uniq:
            id_map[local_id] = next_id
            next_id += 1
        for i, l in enumerate(sub_labels_local):
            if int(l) != -1:
                sub_labels_global[i] = id_map[int(l)]

        # Docs / rows in the same order as idx
        sub_rows = [rows[i] for i in idx]
        sub_texts = [texts[i] for i in idx]
        sub_labels_np = np.array(sub_labels_global)

        sub_terms = top_terms_per_cluster(sub_labels_np, sub_texts)
        sub_tags = top_tags_per_cluster(sub_labels_np, sub_rows)
        sub_progs = program_distribution(sub_labels_np, sub_rows)
        sub_reps = representative_indices(X[idx], sub_labels_np)

        years_by_sub: Dict[int, List[int]] = {}
        for lbl, r in zip(sub_labels_np, sub_rows):
            years_by_sub.setdefault(int(lbl), []).append(r.get("year_founded") or 0)

        # Insert idea_clusters rows and update company_ideas.cluster_id
        for new_id in id_map.values():
            mask = sub_labels_np == new_id
            size = int(mask.sum())
            if size == 0:
                continue
            rep_local = sub_reps.get(new_id, [])
            rep_ids = [sub_rows[i]["id"] for i in rep_local]
            label = make_label(sub_terms.get(new_id, []), sub_tags.get(new_id, []))
            era = compute_era_metrics(years_by_sub.get(new_id, []))
            cur.execute(
                """
                INSERT INTO idea_clusters
                (cluster_id, size, label, top_terms, top_tags, top_programs,
                 representative_ids,
                 min_year, median_year, max_year,
                 count_pre_2015, count_2015_2022, count_2023_plus,
                 era_class, year_coverage_pct,
                 parent_cluster_id, embedding_model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id, size, label,
                 json.dumps(sub_terms.get(new_id, [])),
                 json.dumps(sub_tags.get(new_id, [])),
                 json.dumps(sub_progs.get(new_id, {})),
                 json.dumps(rep_ids),
                 era["min_year"], era["median_year"], era["max_year"],
                 era["count_pre_2015"], era["count_2015_2022"],
                 era["count_2023_plus"],
                 era["era_class"], era["year_coverage_pct"],
                 parent_id, EMBED_MODEL),
            )
            n_sub_inserted += 1
            # Reassign cluster_id on company_ideas for rows that fell into
            # this sub-cluster. Rows with -1 keep the parent id.
            row_ids = [sub_rows[i]["id"] for i in np.where(mask)[0]]
            placeholders = ",".join("?" * len(row_ids))
            cur.execute(
                f"UPDATE company_ideas SET cluster_id = ? WHERE id IN ({placeholders})",
                (new_id, *row_ids),
            )
            n_rows_reassigned += len(row_ids)
        db.conn.commit()

    logger.info("subcluster: %d sub-clusters created, %d rows reassigned",
                n_sub_inserted, n_rows_reassigned)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--min-cluster-size", type=int, default=8)
    p.add_argument("--min-samples", type=int, default=3)
    p.add_argument("--subcluster-min-parent-size", type=int, default=25,
                   help="Split any top-level cluster with size >= this. "
                        "Pass 0 to disable sub-clustering.")
    p.add_argument("--sub-min-cluster-size", type=int, default=3)
    p.add_argument("--sub-min-samples", type=int, default=2)
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
    run(args.db, args.min_cluster_size, args.min_samples,
        subcluster_min_parent_size=args.subcluster_min_parent_size,
        sub_min_cluster_size=args.sub_min_cluster_size,
        sub_min_samples=args.sub_min_samples)


if __name__ == "__main__":
    main()
