"""
idea_gap_report — surface (mechanism × sector) recombination gaps.

Reads `idea_extraction` tags and builds the pivot the memory describes:
which mechanisms are proven in sector A but absent/rare in sector B?
Empty or sparse cells in the (mechanism × sector) matrix are the
candidate recombination opportunities.

Outputs:
  1. Matrix summary — top N mechanisms × top N sectors, with counts
  2. Gap list — for each "proven" mechanism (used in ≥ MIN_PROVEN sectors),
     list sectors where it's absent but plausibly applicable (heuristic:
     sectors that are themselves active overall). Sorted by attractiveness.
  3. Raw pair counts — CSV dump for spreadsheet exploration.

Usage:
  python3 -m scripts.idea_gap_report                  # default DB, stdout
  python3 -m scripts.idea_gap_report --db foo.db
  python3 -m scripts.idea_gap_report --csv gaps.csv   # also dump raw pairs
  python3 -m scripts.idea_gap_report --min-proven 3   # tweak proven threshold
  python3 -m scripts.idea_gap_report --top 20         # matrix dimensions
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Allow running as a script from the repo root (mirrors other scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from persistence.database import Database  # noqa: E402


def _load_alias_map(db: Database, axis: str) -> dict:
    """Return {raw_tag: canonical_tag} for one axis.

    Falls back to identity mapping for raw tags not in tag_alias (e.g. when
    tag_normalize hasn't been run yet). 'dropped' rows map to None, which
    callers treat as "skip this tag".
    """
    cur = db.conn.cursor()
    try:
        rows = cur.execute(
            "SELECT raw_tag, canonical, method FROM tag_alias WHERE axis = ?",
            (axis,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — table missing
        return {}
    return {r["raw_tag"]: r["canonical"] for r in rows}


def _load_pairs(db: Database, use_aliases: bool = True) -> Tuple[
    List[Tuple[str, str]],   # mechanism × sector pairs (one per row per combo)
    Counter,                 # mechanism counts across all rows
    Counter,                 # sector counts across all rows
    int,                     # total extracted rows
]:
    """Unpack (mechanism, sector) pairs from every successful extraction.

    When use_aliases=True (default) and tag_alias exists, raw tags are
    replaced with their canonical form before counting. This collapses the
    long-tail invented-tag noise onto seed vocabulary.
    """
    cur = db.conn.cursor()
    rows = cur.execute(
        """
        SELECT mechanism_tags, sector_tags
          FROM idea_extraction
         WHERE error IS NULL
           AND mechanism_tags IS NOT NULL
           AND sector_tags    IS NOT NULL
        """
    ).fetchall()

    mech_alias = _load_alias_map(db, "mechanism") if use_aliases else {}
    sect_alias = _load_alias_map(db, "sector") if use_aliases else {}

    def resolve(tag: str, alias_map: dict) -> Optional[str]:
        if not tag:
            return None
        if alias_map:
            # If tag is in the alias table, use the canonical (may be None if
            # dropped). If not in the table at all, pass through unchanged.
            if tag in alias_map:
                return alias_map[tag]
        return tag.strip() or None

    pairs: List[Tuple[str, str]] = []
    mech_counts: Counter = Counter()
    sect_counts: Counter = Counter()
    for r in rows:
        try:
            raw_mechs = json.loads(r["mechanism_tags"] or "[]")
            raw_sects = json.loads(r["sector_tags"]    or "[]")
        except Exception:  # noqa: BLE001
            continue
        mechs = {m for m in (resolve(t, mech_alias) for t in raw_mechs) if m}
        sects = {s for s in (resolve(t, sect_alias) for t in raw_sects) if s}
        for m in mechs:
            mech_counts[m] += 1
        for s in sects:
            sect_counts[s] += 1
        for m in mechs:
            for s in sects:
                pairs.append((m, s))
    return pairs, mech_counts, sect_counts, len(rows)


def _build_matrix(pairs: List[Tuple[str, str]]) -> Counter:
    """Count occurrences of every (mechanism, sector) pair."""
    return Counter(pairs)


def _print_matrix(
    matrix: Counter,
    mech_counts: Counter,
    sect_counts: Counter,
    top: int,
) -> None:
    top_mechs = [m for m, _ in mech_counts.most_common(top)]
    top_sects = [s for s, _ in sect_counts.most_common(top)]

    print(f"\n=== (mechanism × sector) matrix — top {top} × top {top} ===")
    print(f"  rows  = mechanism (total uses)")
    print(f"  cols  = sector    (total uses)")
    print(f"  cells = co-occurrences; '.' = zero (gap candidate)")

    # Header
    col_w = 4
    head_w = 34
    print("")
    print(" " * head_w, end="")
    for s in top_sects:
        label = s[: col_w - 1]
        print(f" {label:>{col_w - 1}}", end="")
    print(f"   TOT")
    print(" " * head_w, end="")
    for s in top_sects:
        print(f" {sect_counts[s]:>{col_w - 1}}", end="")
    print("")

    # Rows
    for m in top_mechs:
        label = f"{m[: head_w - 8]:<{head_w - 8}} ({mech_counts[m]:>3})"
        print(f"  {label:<{head_w - 2}}", end="")
        for s in top_sects:
            v = matrix.get((m, s), 0)
            cell = "." if v == 0 else str(v)
            print(f" {cell:>{col_w - 1}}", end="")
        print("")


def _print_gaps(
    matrix: Counter,
    mech_counts: Counter,
    sect_counts: Counter,
    min_proven_sectors: int,
    min_sector_activity: int,
    max_rows: int,
) -> None:
    """For each 'proven' mechanism (applied in >= min_proven_sectors
    sectors), list sectors where it is entirely absent — candidate
    recombination slots.
    """
    print(
        f"\n=== recombination gaps — "
        f"mechanisms proven in ≥{min_proven_sectors} sectors, "
        f"absent from sectors with ≥{min_sector_activity} total uses ==="
    )

    # Build mechanism -> set(sectors it's been applied in)
    applied_in: Dict[str, Set[str]] = defaultdict(set)
    for (m, s), n in matrix.items():
        if n > 0:
            applied_in[m].add(s)

    active_sectors = {
        s for s, n in sect_counts.items() if n >= min_sector_activity
    }

    rows: List[Tuple[str, int, str, int]] = []
    for m, sectors in applied_in.items():
        if len(sectors) < min_proven_sectors:
            continue
        gap_sectors = active_sectors - sectors
        for s in gap_sectors:
            # Score: how well-established is the mechanism × how big is the
            # sector. Bigger = more interesting gap (the tag is proven and
            # the sector has obvious demand).
            score = mech_counts[m] * sect_counts[s]
            rows.append((m, mech_counts[m], s, sect_counts[s]))
    # Sort by (mech uses × sector uses), descending.
    rows.sort(key=lambda r: r[1] * r[3], reverse=True)

    if not rows:
        print(
            "  (no gaps at current thresholds — dataset too small or "
            "thresholds too strict)"
        )
        return

    print(f"  {'MECHANISM':<34} {'uses':>5}   "
          f"{'ABSENT FROM SECTOR':<28} {'sect-uses':>10}")
    for (m, mu, s, su) in rows[:max_rows]:
        print(f"  {m[:34]:<34} {mu:>5}   {s[:28]:<28} {su:>10}")
    if len(rows) > max_rows:
        print(f"  ... ({len(rows) - max_rows} more; raise --top-gaps to see)")


def _dump_csv(path: Path, matrix: Counter) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mechanism", "sector", "count"])
        for (m, s), n in sorted(
            matrix.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
        ):
            w.writerow([m, s, n])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument(
        "--top",
        type=int,
        default=12,
        help="Matrix dimensions (top N mechanisms × top N sectors)",
    )
    p.add_argument(
        "--min-proven",
        type=int,
        default=2,
        help="Min distinct sectors a mechanism must appear in "
        "to count as 'proven' (default: 2)",
    )
    p.add_argument(
        "--min-sector-activity",
        type=int,
        default=2,
        help="Min total uses for a sector to be in the gap candidate pool",
    )
    p.add_argument(
        "--top-gaps", type=int, default=25, help="Max gap rows to print"
    )
    p.add_argument(
        "--csv", type=str, default=None, help="Also dump raw pairs to CSV"
    )
    p.add_argument(
        "--no-aliases",
        action="store_true",
        help="Skip the tag_alias normalization (show raw, noisy tags)",
    )
    args = p.parse_args()

    db = Database(args.db)
    try:
        pairs, mech_counts, sect_counts, n_rows = _load_pairs(
            db, use_aliases=not args.no_aliases
        )
        print(f"loaded {n_rows} extracted rows → "
              f"{len(pairs)} (mech × sector) pair-instances")
        print(f"  distinct mechanisms: {len(mech_counts)}")
        print(f"  distinct sectors:    {len(sect_counts)}")
        if n_rows == 0:
            print("no extractions found — run idea_llm_extract_job first")
            return

        matrix = _build_matrix(pairs)
        _print_matrix(matrix, mech_counts, sect_counts, args.top)
        _print_gaps(
            matrix,
            mech_counts,
            sect_counts,
            args.min_proven,
            args.min_sector_activity,
            args.top_gaps,
        )

        if args.csv:
            out = Path(args.csv)
            _dump_csv(out, matrix)
            print(f"\nraw pairs dumped to {out}")
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
