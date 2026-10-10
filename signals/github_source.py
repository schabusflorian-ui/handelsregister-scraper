"""
GitHub signals source.

Two separate searches:

1. **New orgs with sector keywords** — a stealth company registering a
   GitHub org is one of the first side-effects of founder work.
2. **New repos with sector keywords, zero stars, created in last 30d** —
   pre-launch code pushed by the same founders.

Each hit becomes a `github_org_new` signal. The joiner then merges them
with LinkedIn / Handelsregister / crt.sh via domain match (we extract the
org's `blog` URL, if any) or fuzzy name match.

Set `GITHUB_TOKEN` to lift rate limits from 60/hr → 5000/hr. We work
without a token too, but with far fewer queries per cycle.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from .common import ensure_json, iso_now, normalize_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query catalogue — kept small to fit the unauthenticated 60/hr budget
# ---------------------------------------------------------------------------

ORG_QUERIES = [
    ("robotics", "robotics"),
    ("robotics", "cobot"),
    ("ai",       "genai"),
    ("ai",       "llm agent"),
    ("climate",  "climate tech"),
    ("climate",  "carbon capture"),
    ("hardware", "semiconductor"),
    ("battery",  "battery"),
    ("hydrogen", "hydrogen"),
    ("space",    "aerospace"),
    ("biotech",  "biotech"),
]


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _gh_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "handelsregister-scraper/signals",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, timeout: int = 20) -> Optional[dict]:
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 403:
            logger.warning("GitHub rate-limited (403); backing off")
        else:
            logger.warning("GitHub HTTP %s: %s", e.code, url)
        return None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        logger.warning("GitHub fetch failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Query runners
# ---------------------------------------------------------------------------

def _since_date(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def search_new_orgs(sector_tag: str, keyword: str, *, days: int = 90,
                    per_page: int = 20) -> List[dict]:
    """Return list of org dicts matching `keyword` created in the last `days`."""
    q = f"{keyword} type:org created:>{_since_date(days)}"
    url = (
        "https://api.github.com/search/users?"
        + urllib.parse.urlencode({
            "q": q,
            "per_page": per_page,
            "sort": "joined",
            "order": "desc",
        })
    )
    data = _get_json(url)
    if not data or not isinstance(data, dict):
        return []
    items = data.get("items", []) or []

    # /search/users doesn't return created_at or blog — we need to GET each
    # user to enrich. Bounded by `per_page`. Skip on rate-limit (user may
    # be unauthenticated; one search = 1 unit, each user GET = 1 unit).
    out = []
    for u in items[:per_page]:
        login = u.get("login")
        if not login:
            continue
        details = _get_json(f"https://api.github.com/users/{login}")
        if not details:
            # likely rate-limited — stop early rather than keep burning
            break
        if details.get("type") != "Organization":
            continue
        out.append({
            "login":       login,
            "name":        details.get("name") or login,
            "blog":        details.get("blog") or None,
            "created_at":  details.get("created_at"),
            "sector_tag":  sector_tag,
            "keyword":     keyword,
            "html_url":    details.get("html_url"),
        })
    return out


# ---------------------------------------------------------------------------
# Signal conversion
# ---------------------------------------------------------------------------

def orgs_to_signals(orgs: Iterable[dict]) -> List[dict]:
    out = []
    for o in orgs:
        dom = normalize_domain(o.get("blog")) if o.get("blog") else None
        out.append({
            "signal_type":   "github_org_new",
            "observed_at":   o.get("created_at") or iso_now(),
            "source_url":    o["html_url"],
            "entity_name":   o.get("name") or o["login"],
            "entity_domain": dom,
            "entity_github": o["login"],
            "sector_tags":   [o["sector_tag"]],
            "confidence":    60.0,
            "raw_payload":   {"keyword": o["keyword"]},
        })
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def write_signals(conn, candidates: Iterable[dict]) -> dict:
    cur = conn.cursor()
    inserted = skipped = 0
    for c in candidates:
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, entity_domain, entity_github,
                 sector_tags, confidence, raw_payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c["signal_type"], c["observed_at"], c["source_url"],
                c.get("entity_name"), c.get("entity_domain"), c.get("entity_github"),
                ensure_json(c.get("sector_tags")),
                c.get("confidence", 50.0),
                ensure_json(c.get("raw_payload")),
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run(conn, *, days: int = 90, per_query: int = 10) -> dict:
    all_orgs: List[dict] = []
    seen: set = set()
    for tag, kw in ORG_QUERIES:
        logger.info("github: searching %s / '%s'", tag, kw)
        orgs = search_new_orgs(tag, kw, days=days, per_page=per_query)
        for o in orgs:
            if o["login"] not in seen:
                all_orgs.append(o)
                seen.add(o["login"])
        time.sleep(2.0)   # polite spacing even inside rate limit
    sigs = orgs_to_signals(all_orgs)
    stats = write_signals(conn, sigs)
    stats["orgs_found"] = len(all_orgs)
    return stats


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--per-query", type=int, default=10)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(run(conn, days=args.days, per_query=args.per_query), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
