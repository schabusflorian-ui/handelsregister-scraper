"""
tag_normalize — fuzzy-map the long-tail of invented mechanism/sector tags
onto the seed vocabularies defined in idea_llm_extract_job.

Why this exists:
  The LLM extraction was told to *prefer* seed tags but *invent* in
  kebab-case when nothing fit. After 7k+ extractions we have ~1,800
  distinct mechanisms and ~370 distinct sectors, when the seeds only
  define ~55 and ~45. Most of the invented ones are near-duplicates of
  seeds (e.g. "saas-subscription" ≈ "subscription-saas", "community" ≈
  "community-led-growth", "ai-engineering" ≈ "developers"). This script
  collapses those onto seeds so the gap report isn't polluted.

Approach:
  1. Canonicalize each raw tag (lowercase, kebab-case, strip JSON quoting).
  2. Exact match against the seed vocab — done.
  3. Fuzzy match with rapidfuzz.token_sort_ratio. If score >= THRESHOLD,
     accept the seed as canonical. Otherwise, keep the tag as "invented"
     (a new primitive that may be worth promoting to the seed vocab
     later).
  4. Persist the mapping in a `tag_alias` table keyed on (raw_tag, axis).

The gap report joins through this table; we don't mutate the original
idea_extraction rows — that preserves the raw model output for audit.

Re-runs safely: TRUNCATE + rebuild. Fast (few seconds).

Usage:
  python3 -m scripts.tag_normalize
  python3 -m scripts.tag_normalize --db foo.db --threshold 80
  python3 -m scripts.tag_normalize --dry-run --verbose  # inspect without writing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz, process  # noqa: E402

from persistence.database import Database  # noqa: E402
from scheduler.jobs.idea_llm_extract_job import (  # noqa: E402
    MECHANISM_VOCAB,
    SECTOR_VOCAB,
)


DDL = """
CREATE TABLE IF NOT EXISTS tag_alias (
    raw_tag     TEXT NOT NULL,
    axis        TEXT NOT NULL,        -- 'mechanism' or 'sector'
    canonical   TEXT,                 -- NULL for dropped junk
    score       REAL,                 -- 100 = exact, 0..100 fuzzy
    method      TEXT NOT NULL,        -- 'exact' | 'fuzzy' | 'invented' | 'dropped'
    occurrences INTEGER NOT NULL,     -- how many times seen across idea_extraction
    PRIMARY KEY (raw_tag, axis)
);
CREATE INDEX IF NOT EXISTS idx_alias_canonical ON tag_alias(canonical);
CREATE INDEX IF NOT EXISTS idx_alias_method    ON tag_alias(method);
"""


_WS_RE = re.compile(r"\s+")


def canonicalize(raw: str) -> Optional[str]:
    """Lowercase kebab-case, strip JSON-string leftovers and stray brackets.

    Returns None for empty / <UNKNOWN> / pure junk so the caller can drop them.
    """
    if not raw:
        return None
    t = raw.strip()
    # Strip repeated layers of quotes/brackets — the model occasionally emits
    # values like '["saas"]' or '">saas' because of tool-use serialization.
    for _ in range(4):
        before = t
        t = t.strip().strip("[").strip("]").strip('"').strip("'").strip(">").strip("<")
        if t == before:
            break
    t = _WS_RE.sub("-", t.lower())
    t = t.replace("_", "-")
    t = re.sub(r"-+", "-", t).strip("-")
    if not t:
        return None
    if t in {"unknown", "n-a", "na", "none", "null"}:
        return None
    return t


def build_alias_map(
    raw_counter: Counter,
    vocab: List[str],
    threshold: int,
    verbose: bool,
) -> List[Tuple[str, Optional[str], float, str, int]]:
    """Return (raw_tag, canonical, score, method, occurrences) tuples.

    `vocab` is the seed list (already in canonical kebab-case).
    """
    vocab_set = set(vocab)
    out: List[Tuple[str, Optional[str], float, str, int]] = []

    for raw, n in raw_counter.most_common():
        canon = canonicalize(raw)
        if canon is None:
            out.append((raw, None, 0.0, "dropped", n))
            continue
        if canon in vocab_set:
            out.append((raw, canon, 100.0, "exact", n))
            continue
        # Fuzzy — token_sort_ratio handles word-order flips like
        # "saas-subscription" vs "subscription-saas".
        match = process.extractOne(
            canon, vocab, scorer=fuzz.token_sort_ratio
        )
        if match is not None and match[1] >= threshold:
            out.append((raw, match[0], float(match[1]), "fuzzy", n))
        else:
            # Keep the canonicalized form as its own "invented" tag — useful
            # for spotting emergent primitives to promote into the seed vocab.
            out.append((raw, canon, 0.0, "invented", n))
    return out


def _load_raw_counts(db: Database, axis: str) -> Counter:
    """Count occurrences of every raw tag on a given axis across extractions."""
    column = "mechanism_tags" if axis == "mechanism" else "sector_tags"
    cur = db.conn.cursor()
    c: Counter = Counter()
    rows = cur.execute(
        f"SELECT {column} FROM idea_extraction WHERE error IS NULL "
        f"AND {column} IS NOT NULL"
    ).fetchall()
    for r in rows:
        try:
            tags = json.loads(r[column] or "[]")
        except Exception:  # noqa: BLE001
            continue
        for t in tags:
            if not isinstance(t, str):
                continue
            c[t] += 1
    return c


def _persist(
    db: Database,
    axis: str,
    rows: List[Tuple[str, Optional[str], float, str, int]],
) -> None:
    cur = db.conn.cursor()
    cur.execute("DELETE FROM tag_alias WHERE axis = ?", (axis,))
    cur.executemany(
        "INSERT INTO tag_alias (raw_tag, axis, canonical, score, method, occurrences) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(r[0], axis, r[1], r[2], r[3], r[4]) for r in rows],
    )
    db.conn.commit()


def _summary(
    axis: str,
    rows: List[Tuple[str, Optional[str], float, str, int]],
    top_invented: int,
) -> None:
    by_method: Counter = Counter()
    occ_by_method: Counter = Counter()
    for _, _, _, method, n in rows:
        by_method[method] += 1
        occ_by_method[method] += n
    total_distinct = sum(by_method.values())
    total_occ = sum(occ_by_method.values())

    print(f"\n=== {axis} normalization ===")
    print(f"  distinct raw tags:   {total_distinct}")
    print(f"  total occurrences:   {total_occ}")
    for m in ("exact", "fuzzy", "invented", "dropped"):
        d = by_method.get(m, 0)
        o = occ_by_method.get(m, 0)
        d_pct = 100 * d / total_distinct if total_distinct else 0
        o_pct = 100 * o / total_occ if total_occ else 0
        print(
            f"    {m:<10} {d:>5} tags  ({d_pct:>4.1f}%)   "
            f"{o:>7} occ  ({o_pct:>4.1f}%)"
        )

    invented = [r for r in rows if r[3] == "invented"]
    invented.sort(key=lambda r: r[4], reverse=True)
    if invented:
        print(
            f"\n  top {min(top_invented, len(invented))} invented tags "
            f"(candidates for seed promotion):"
        )
        for raw, canon, _, _, n in invented[:top_invented]:
            print(f"    {n:>4}  {canon}   (raw: {raw!r})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--threshold",
        type=int,
        default=85,
        help="Fuzzy match threshold on token_sort_ratio (0-100). "
        "85 is conservative; 75 folds more aggressively.",
    )
    p.add_argument(
        "--top-invented",
        type=int,
        default=25,
        help="How many invented tags to print per axis",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary without writing to tag_alias",
    )
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    db = Database(args.db)
    cur = db.conn.cursor()
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    db.conn.commit()

    try:
        for axis, vocab in (("mechanism", MECHANISM_VOCAB),
                            ("sector",    SECTOR_VOCAB)):
            raw_counter = _load_raw_counts(db, axis)
            rows = build_alias_map(
                raw_counter, vocab, args.threshold, args.verbose
            )
            if not args.dry_run:
                _persist(db, axis, rows)
            _summary(axis, rows, args.top_invented)

        if args.dry_run:
            print("\n(dry-run: no changes written)")
        else:
            total = cur.execute(
                "SELECT COUNT(*) FROM tag_alias"
            ).fetchone()[0]
            print(f"\n=> wrote {total} alias rows into tag_alias")
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
