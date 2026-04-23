"""
website_enrichment_job — lightweight homepage fetch for every company in
`company_ideas`, writing title / meta / hero-text into `website_enrichment`.

Purpose:
    Idea-clustering needs real pitch copy. Many scraped rows are thin on
    `one_liner` / `long_description` (Lux tags are internal theses; GC often
    lacks descriptions; Playground has two-sentence blurbs). The homepage
    meta description + h1 + first paragraphs typically fill this gap.

    This is deliberately NOT the full `sources/website_scraper.py` pipeline.
    We fetch one page per domain and grab the four fields we actually need
    for embedding. Keyed by `normalized_website` so cross-program dupes
    share a single fetch.

Behaviour:
    * Respects robots.txt per host (cached). Homepage disallowed -> skip,
      record reason = 'robots'.
    * ThreadPoolExecutor with `--workers` concurrency (default 8). One
      fetch per host, so concurrency across hosts is fine.
    * Idempotent. Re-running only enriches hosts not already present in
      `website_enrichment`. Pass --refresh <days> to re-fetch old rows.

Usage:
    python3 -m scheduler.jobs.website_enrichment_job
    python3 -m scheduler.jobs.website_enrichment_job --limit 50
    python3 -m scheduler.jobs.website_enrichment_job --workers 16 --timeout 20
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from persistence.database import Database

logger = logging.getLogger(__name__)


DDL = """
CREATE TABLE IF NOT EXISTS website_enrichment (
    normalized_website  TEXT PRIMARY KEY,
    fetched_url         TEXT,
    final_url           TEXT,
    http_status         INTEGER,
    title               TEXT,
    meta_description    TEXT,
    hero_h1             TEXT,
    hero_text           TEXT,          -- up to ~2000 chars of cleaned body
    lang                TEXT,
    error               TEXT,          -- 'timeout' | 'dns' | 'ssl' | 'robots' | 'non_html' | 'parse' | 'http_4xx' | 'http_5xx' | NULL
    fetched_at          TEXT NOT NULL,
    duration_ms         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_enrich_status ON website_enrichment(http_status);
CREATE INDEX IF NOT EXISTS idx_enrich_error  ON website_enrichment(error);
"""


USER_AGENT = (
    "Mozilla/5.0 (compatible; IdeaClusterBot/0.1; +https://handelsregister-scraper/ideas) "
    "Chrome/120.0 Safari/537.36"
)
MAX_BYTES = 1_024_000  # 1 MB cap — marketing homepages are all well under this
HERO_MAX_CHARS = 2000


# --- robots cache -----------------------------------------------------------

_ROBOTS_LOCK = threading.Lock()
_ROBOTS_CACHE: Dict[str, Optional[RobotFileParser]] = {}


def _robots_allows(url: str, user_agent: str = USER_AGENT, timeout: float = 5.0) -> bool:
    """Return True if robots.txt allows fetching this URL. Missing or
    unreachable robots files are treated as permissive (standard practice)."""
    host = urlparse(url).hostname
    if not host:
        return True
    with _ROBOTS_LOCK:
        rp = _ROBOTS_CACHE.get(host, "MISS")
    if rp == "MISS":
        rp_obj = RobotFileParser()
        robots_url = f"{urlparse(url).scheme or 'https'}://{host}/robots.txt"
        try:
            r = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=timeout)
            if r.status_code >= 400 or not r.text.strip():
                rp_obj = None
            else:
                rp_obj.parse(r.text.splitlines())
        except Exception:  # noqa: BLE001
            rp_obj = None
        with _ROBOTS_LOCK:
            _ROBOTS_CACHE[host] = rp_obj
        rp = rp_obj
    if rp is None:
        return True
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True


# --- fetch + parse ----------------------------------------------------------

def _clean_text(soup: BeautifulSoup) -> str:
    # Strip anything that isn't part of the pitch copy.
    for tag in soup.find_all(["script", "style", "noscript", "nav", "footer",
                              "header", "aside", "svg", "iframe", "form"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _guess_url(raw: str) -> str:
    raw = raw.strip()
    if "://" not in raw:
        raw = "https://" + raw
    return raw


def fetch_one(url: str, timeout: float = 15.0) -> Dict[str, Optional[object]]:
    """Fetch a homepage and extract the four fields we care about."""
    start = time.monotonic()
    out: Dict[str, Optional[object]] = {
        "fetched_url": url, "final_url": None, "http_status": None,
        "title": None, "meta_description": None, "hero_h1": None,
        "hero_text": None, "lang": None, "error": None,
    }
    try:
        if not _robots_allows(url):
            out["error"] = "robots"
            return out
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT,
                     "Accept-Language": "en,de;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        out["final_url"] = r.url
        out["http_status"] = r.status_code
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "html" not in ctype and "xml" not in ctype:
            out["error"] = "non_html"
            return out
        if r.status_code >= 500:
            out["error"] = "http_5xx"
            return out
        if r.status_code >= 400:
            out["error"] = "http_4xx"
            return out

        # Read at most MAX_BYTES of the body.
        body = b""
        for chunk in r.iter_content(chunk_size=8192):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        text = body.decode(r.encoding or "utf-8", errors="replace")

        soup = BeautifulSoup(text[:MAX_BYTES], "html.parser")
        if soup.title and soup.title.string:
            out["title"] = re.sub(r"\s+", " ", soup.title.string).strip()[:500]
        for m in soup.find_all("meta"):
            name = (m.get("name") or m.get("property") or "").lower()
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if name in ("description", "og:description", "twitter:description") \
                    and not out["meta_description"]:
                out["meta_description"] = content[:1000]
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            out["lang"] = html_tag["lang"][:16]
        h1 = soup.find("h1")
        if h1:
            t = re.sub(r"\s+", " ", h1.get_text(strip=True))
            if t:
                out["hero_h1"] = t[:500]
        out["hero_text"] = _clean_text(soup)[:HERO_MAX_CHARS] or None
    except requests.exceptions.SSLError:
        out["error"] = "ssl"
    except requests.exceptions.ConnectTimeout:
        out["error"] = "timeout"
    except requests.exceptions.ReadTimeout:
        out["error"] = "timeout"
    except requests.exceptions.ConnectionError as e:
        # DNS failures + refused connections land here
        out["error"] = "dns" if "Name or service not known" in str(e) or \
                                "nodename nor servname" in str(e) or \
                                "getaddrinfo" in str(e).lower() else "connection"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"parse:{type(e).__name__}"
    finally:
        out["duration_ms"] = int((time.monotonic() - start) * 1000)
    return out


# --- DB coordination --------------------------------------------------------

def _ensure_schema(db: Database) -> None:
    cur = db.conn.cursor()
    for stmt in DDL.strip().split(";"):
        if stmt.strip():
            cur.execute(stmt)
    db.conn.commit()


def _targets(db: Database, limit: Optional[int], refresh_days: Optional[int]
             ) -> List[Tuple[str, str]]:
    """Return [(normalized_website, url), ...] to enrich."""
    cur = db.conn.cursor()
    if refresh_days is not None:
        cutoff = (datetime.now() - timedelta(days=refresh_days)).isoformat()
        sql = """
            SELECT ci.normalized_website, MIN(ci.company_website) AS url
              FROM company_ideas ci
         LEFT JOIN website_enrichment we
                ON we.normalized_website = ci.normalized_website
             WHERE ci.normalized_website IS NOT NULL
               AND ci.company_website IS NOT NULL
               AND (we.normalized_website IS NULL OR we.fetched_at < ?)
          GROUP BY ci.normalized_website
        """
        rows = cur.execute(sql, (cutoff,)).fetchall()
    else:
        sql = """
            SELECT ci.normalized_website, MIN(ci.company_website) AS url
              FROM company_ideas ci
         LEFT JOIN website_enrichment we
                ON we.normalized_website = ci.normalized_website
             WHERE ci.normalized_website IS NOT NULL
               AND ci.company_website IS NOT NULL
               AND we.normalized_website IS NULL
          GROUP BY ci.normalized_website
        """
        rows = cur.execute(sql).fetchall()
    targets = [(r["normalized_website"], r["url"]) for r in rows]
    if limit:
        targets = targets[:limit]
    return targets


_INSERT_LOCK = threading.Lock()  # sqlite3 connections are per-thread-only


def _save(db: Database, norm: str, url: str, data: Dict[str, Optional[object]]) -> None:
    with _INSERT_LOCK:
        cur = db.conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO website_enrichment
            (normalized_website, fetched_url, final_url, http_status,
             title, meta_description, hero_h1, hero_text, lang, error,
             fetched_at, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (norm, url, data.get("final_url"), data.get("http_status"),
             data.get("title"), data.get("meta_description"),
             data.get("hero_h1"), data.get("hero_text"),
             data.get("lang"), data.get("error"),
             datetime.now().isoformat(), data.get("duration_ms")),
        )
        db.conn.commit()


# --- main loop --------------------------------------------------------------

def run(db_path: str, workers: int, limit: Optional[int], timeout: float,
        refresh_days: Optional[int]) -> None:
    db = Database(db_path)
    _ensure_schema(db)
    targets = _targets(db, limit, refresh_days)
    logger.info("enrichment: %d sites to fetch (workers=%d, timeout=%.0fs)",
                len(targets), workers, timeout)
    if not targets:
        return

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_target = {
            pool.submit(fetch_one, _guess_url(url), timeout): (norm, url)
            for norm, url in targets
        }
        t0 = time.monotonic()
        for fut in as_completed(future_to_target):
            norm, url = future_to_target[fut]
            try:
                data = fut.result()
            except Exception as e:  # noqa: BLE001
                data = {"error": f"crash:{type(e).__name__}",
                        "fetched_url": url, "duration_ms": 0}
            _save(db, norm, url, data)
            done += 1
            if not data.get("error"):
                ok += 1
            if done % 50 == 0 or done == len(targets):
                rate = done / (time.monotonic() - t0) if time.monotonic() > t0 else 0
                logger.info("enrichment: %d/%d done, %d ok (%.1f/s)",
                            done, len(targets), ok, rate)
    report(db)
    db.conn.close()


def report(db: Database) -> None:
    cur = db.conn.cursor()
    total = cur.execute("SELECT COUNT(*) AS n FROM website_enrichment").fetchone()["n"]
    print(f"\n=== website_enrichment — {total} rows ===")
    print("  outcome                count")
    for row in cur.execute(
        """
        SELECT CASE
                 WHEN error IS NOT NULL THEN error
                 WHEN http_status = 200 THEN 'ok'
                 ELSE 'http_' || http_status
               END AS outcome,
               COUNT(*) AS n
          FROM website_enrichment
         GROUP BY outcome
         ORDER BY n DESC
        """
    ):
        print(f"  {row['outcome']:<20}   {row['n']:>5}")

    # How much new description coverage does enrichment add?
    n_idea_no_desc = cur.execute(
        """
        SELECT COUNT(*) AS n
          FROM company_ideas ci
         WHERE (ci.one_liner IS NULL OR ci.one_liner = '')
           AND (ci.long_description IS NULL OR ci.long_description = '')
        """
    ).fetchone()["n"]
    n_filled_by_enrich = cur.execute(
        """
        SELECT COUNT(DISTINCT ci.id) AS n
          FROM company_ideas ci
          JOIN website_enrichment we ON we.normalized_website = ci.normalized_website
         WHERE (ci.one_liner IS NULL OR ci.one_liner = '')
           AND (ci.long_description IS NULL OR ci.long_description = '')
           AND (we.meta_description IS NOT NULL OR we.hero_text IS NOT NULL)
        """
    ).fetchone()["n"]
    print(f"\n  company_ideas with no description: {n_idea_no_desc}")
    print(f"  of those now filled by enrichment: {n_filled_by_enrich} "
          f"({(n_filled_by_enrich / n_idea_no_desc * 100) if n_idea_no_desc else 0:.0f}%)")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--limit", type=int, default=None, help="Cap number of sites this run")
    p.add_argument("--refresh", type=int, default=None,
                   help="Re-fetch rows older than N days")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.report_only:
        db = Database(args.db)
        _ensure_schema(db)
        report(db)
        db.conn.close()
        return
    run(args.db, args.workers, args.limit, args.timeout, args.refresh)


if __name__ == "__main__":
    main()
