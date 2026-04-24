"""
idea_gap_rank — score and persist every (mechanism × sector) pair by
recombination attractiveness, so the gap view is queryable from SQL or a
future web UI instead of recomputed on every report.

Scoring model:
    For each pair (m, s):
      gap_size  = max(0, expected_cell - actual_cell)
      proof     = log1p(mech_uses)      # log to damp mega-popular mechanisms
      demand    = log1p(sector_uses)
      solo_boost= (1 + avg_solo_fraction)   # 1.0..2.0
      ai_boost  = (1 + avg_ai_fraction)     # 1.0..2.0
      score     = gap_size * proof * demand * solo_boost

    expected_cell = (mech_uses * sector_uses) / total_pair_instances
    — the "what we'd expect if mechanism and sector were independent"

    gap_size > 0 means the pair is under-represented vs. independence.
    Empty cells (actual = 0) with high proof × demand bubble to the top,
    which is exactly what we want: mechanisms proven elsewhere that have
    *never* been applied in an active sector.

Why NOT freshness yet:
    v1 intentionally ignores recency — idea_extraction doesn't yet
    store created_at from the source (buried in raw_json.created_at or
    .created_at_i with different formats per program). Adding a
    freshness_score is a follow-up; the static gap ranking is still the
    highest-signal view today.

Persists to table `idea_gap_ranking`, rebuilt on every run.

Usage:
    python3 -m scripts.idea_gap_rank                   # rebuild + print top 30
    python3 -m scripts.idea_gap_rank --top 50
    python3 -m scripts.idea_gap_rank --min-proof 4 --min-demand 4
    python3 -m scripts.idea_gap_rank --csv ranking.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence.database import Database  # noqa: E402


DDL = """
CREATE TABLE IF NOT EXISTS idea_gap_ranking (
    mechanism         TEXT NOT NULL,
    sector            TEXT NOT NULL,
    actual_count      INTEGER NOT NULL,   -- how many rows combine this pair
    mech_uses         INTEGER NOT NULL,   -- total uses of this mechanism
    sector_uses       INTEGER NOT NULL,   -- total uses of this sector
    expected_count    REAL    NOT NULL,   -- independence prediction
    gap_size          REAL    NOT NULL,   -- expected - actual (if positive)
    solo_fraction     REAL,               -- avg solo_buildable across cohort
    ai_fraction       REAL,               -- avg ai_first across cohort
    score             REAL NOT NULL,      -- ranking score; higher = more interesting
    rank              INTEGER NOT NULL,   -- 1..N by score desc
    computed_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (mechanism, sector)
);
CREATE INDEX IF NOT EXISTS idx_gap_rank_score ON idea_gap_ranking(score DESC);
CREATE INDEX IF NOT EXISTS idx_gap_rank_mech  ON idea_gap_ranking(mechanism);
CREATE INDEX IF NOT EXISTS idx_gap_rank_sect  ON idea_gap_ranking(sector);
"""


def _load_alias_map(db: Database, axis: str) -> Dict[str, Optional[str]]:
    cur = db.conn.cursor()
    try:
        rows = cur.execute(
            "SELECT raw_tag, canonical FROM tag_alias WHERE axis = ?",
            (axis,),
        ).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    return {r["raw_tag"]: r["canonical"] for r in rows}


def _resolve(tag: str, alias_map: Dict) -> Optional[str]:
    if not tag:
        return None
    if alias_map and tag in alias_map:
        return alias_map[tag]
    return tag.strip() or None


def _load_extractions(db: Database) -> List[dict]:
    """Each dict carries canonical mechs, canonical sects, and buildable flags."""
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        SELECT mechanism_tags, sector_tags, solo_buildable, ai_first_advantage
          FROM idea_extraction
         WHERE error IS NULL
           AND mechanism_tags IS NOT NULL
           AND sector_tags    IS NOT NULL
        """
    ).fetchall()
    mech_alias = _load_alias_map(db, "mechanism")
    sect_alias = _load_alias_map(db, "sector")

    out: List[dict] = []
    for r in rows:
        try:
            raw_m = json.loads(r["mechanism_tags"] or "[]")
            raw_s = json.loads(r["sector_tags"]    or "[]")
        except Exception:  # noqa: BLE001
            continue
        mechs = {m for m in (_resolve(t, mech_alias) for t in raw_m) if m}
        sects = {s for s in (_resolve(t, sect_alias) for t in raw_s) if s}
        if not mechs or not sects:
            continue
        out.append(
            {
                "mechs": mechs,
                "sects": sects,
                "solo": 1 if r["solo_buildable"] == 1 else 0,
                "ai":   1 if r["ai_first_advantage"] == 1 else 0,
            }
        )
    return out


