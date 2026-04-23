"""
Show HN scraper — indie/solo-founder launches via the Algolia HN Search API.

Purpose:
    Capture every "Show HN" post as one CompanyIdea row so the existing
    idea-discovery / clustering pipeline picks it up unchanged. Show HN is
    heavy on solo builders and bootstrappers, which the VC-portfolio
    scrapers systematically miss.

Mapping Algolia hit -> CompanyIdea:
    program         = "Show HN"
    company         = name extracted from the "Show HN: <Name> – ..." prefix
    one_liner       = descriptor after the title separator (or None)
    long_description= hit.story_text (self-post body), HTML-stripped
    tags            = []  (Show HN has no structured tags; downstream
                           enrichment is expected to fill mechanism/sector)
    company_website = hit.url (external product link; None for self-posts)
    batch           = YYYY-MM of created_at (cohort-style month bucket)
    source_url      = https://news.ycombinator.com/item?id=<objectID>
    raw             = points, num_comments, author, created_at_i, objectID

API:
    https://hn.algolia.com/api/v1/search_by_date?tags=show_hn
    No auth. Algolia caps any single query at ~1000 hits, so `fetch_since`
    walks backwards in time using created_at_i<oldest as an exclusive
    upper bound until it exhausts the window.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sources.accelerator_scraper import (
    AcceleratorScraper,
    CompanyIdea,
    OnRecord,
)

logger = logging.getLogger(__name__)


class ShowHNScraper(AcceleratorScraper):
    program_name = "Show HN"
    crawl_delay = 1.0

    ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
    HN_ITEM_BASE = "https://news.ycombinator.com/item?id="
    HITS_PER_PAGE = 1000

    # Strips "Show HN:", "Show HN -", "Show HN –", "Show HN —" (case-insensitive).
    _PREFIX_RE = re.compile(r"^\s*Show\s*HN\s*[:\-–—]\s*", re.I)
    # Title separator between name and descriptor. Must be surrounded by whitespace
    # so hyphens inside compound names (e.g. "Side-Project") aren't split.
    _SEP_RE = re.compile(r"\s+[–—\-]\s+|\s*:\s+|\s+\|\s+")
    _TAG_RE = re.compile(r"<[^>]+>")

    def fetch_since(
        self,
        since_ts: int,
        on_record: OnRecord = None,
    ) -> List[CompanyIdea]:
        """Fetch every Show HN post with created_at_i > since_ts.

        Walks backwards in time because Algolia caps a single query at ~1000
        hits: each iteration uses the oldest created_at_i from the previous
        batch as an exclusive upper bound.
        """
        out: List[CompanyIdea] = []
        upper: Optional[int] = None
        prev_oldest: Optional[int] = None

        while True:
            filters = [f"created_at_i>{since_ts}"]
            if upper is not None:
                filters.append(f"created_at_i<{upper}")
            params = {
                "tags": "show_hn",
                "numericFilters": ",".join(filters),
                "hitsPerPage": self.HITS_PER_PAGE,
            }
            try:
                r = self.session.get(
                    self.ENDPOINT,
                    params=params,
                    headers=self.headers,
                    timeout=30,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Algolia fetch errored: %s", e)
                break
            if r.status_code != 200:
                logger.warning(
                    "Algolia -> %s (filters=%s)", r.status_code, filters
                )
                break

            data = r.json()
            hits = data.get("hits") or []
            if not hits:
                break

            for hit in hits:
                rec = self._to_idea(hit)
                if rec is None:
                    continue
                out.append(rec)
                if on_record:
                    on_record(rec)
                if self.max_records and len(out) >= self.max_records:
                    return out

            oldest = min(
                (h.get("created_at_i") or 0) for h in hits
            )
            logger.info(
                "Show HN: page got %d hits, oldest=%s, total=%d",
                len(hits),
                datetime.fromtimestamp(oldest, timezone.utc).isoformat()
                if oldest
                else "?",
                len(out),
            )

            if len(hits) < self.HITS_PER_PAGE:
                break
            if not oldest or oldest == prev_oldest:
                # Defensive: avoid infinite loop if the API stalls on a boundary.
                logger.warning("Show HN: stopping at ts=%s (no progress)", oldest)
                break
            prev_oldest = oldest
            upper = oldest
            self._delay()

        return out

    def scrape(
        self,
        on_record: OnRecord = None,
        since_ts: Optional[int] = None,
    ) -> List[CompanyIdea]:
        if since_ts is None:
            since_ts = int(
                (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
            )
        logger.info(
            "Show HN: fetching posts since %s (ts=%d)",
            datetime.fromtimestamp(since_ts, timezone.utc).isoformat(),
            since_ts,
        )
        return self.fetch_since(since_ts, on_record=on_record)

    def _to_idea(self, hit: dict) -> Optional[CompanyIdea]:
        title = self._clean(html.unescape(hit.get("title") or ""))
        if not title:
            return None
        company, one_liner = self._parse_title(title)

        story_text = hit.get("story_text") or None
        if story_text:
            story_text = self._clean(
                html.unescape(self._TAG_RE.sub(" ", story_text))
            )

        created_at_i = hit.get("created_at_i") or 0
        batch = (
            datetime.fromtimestamp(created_at_i, timezone.utc).strftime("%Y-%m")
            if created_at_i
            else None
        )

        external_url = hit.get("url")
        if external_url and "news.ycombinator.com" in external_url:
            # Self-post — no external product URL. Leave website null.
            external_url = None

        object_id = hit.get("objectID") or ""
        if not object_id:
            return None

        return CompanyIdea(
            program=self.program_name,
            company=company or "",
            one_liner=one_liner,
            long_description=story_text,
            tags=[],
            company_website=external_url,
            batch=batch,
            source_url=f"{self.HN_ITEM_BASE}{object_id}",
            raw={
                "objectID": object_id,
                "author": hit.get("author"),
                "points": hit.get("points"),
                "num_comments": hit.get("num_comments"),
                "created_at_i": created_at_i,
                "created_at": hit.get("created_at"),
                "original_title": title,
            },
        )

    def _parse_title(self, title: str) -> Tuple[Optional[str], Optional[str]]:
        """Split "Show HN: FooBar – a tool for X" into (company, one_liner)."""
        body = self._PREFIX_RE.sub("", title)
        if body == title:
            # No recognizable prefix — keep whole title as descriptor.
            return None, self._clean(title)
        m = self._SEP_RE.search(body)
        if not m:
            return self._clean(body), None
        return self._clean(body[: m.start()]), self._clean(body[m.end() :])


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=None)
    p.add_argument(
        "--since-days",
        type=int,
        default=30,
        help="Fetch posts from the last N days (default: 30)",
    )
    p.add_argument(
        "--since-ts",
        type=int,
        default=None,
        help="Unix timestamp override (takes precedence over --since-days)",
    )
    p.add_argument(
        "--jsonl",
        type=str,
        default=None,
        help="Append every record to this JSONL file as it's scraped",
    )
    args = p.parse_args()

    s = ShowHNScraper(max_records=args.max)

    jsonl_fh = open(args.jsonl, "a", buffering=1) if args.jsonl else None

    def _write(rec: CompanyIdea):
        d = asdict(rec)
        d["scraped_at"] = rec.scraped_at.isoformat()
        if jsonl_fh:
            jsonl_fh.write(
                json.dumps(d, default=str, ensure_ascii=False) + "\n"
            )

    since_ts = args.since_ts
    if since_ts is None:
        since_ts = int(
            (
                datetime.now(timezone.utc) - timedelta(days=args.since_days)
            ).timestamp()
        )

    try:
        records = s.scrape(on_record=_write, since_ts=since_ts)
    finally:
        if jsonl_fh:
            jsonl_fh.close()

    if not args.jsonl:
        for r in records[:5]:
            d = {
                k: v
                for k, v in asdict(r).items()
                if k != "scraped_at" and v not in (None, "", [], {})
            }
            print(json.dumps(d, default=str, indent=2))
    print(
        f"\n=> {len(records)} records from Show HN"
        + (f"; wrote to {args.jsonl}" if args.jsonl else "")
    )
