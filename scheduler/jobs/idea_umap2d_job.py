"""
idea_umap2d_job — compute 2D UMAP coordinates for every row in
company_ideas so the /ideas overview page can render a scatter "map of
idea space."

Deliberately separate from idea_cluster_job so we can refresh the 2D
coordinates without touching cluster_id / idea_clusters — cluster ids
must stay stable because idea_extraction, gap_feedback, and cluster
labels all key off them.

Pipeline:
    1. Embed text (same MiniLM recipe as the clustering job)
    2. UMAP(n_components=2) with fixed random_state=42
    3. Persist to company_ideas.umap_x / .umap_y (columns auto-created)

Takes ~40s on 26K rows on CPU.

Usage:
    python3 -m scheduler.jobs.idea_umap2d_job
    python3 -m scheduler.jobs.idea_umap2d_job --refresh    # re-run from scratch
"""

from __future__ import annotations

import argparse
import logging
from typing import List

import numpy as np

from persistence.database import Database
from scheduler.jobs.idea_cluster_job import build_text, fetch_rows

logger = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _ensure_columns(db: Database) -> None:
    cur = db.conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(company_ideas)")}
    if "umap_x" not in cols:
        cur.execute("ALTER TABLE company_ideas ADD COLUMN umap_x REAL")
    if "umap_y" not in cols:
        cur.execute("ALTER TABLE company_ideas ADD COLUMN umap_y REAL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ideas_umap ON company_ideas(umap_x, umap_y)")
    db.conn.commit()


def run(db_path: str, refresh: bool) -> None:
    db = Database(db_path)
    _ensure_columns(db)

    if not refresh:
        cur = db.conn.cursor()
        n_missing = cur.execute(
            "SELECT COUNT(*) FROM company_ideas WHERE umap_x IS NULL"
        ).fetchone()[0]
        n_total = cur.execute("SELECT COUNT(*) FROM company_ideas").fetchone()[0]
        if n_missing == 0:
            logger.info("umap_x already populated for all %d rows; use --refresh to recompute", n_total)
            db.conn.close()
            return
        logger.info("umap_x missing on %d / %d rows", n_missing, n_total)

    rows = fetch_rows(db)
    logger.info("fetched %d company_ideas rows", len(rows))
    texts = [build_text(r) for r in rows]
    keep_idx = [i for i, t in enumerate(texts) if len(t) >= 20]
    dropped = len(rows) - len(keep_idx)
    rows = [rows[i] for i in keep_idx]
    texts = [texts[i] for i in keep_idx]
    logger.info("dropped %d empty-text rows; embedding %d texts", dropped, len(rows))

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(EMBED_MODEL)
    X = model.encode(texts, batch_size=64, show_progress_bar=True,
                     normalize_embeddings=True)
    X = np.asarray(X)

    import umap
    logger.info("UMAP reducing %d x %d -> n_components=2", X.shape[0], X.shape[1])
    reducer = umap.UMAP(n_components=2, n_neighbors=15, min_dist=0.1,
                        metric="cosine", random_state=42)
    X2 = reducer.fit_transform(X)

    # Persist
    cur = db.conn.cursor()
    logger.info("writing umap_x/umap_y to %d rows", len(rows))
    with db.conn:
        for (x, y), r in zip(X2, rows):
            cur.execute(
                "UPDATE company_ideas SET umap_x = ?, umap_y = ? WHERE id = ?",
                (float(x), float(y), r["id"]),
            )

    n_populated = cur.execute(
        "SELECT COUNT(*) FROM company_ideas WHERE umap_x IS NOT NULL"
    ).fetchone()[0]
    logger.info("done. %d rows now have umap_x / umap_y", n_populated)
    db.conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--refresh", action="store_true",
                   help="Recompute even for rows that already have umap_x")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    run(args.db, args.refresh)


if __name__ == "__main__":
    main()