def _compute_scores(
    rows: List[dict],
    min_proof: int,
    min_demand: int,
) -> List[dict]:
    """Returns a list of ranking dicts sorted by score desc.

    Gates:
      mech_uses >= min_proof   AND   sector_uses >= min_demand
    to avoid ranking noise-only gaps where either side has no signal.
    """
    mech_counts: Counter = Counter()
    sect_counts: Counter = Counter()
    cell_counts: Counter = Counter()

    # Per-mechanism and per-sector solo/ai fractions across their cohorts.
    mech_solo: Dict[str, List[int]] = defaultdict(list)
    mech_ai:   Dict[str, List[int]] = defaultdict(list)
    sect_solo: Dict[str, List[int]] = defaultdict(list)
    sect_ai:   Dict[str, List[int]] = defaultdict(list)

    total_pair_instances = 0
    for r in rows:
        for m in r["mechs"]:
            mech_counts[m] += 1
            mech_solo[m].append(r["solo"])
            mech_ai[m].append(r["ai"])
        for s in r["sects"]:
            sect_counts[s] += 1
            sect_solo[s].append(r["solo"])
            sect_ai[s].append(r["ai"])
        for m in r["mechs"]:
            for s in r["sects"]:
                cell_counts[(m, s)] += 1
                total_pair_instances += 1

    if total_pair_instances == 0:
        return []

    rankings: List[dict] = []
    for m, mu in mech_counts.items():
        if mu < min_proof:
            continue
        m_solo = sum(mech_solo[m]) / len(mech_solo[m]) if mech_solo[m] else 0
        m_ai   = sum(mech_ai[m])   / len(mech_ai[m])   if mech_ai[m]   else 0
        for s, su in sect_counts.items():
            if su < min_demand:
                continue
            actual = cell_counts.get((m, s), 0)
            expected = (mu * su) / total_pair_instances
            gap_size = max(0.0, expected - actual)
            if gap_size <= 0:
                # Pair is at or above independence — not a gap.
                continue
            s_solo = sum(sect_solo[s]) / len(sect_solo[s]) if sect_solo[s] else 0
            s_ai   = sum(sect_ai[s])   / len(sect_ai[s])   if sect_ai[s]   else 0
            avg_solo = (m_solo + s_solo) / 2
            avg_ai   = (m_ai   + s_ai)   / 2

            proof  = math.log1p(mu)
            demand = math.log1p(su)
            score  = gap_size * proof * demand * (1.0 + avg_solo)

            rankings.append({
                "mechanism": m,
                "sector": s,
                "actual_count": actual,
                "mech_uses": mu,
                "sector_uses": su,
                "expected_count": round(expected, 2),
                "gap_size": round(gap_size, 2),
                "solo_fraction": round(avg_solo, 3),
                "ai_fraction": round(avg_ai, 3),
                "score": round(score, 3),
            })

    rankings.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rankings, 1):
        r["rank"] = i
    return rankings


def _persist(db: Database, rankings: List[dict]) -> None:
    cur = db.conn.cursor()
    cur.execute("DELETE FROM idea_gap_ranking")
    cur.executemany(
        """
        INSERT INTO idea_gap_ranking
            (mechanism, sector, actual_count, mech_uses, sector_uses,
             expected_count, gap_size, solo_fraction, ai_fraction,
             score, rank)
        VALUES (:mechanism, :sector, :actual_count, :mech_uses, :sector_uses,
                :expected_count, :gap_size, :solo_fraction, :ai_fraction,
                :score, :rank)
        """,
        rankings,
    )
    db.conn.commit()


def _print_top(rankings: List[dict], top: int) -> None:
    print(f"\n=== top {min(top, len(rankings))} recombination gaps ===")
    print(
        f"  {'#':<4} {'MECHANISM':<28} {'×':<1} {'SECTOR':<22} "
        f"{'actual':>6} {'expect':>6} {'gap':>6} {'solo%':>5} {'score':>8}"
    )
    for r in rankings[:top]:
        print(
            f"  {r['rank']:<4} {r['mechanism'][:28]:<28} × "
            f"{r['sector'][:22]:<22} "
            f"{r['actual_count']:>6} "
            f"{r['expected_count']:>6.1f} "
            f"{r['gap_size']:>6.1f} "
            f"{int(r['solo_fraction']*100):>4}% "
            f"{r['score']:>8.1f}"
        )


def _dump_csv(path: Path, rankings: List[dict]) -> None:
    if not rankings:
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rankings[0].keys()))
        w.writeheader()
        w.writerows(rankings)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--min-proof",
        type=int,
        default=30,
        help="Min mechanism uses to count as proven (default: 30)",
    )
    p.add_argument(
        "--min-demand",
        type=int,
        default=30,
        help="Min sector uses to count as active (default: 30)",
    )
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--csv", type=str, default=None)
    args = p.parse_args()

    db = Database(args.db)
    db.conn.executescript(DDL)
    db.conn.commit()

    try:
        rows = _load_extractions(db)
        print(f"loaded {len(rows)} extracted rows")
        rankings = _compute_scores(rows, args.min_proof, args.min_demand)
        print(
            f"scored {len(rankings)} gap pairs "
            f"(proof>={args.min_proof}, demand>={args.min_demand})"
        )
        _persist(db, rankings)
        _print_top(rankings, args.top)
        if args.csv:
            _dump_csv(Path(args.csv), rankings)
            print(f"\nwrote csv to {args.csv}")
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
