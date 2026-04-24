"""
idea_tag_canonicalize_job — dedupe near-duplicate tags in
`idea_extraction.mechanism_tags` and `.sector_tags` so the
(mechanism × sector) matrix lands on a stable vocabulary.

Two passes:
  1. String normalization
     - lowercase, strip whitespace
     - underscore -> hyphen
     - drop characters that aren't alnum / hyphen / space
     - collapse repeated hyphens
     - map trivial plurals: "saas-tools" == "saas-tool"

  2. Semantic clustering (sentence-transformers MiniLM + cosine >=
     threshold) — catches cases like "ai-copilot" ~ "llm-copilot",
     "e-commerce" ~ "ecommerce", "hr-tech" ~ "hr-recruiting".

For each cluster of near-duplicates we pick the **most-frequent** variant
as canonical and map everyone else to it. The mapping is logged to
stdout (--dry-run) or applied in-place (default).

Usage:
  python3 -m scheduler.jobs.idea_tag_canonicalize_job --dry-run
  python3 -m scheduler.jobs.idea_tag_canonicalize_job --threshold 0.88
  python3 -m scheduler.jobs.idea_tag_canonicalize_job          # applies

A backup of the unmapped tag arrays is saved to idea_extraction_tag_backup
the first time this runs, so the operation is reversible.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from persistence.database import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pass 1 — string normalization
# ---------------------------------------------------------------------------

_STRIP_PUNCT = re.compile(r"[^a-z0-9\- ]+")
_COLLAPSE_DASH = re.compile(r"-+")
_COLLAPSE_WS = re.compile(r"\s+")


# Tokens that LOOK plural but aren't — depluralizing them produced bugs in
# earlier runs (subscription-saa, devop, logistic, sale, analytic, etc.).
# When a hyphenated tag's final segment is in this set, we do NOT strip the
# trailing -s. Built conservatively from observation of the live DB.
_KEEP_PLURAL = frozenset({
    # Cloud / tech acronyms
    "saas", "paas", "iaas", "faas", "baas", "daas", "aws", "api", "apis",
    "css", "js", "news", "rss", "sass", "ops", "mlops", "devops",
    "aiops", "secops", "dataops", "finops", "gitops",
    # Disciplines / fields in -ics / -s
    "analytics", "logistics", "electronics", "optics", "tactics",
    "ethics", "metrics", "statistics", "economics", "physics",
    "mathematics", "politics", "graphics", "dynamics", "robotics",
    "cosmetics", "genetics", "mechanics", "aesthetics", "acoustics",
    "diagnostics", "pediatrics", "semiconductors",
    # Business/consumer functions that are canonical in plural form
    "sales", "sports", "series", "species", "premises",
    # Audience categories from our seed vocab (plurals are canonical)
    "developers", "creators", "designers", "founders", "freelancers",
    "prosumer-freelancers", "operations",
})


def normalize_string(tag: str) -> Optional[str]:
    if tag is None:
        return None
    t = tag.strip().lower().replace("_", "-").replace(" ", "-")
    t = _STRIP_PUNCT.sub("", t)
    t = _COLLAPSE_DASH.sub("-", t)
    t = _COLLAPSE_WS.sub("-", t)
    t = t.strip("-")
    if len(t) < 2:
        return None
    # Suffix-based protection: skip depluralization for words ending in
    # "-ss" (business), "-us" (corpus), "-is" (analysis), "-ys" (says).
    bad_endings = ("ss", "us", "is", "ys")
    # Token-based protection: if the full tag or its last hyphen-segment
    # is a known-singular-that-looks-plural (saas, devops, analytics,
    # sales, creators, …) leave it alone.
    last_seg = t.rsplit("-", 1)[-1]
    if t in _KEEP_PLURAL or last_seg in _KEEP_PLURAL:
        return t
    if (t.endswith("s") and not any(t.endswith(e) for e in bad_endings)
            and len(t) > 3):
        return t[:-1]
    return t


# ---------------------------------------------------------------------------
# Pass 2 — semantic clustering
# ---------------------------------------------------------------------------

def semantic_merge(
    vocab: List[str],
    counts: Counter,
    threshold: float = 0.88,
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
) -> Dict[str, str]:
    """Cluster near-duplicate tags with cosine >= threshold. Returns a
    mapping variant -> canonical (where canonical is the most common
    variant in its cluster). Tags with no near-duplicates map to
    themselves."""
    if not vocab:
        return {}
    from sentence_transformers import SentenceTransformer
    import numpy as np
    logger.info("embedding %d unique tags", len(vocab))
    model = SentenceTransformer(model_name)
    X = model.encode(vocab, normalize_embeddings=True, show_progress_bar=False)
    X = np.asarray(X)

    # Naive O(n^2) clustering — n is ~500, fine.
    n = len(vocab)
    sims = X @ X.T
    np.fill_diagonal(sims, 0.0)

    # Union-find over (i, j) edges where sims[i, j] >= threshold
    parent = list(range(n))
    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    pairs = np.argwhere(sims >= threshold)
    for i, j in pairs:
        if i < j:
            union(int(i), int(j))

    # Group by root
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    mapping: Dict[str, str] = {}
    for idxs in groups.values():
        if len(idxs) == 1:
            t = vocab[idxs[0]]
            mapping[t] = t
            continue
        # canonical = most-frequent variant in the group
        canonical_idx = max(idxs, key=lambda i: counts.get(vocab[i], 0))
        canonical = vocab[canonical_idx]
        for i in idxs:
            mapping[vocab[i]] = canonical
    return mapping


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def collect(db: Database, column: str) -> Tuple[Counter, Dict[int, List[str]]]:
    """Return (counts per canonical-after-string-norm tag, per-row tag lists)."""
    cur = db.conn.cursor()
    rows = cur.execute(
        f"SELECT company_idea_id, {column} FROM idea_extraction "
        f"WHERE {column} IS NOT NULL AND error IS NULL"
    ).fetchall()
    by_row: Dict[int, List[str]] = {}
    counts: Counter = Counter()
    for r in rows:
        try:
            tags = json.loads(r[column])
        except Exception:  # noqa: BLE001
            continue
        norm_list: List[str] = []
        for t in tags or []:
            n = normalize_string(t)
            if n:
                norm_list.append(n)
                counts[n] += 1
        if norm_list:
            by_row[r["company_idea_id"]] = norm_list
    return counts, by_row


def apply_mapping(db: Database, column: str,
                  by_row: Dict[int, List[str]],
                  mapping: Dict[str, str]) -> int:
    cur = db.conn.cursor()
    updated = 0
    for row_id, tags in by_row.items():
        new_tags = []
        seen: Set[str] = set()
        for t in tags:
            c = mapping.get(t, t)
            if c not in seen:
                seen.add(c)
                new_tags.append(c)
        cur.execute(
            f"UPDATE idea_extraction SET {column} = ? WHERE company_idea_id = ?",
            (json.dumps(new_tags), row_id),
        )
        updated += cur.rowcount
    db.conn.commit()
    return updated


def backup_once(db: Database) -> None:
    cur = db.conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='idea_extraction_tag_backup'"
    )
    if cur.fetchone():
        logger.info("backup table already exists; skipping")
        return
    cur.execute(
        """
        CREATE TABLE idea_extraction_tag_backup AS
        SELECT company_idea_id,
               mechanism_tags AS mechanism_tags_original,
               sector_tags    AS sector_tags_original,
               CURRENT_TIMESTAMP AS backed_up_at
          FROM idea_extraction
         WHERE error IS NULL
        """
    )
    db.conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM idea_extraction_tag_backup").fetchone()[0]
    logger.info("backed up %d rows to idea_extraction_tag_backup", n)


def report_mapping(label: str, mapping: Dict[str, str],
                   counts: Counter) -> None:
    # Only show lines where variant != canonical (i.e. something got merged)
    merges = [(v, c) for v, c in mapping.items() if v != c]
    if not merges:
        print(f"\n=== {label}: no merges ===")
        return
    merges.sort(key=lambda x: (x[1], -counts.get(x[0], 0)))
    print(f"\n=== {label}: {len(merges)} merges ===")
    prev_canon = None
    for variant, canonical in merges:
        if canonical != prev_canon:
            total = sum(counts.get(v, 0)
                        for v, c in mapping.items() if c == canonical)
            print(f"  -> {canonical}   (total n={total})")
            prev_canon = canonical
        print(f"       {variant}   (n={counts.get(variant, 0)})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--threshold", type=float, default=0.88,
                   help="Cosine threshold for semantic merging")
    p.add_argument("--dry-run", action="store_true",
                   help="Print proposed merges; do not modify the DB")
    p.add_argument("--columns", nargs="+",
                   default=["mechanism_tags", "sector_tags"])
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    db = Database(args.db)
    try:
        if not args.dry_run:
            backup_once(db)
        for col in args.columns:
            logger.info("processing %s", col)
            counts, by_row = collect(db, col)
            logger.info("  %d rows, %d distinct tags after string norm",
                        len(by_row), len(counts))
            vocab = [t for t, _ in counts.most_common()]  # freq desc
            mapping = semantic_merge(vocab, counts, threshold=args.threshold)
            report_mapping(col, mapping, counts)
            if not args.dry_run:
                n = apply_mapping(db, col, by_row, mapping)
                logger.info("  %s: wrote back %d rows", col, n)
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
