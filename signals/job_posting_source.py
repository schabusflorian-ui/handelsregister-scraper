"""
job_posting_stealth source — mines HN "Ask HN: Who is hiring?" threads
for stealth-startup / founding-engineer / 0→1 postings.

Harmonic + Evertrace both call out job postings for unnamed early-stage
companies as a leading signal. HN's monthly whoishiring threads are the
single densest free source for exactly this: thousands of hiring posts
per month, free-text, consistently formatted ("Company | Role | Loc").

Flow:
    1. Query HN Algolia API for the most recent whoishiring threads
    2. Fetch each thread's full comment tree
    3. Filter comments to those matching stealth/founding-engineer patterns
    4. Emit one job_posting_stealth signal per matching comment

Idempotent via source_url = https://news.ycombinator.com/item?id={comment_id}
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Iterable, List, Optional

from .common import ensure_json, iso_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pattern catalog — what a "stealth startup" job posting looks like
# ---------------------------------------------------------------------------

# Any of these in the first ~500 chars of a comment = probable stealth-startup posting.
_STEALTH_JOB_RX = re.compile(
    r"\b("
    r"stealth(\s+(startup|mode|company))?|"
    r"founding (engineer|team|member|designer|product|pm|researcher|scientist)|"
    r"founder'?s? associate|chief of staff to (ceo|founder)|"
    r"first (engineer|hire|technical hire|employee)|"
    r"employee #?[1-5]\b|first ten\b|first five\b|first few hires|"
    r"0\s*(?:to|→|->)\s*1|zero to one|day one engineer|"
    r"ground floor|from scratch|green ?field|"
    r"pre[- ]seed|pre[- ]launch|pre[- ]product|early[- ]stage"
    r")\b",
    re.I,
)

# Exclude posts that are clearly from established companies even though
# they might mention "founding" (as in "founding team of 100").
_EXCLUDE_JOB_RX = re.compile(
    r"\b("
    r"series\s+[cdef]\b|post[- ]ipo|publicly traded|nasdaq|nyse|"
    r"500\+? engineers|1000\+? employees|unicorn|\$\d+b valuation|"
    r"headcount of \d{3,}"
    r")\b",
    re.I,
)

# DACH geography hints in the posting body
_DACH_LOCATION_RX = re.compile(
    r"\b(berlin|münchen|munich|hamburg|frankfurt|köln|cologne|stuttgart|"
    r"düsseldorf|dusseldorf|leipzig|wien|vienna|zürich|zurich|basel|"
    r"germany|deutschland|austria|österreich|switzerland|schweiz|dach)\b",
    re.I,
)

# Rough company-name extraction from HN comment first line.
# Typical format: "CompanyName | Role | Loc | remote/onsite ..."
_HN_COMPANY_RX = re.compile(
    r"^\s*(?:<p>)?\s*([A-Za-z][\w &.\-+]{1,60})\s*(?:\(?YC\s*[WSF]?\d{2}\)?)?\s*[|]",
    re.M,
)


# ---------------------------------------------------------------------------
# Algolia HN API
# ---------------------------------------------------------------------------

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ALGOLIA_ITEM = "https://hn.algolia.com/api/v1/items/{}"


def _get_json(url: str, timeout: int = 25) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "handelsregister-scraper/signals (hn-whoishiring)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        logger.warning("HN Algolia HTTP %s: %s", e.code, url)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        logger.warning("HN Algolia fetch failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.warning("HN Algolia unexpected: %s", e)
    return None


def _recent_whoishiring_threads(limit: int = 3) -> List[dict]:
    """Return the N most recent 'Ask HN: Who is hiring?' story items."""
    q = urllib.parse.quote("Ask HN: Who is hiring?")
    url = f"{ALGOLIA_SEARCH}?query={q}&tags=story,author_whoishiring&hitsPerPage={limit}"
    data = _get_json(url)
    if not data:
        return []
    return (data.get("hits") or [])[:limit]


def _comments_in_item(item_id: int) -> Iterable[dict]:
    """Depth-first iteration over all comments in an HN story."""
    item = _get_json(ALGOLIA_ITEM.format(item_id))
    if not item:
        return
    stack = list(item.get("children") or [])
    while stack:
        node = stack.pop()
        yield node
        kids = node.get("children") or []
        stack.extend(kids)


# ---------------------------------------------------------------------------
# Parse one HN comment → signal
# ---------------------------------------------------------------------------


def _extract_company_name(text: str) -> Optional[str]:
    """Heuristic — HN hiring comments start with 'CompanyName | Role | ...'
    about 70% of the time. When it doesn't match, we return None rather
    than guess (and the comment contributes an un-named signal — still
    useful for the person-match graph if someone replies with their LI)."""

    # unescape basic HTML entities
    text = text.replace("&amp;", "&").replace("&#x2F;", "/").replace("&quot;", '"')
    m = _HN_COMPANY_RX.search(text[:500])
    if m:
        return m.group(1).strip()[:80]
    return None


def _parse_comment(comment: dict) -> Optional[dict]:
    """One comment → optional job_posting_stealth signal dict."""
    text = comment.get("text") or ""
    if not text:
        return None
    if not _STEALTH_JOB_RX.search(text[:1500]):
        return None
    if _EXCLUDE_JOB_RX.search(text[:1500]):
        return None
    company = _extract_company_name(text) or "(unnamed stealth company)"
    is_dach = bool(_DACH_LOCATION_RX.search(text[:1500]))
    comment_id = comment.get("id")
    return {
        "signal_type": "job_posting_stealth",
        "observed_at": comment.get("created_at") or iso_now(),
        "source_url": f"https://news.ycombinator.com/item?id={comment_id}",
        "entity_name": company,
        "sector_tags": ["hiring_stealth"] + (["dach"] if is_dach else []),
        "geo_region": "DE" if is_dach else None,
        "confidence": 55.0 if is_dach else 40.0,
        "raw_payload": {
            "hn_comment_id": comment_id,
            "excerpt": text[:400],
        },
    }


# ---------------------------------------------------------------------------
# Signal write
# ---------------------------------------------------------------------------


def write_signals(conn, candidates: Iterable[dict]) -> dict:
    cur = conn.cursor()
    inserted = skipped = 0
    for c in candidates:
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, sector_tags, geo_region, confidence, raw_payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c["signal_type"],
                c["observed_at"],
                c["source_url"],
                c.get("entity_name"),
                ensure_json(c.get("sector_tags")),
                c.get("geo_region"),
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
# Driver
# ---------------------------------------------------------------------------


def run(conn, *, thread_limit: int = 3) -> dict:
    threads = _recent_whoishiring_threads(limit=thread_limit)
    if not threads:
        logger.warning("job_posting source: no whoishiring threads returned by Algolia")
        return {"threads": 0, "candidates": 0, "inserted": 0}

    logger.info("job_posting source: scanning %d thread(s)", len(threads))
    all_sigs: List[dict] = []
    for t in threads:
        tid = t.get("objectID")
        if not tid:
            continue
        try:
            tid_int = int(tid)
        except (ValueError, TypeError):
            continue
        logger.info("  thread %s (%s)", tid, t.get("title", "?")[:60])
        for c in _comments_in_item(tid_int):
            sig = _parse_comment(c)
            if sig:
                all_sigs.append(sig)
        time.sleep(1.0)  # polite spacing between threads

    stats = write_signals(conn, all_sigs)
    stats["threads"] = len(threads)
    stats["candidates"] = len(all_sigs)
    return stats


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--threads", type=int, default=3)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(run(conn, thread_limit=args.threads), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
