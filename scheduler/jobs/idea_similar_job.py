"""
idea_similar_job — pre-compute the 10 nearest semantic neighbours for
every row in company_ideas so the /ideas/{id} page can show
"Similar companies" and /ideas/api/similar/{id}.json can answer
without any ML dep on the Railway image.

Approach:
    1. Load every company_ideas row + its joined text (same recipe as
       the cluster job's build_text).
    2. Embed with sentence-transformers/all-MiniLM-L6-v2 (L2-norm'd).
    3. For each row, compute cosine similarity vs. all others by doing
       a single matmul X @ X.T — O(N² * D) but N=26K × D=384 is ~3s
       with numpy on laptop CPU.
    4. For each row, keep the top 10 (excluding self), store in a
       dedicated table `idea_nearest` (company_idea_id, rank, neighbor_id,
       similarity). Clustered by source so lookups are O(log N).

Runs LOCALLY. The resulting table is ~260K rows (~5 MB in SQLite) and
ships to Railway via the seed dump. Railway itself never embeds — the
endpoint is a cheap JOIN.

Usage:
    python3 -m scheduler.jobs.idea_similar_job                    # full
    python3 -m scheduler.jobs.idea_similar_job --top-k 20         # more neighbours
    python3 -m scheduler.jobs.idea_similar_job --report-only
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import List

import numpy as np

from persistence.database import Database
from scheduler.jobs.idea_cluster_job import build_text, fetch_rows

logger = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


_DDL = """
CREATE TABLE IF NOT EXISTS idea_nearest (
    company_idea_id  INTEGER NOT NULL,
    rank             INTEGER NOT NULL,      -- 1..top_k
    neighbor_id      INTEGER NOT NULL,
    similarity       REAL    NOT NULL,      -- cosine, 0..1
    PRIMARY KEY (company_idea_id, rank),
    FOREIGN KEY (company_idea_id) REFERENCES company_ideas(id),
    FOREIGN KEY (neighbor_id)     REFERENCES company_ideas(id)
);
CREATE INDEX IF NOT EXISTS idx_nearest_neighbor ON idea_nearest(neighbor_id);
"""


def _ensure_table(db: Database) -> None:
    cur = db.conn.cursor()
    for stmt in _DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    db.conn.commit()


def run(db_path: str, top_k: int, batch: int) -> None:
    db = Database(db_path)
    _ensure_table(db)

    rows = fetch_rows(db)
    texts = [build_text(r) for r in rows]
    keep = [(i, r) for i, (r, t) in enumerate(zip(rows, texts)) if len(t) >= 20]
    logger.info("kept %d of %d rows (rest lacked text)", len(keep), len(rows))
    if not keep:
        return
    rows_kept = [r for _, r in keep]
    texts_kept = [texts[i] for i, _ in keep]
    ids = np.array([r["id"] for r in rows_kept], dtype=np.int64)

    # --- embed ---
    from sentence_transformers import SentenceTransformer
    logger.info("loading %s", EMBED_MODEL)
    model = SentenceTransformer(EMBED_MODEL)
    logger.info("embedding %d texts", len(texts_kept))
    X = model.encode(texts_kept, batch_size=64, show_progress_bar=True,
                     normalize_embeddings=True).astype(np.float32)
    logger.info("X shape: %s", X.shape)

    # --- nearest-neighbour via chunked matmul ---
    # For 26K rows the full pairwise sim matrix is 26K x 26K x 4B = 2.7 GB —
    # too big. Instead compute similarity in row-chunks and keep only the
    # top-k per row. `sim = X[i:i+batch] @ X.T` keeps memory to
    # batch × N × 4B (e.g. 256 × 26K × 4B = 26 MB per chunk).
    N = X.shape[0]
    all_rank_idx = np.empty((N, top_k), dtype=np.int64)
    all_rank_sim = np.empty((N, top_k), dtype=np.float32)

    t0 = time.monotonic()
    for i in range(0, N, batch):
        j = min(i + batch, N)
        chunk = X[i:j] @ X.T                            # (b, N) cosine (X is unit-norm)
        # Mask self-similarity so each row's own nearest is dropped.
        for k, row_i in enumerate(range(i, j)):
            chunk[k, row_i] = -1.0
        # argpartition picks top_k indices, then sort within that slice.
        # Using negative similarity for argpartition of "largest".
        part = np.argpartition(-chunk, top_k, axis=1)[:, :top_k]
        for k in range(j - i):
            row_idx = part[k]
            row_sim = chunk[k, row_idx]
            order = np.argsort(-row_sim)
            all_rank_idx[i + k] = row_idx[order]
            all_rank_sim[i + k] = row_sim[order]
        if (i // batch) % 10 == 0:
            done = j
            dt = time.monotonic() - t0
            logger.info("  %d/%d (%.0f/s)", done, N, done / dt if dt > 0 else 0)

    # --- persist ---
    logger.info("writing %d nearest-neighbor rows", N * top_k)
    cur = db.conn.cursor()
    cur.execute("DELETE FROM idea_nearest")
    with db.conn:
        for row_i, src_id in enumerate(ids):
            for rank in range(top_k):
                neighbor_i = all_rank_idx[row_i, rank]
                cur.execute(
                    "INSERT INTO idea_nearest "
                    "(company_idea_id, rank, neighbor_id, similarity) "
                    "VALUES (?, ?, ?, ?)",
                    (int(src_id), rank + 1,
                     int(ids[neighbor_i]),
                     float(all_rank_sim[row_i, rank])),
                )

    n_written = cur.execute("SELECT COUNT(*) FROM idea_nearest").fetchone()[0]
    logger.info("idea_nearest: %d rows ready", n_written)
    db.conn.close()


def report(db_path: str) -> None:
    db = Database(db_path)
    _ensure_table(db)
    cur = db.conn.cursor()
    n = cur.execute("SELECT COUNT(*) FROM idea_nearest").fetchone()[0]
    per_row = cur.execute(
        "SELECT COUNT(DISTINCT company_idea_id) FROM idea_nearest"
    ).fetchone()[0]
    print(f"idea_nearest: {n} rows across {per_row} sources")
    if n == 0:
        db.conn.close()
        return
    print("\nSample (source -> top 3 neighbors):")
    for src_row in cur.execute(
        """
        SELECT ci.id, ci.company, ci.year_founded
          FROM company_ideas ci
         WHERE ci.id IN (SELECT DISTINCT company_idea_id FROM idea_nearest)
           AND ci.company IS NOT NULL AND ci.company != ''
         ORDER BY RANDOM() LIMIT 5
        """
    ).fetchall():
        src_id = src_row["id"]
        print(f"\n  #{src_id} {src_row['company']} ({src_row['year_founded']})")
        for nb in cur.execute(
            """
            SELECT n.rank, n.similarity, ci.company, ci.year_founded
              FROM idea_nearest n
              JOIN company_ideas ci ON ci.id = n.neighbor_id
             WHERE n.company_idea_id = ? AND n.rank <= 3
             ORDER BY n.rank
            """,
            (src_id,),
        ):
            print(f"    {nb['rank']}. {nb['similarity']:.3f}  "
                  f"{nb['company']} ({nb['year_founded']})")
    db.conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.report_only:
        report(args.db)
    else:
        run(args.db, args.top_k, args.batch)
        report(args.db)


if __name__ == "__main__":
    main()
