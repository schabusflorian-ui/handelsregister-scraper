"""
Reddit ideas scraper — indie / solo-builder launches from maker-focused subreddits.

Substitute for Indie Hackers (which is behind Cloudflare's JS challenge).
Covers the same demographic — people shipping side projects, microSaaS,
and bootstrap businesses — but via Reddit's public JSON endpoints.

Subreddits covered (configurable):
    r/SideProject     — general solo-builder launches, high volume
    r/indiehackers    — bootstrapper culture, frequent revenue posts
    r/microsaas       — small SaaS builders
    r/buildinpublic   — journey-sharing solo founders

Mapping Reddit post -> CompanyIdea:
    program         = "Reddit r/<sub>"   # one program per subreddit
    company         = parsed from [Tag] prefix if present, else ""
    one_liner       = cleaned post title
    long_description= selftext (markdown-ish), trimmed
    tags            = [link_flair_text]  # Reddit's own post flair when set
    company_website = url_overridden_by_dest  (external link if the post is a link post)
    batch           = YYYY-MM of created_utc
    source_url      = https://www.reddit.com<permalink>
    raw             = score, num_comments, author, id, created_utc, is_self

API notes:
    - Public JSON endpoints: https://www.reddit.com/r/<sub>/new.json?limit=100&after=<token>
    - Anonymous reads are allowed at low volume; use a descriptive User-Agent.
    - Server-side doesn't support time filters — we paginate newest-first and
      stop once we pass the incremental boundary (`stop_before_ts`).
    - Reddit caps any listing at ~1000 posts; deeper history isn't available
      via the public endpoint. For backfill, run once and then rely on daily
      incremental runs to accumulate history over time.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sources.accelerator_scraper import (
    AcceleratorScraper,
    CompanyIdea,
    OnRecord,
)

logger = logging.getLogger(__name__)


DEFAULT_SUBREDDITS: tuple[str, ...] = (
    "SideProject",
    "indiehackers",
    "microsaas",
    "buildinpublic",
)


class RedditIdeasScraper(AcceleratorScraper):
    """Scrapes new posts from one or more maker-focused subreddits.

    Each call to scrape() iterates the configured subreddits and yields
    one CompanyIdea per post. Use `stop_before_ts` (unix seconds) to
    resume incrementally — the scraper stops descending the /new feed
    once it hits a post older than that.
    """

    program_name = "Reddit"  # overridden per-row to "Reddit r/<sub>"
    crawl_delay = 2.5  # Reddit anon limit is ~10 req/min; stay well under.

    # Reddit expects a descriptive UA identifying the caller. See:
    # https://support.reddithelp.com/hc/en-us/articles/16160319875092
    DEFAULT_UA = (
        "handelsregister-idea-scraper/0.1 (contact: schabus.florian@gmail.com)"
    )

    # Common Reddit title-prefix conventions: "[Launch] Name - ...", "Show: X — ...",
    # or "I built <X>". Only the bracketed-tag case gives us a reliable name.
    _BRACKET_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$")
    _SEP_RE = re.compile(r"\s+[–—\-]\s+|\s*:\s+|\s+\|\s+")
    # Reddit selftext is markdown; strip HTML-ish remnants just in case.
    _TAG_RE = re.compile(r"<[^>]+>")
    # Self-posts embed the product URL in the body. First external http(s)
    # link that isn't Reddit/imgur is almost always the product.
    _URL_RE = re.compile(r"https?://[^\s\)\]<>]+")
    _URL_SKIP_DOMAINS = ("reddit.com", "redd.it", "redditmedia.com", "imgur.com")

    def __init__(
        self,
        subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
        delay_range: tuple = (2, 5),
        max_records: Optional[int] = None,
        user_agent: Optional[str] = None,
    ):
        super().__init__(delay_range=delay_range, max_records=max_records)
        self.subreddits = tuple(subreddits)
        ua = user_agent or self.DEFAULT_UA
        # Overwrite the base class's browser-ish UA — Reddit's stance is that
        # polite identifying UAs get better rate-limit treatment than
        # impersonating a browser.
        self.headers = {
            "User-Agent": ua,
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------

    def scrape(
        self,
        on_record: OnRecord = None,
        stop_before_ts: Optional[int] = None,
        max_pages_per_sub: int = 10,
    ) -> List[CompanyIdea]:
        """Fetch /new from each configured subreddit.

        Args:
            on_record: streaming callback, invoked per record as they're found.
            stop_before_ts: unix seconds; stop descending a subreddit once we
                see a post older than this (incremental resume).
            max_pages_per_sub: cap pages per subreddit (100 posts/page).
                Reddit's hard listing cap is ~1000 posts (~10 pages).
        """
        out: List[CompanyIdea] = []
        for sub in self.subreddits:
            logger.info("Reddit r/%s: fetching /new", sub)
            before = stop_before_ts
            sub_records = self._fetch_subreddit(
                sub,
                stop_before_ts=before,
                max_pages=max_pages_per_sub,
                on_record=on_record,
            )
            out.extend(sub_records)
            logger.info(
                "Reddit r/%s: %d records (total so far %d)",
                sub,
                len(sub_records),
                len(out),
            )
            if self.max_records and len(out) >= self.max_records:
                return out[: self.max_records]
        return out

    def _fetch_subreddit(
        self,
        sub: str,
        stop_before_ts: Optional[int],
        max_pages: int,
        on_record: OnRecord,
    ) -> List[CompanyIdea]:
        out: List[CompanyIdea] = []
        after: Optional[str] = None
        for page in range(max_pages):
            url = f"https://www.reddit.com/r/{sub}/new.json"
            params = {"limit": 100, "raw_json": 1}
            if after:
                params["after"] = after
            try:
                r = self.session.get(
                    url, params=params, headers=self.headers, timeout=30
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Reddit r/%s page %d errored: %s", sub, page, e)
                break
            if r.status_code == 429:
                # Rate-limited — back off once, then stop this subreddit.
                logger.warning(
                    "Reddit r/%s: 429 rate limit; sleeping 30s and stopping", sub
                )
                time.sleep(30)
                break
            if r.status_code != 200:
                logger.warning(
                    "Reddit r/%s page %d -> %s", sub, page, r.status_code
                )
                break

            data = r.json()
            children = (data.get("data") or {}).get("children") or []
            if not children:
                break

            exhausted = False
            for child in children:
                post = child.get("data") or {}
                # Filter out stickied mod posts and removed/deleted rows.
                if post.get("stickied"):
                    continue
                if post.get("removed_by_category"):
                    continue
                created_utc = int(post.get("created_utc") or 0)
                if stop_before_ts and created_utc and created_utc <= stop_before_ts:
                    # We've hit a post we've already seen — stop paging this sub.
                    exhausted = True
                    break
                rec = self._to_idea(sub, post)
                if rec is None:
                    continue
                out.append(rec)
                if on_record:
                    on_record(rec)
                if self.max_records and len(out) >= self.max_records:
                    return out

            after = (data.get("data") or {}).get("after")
            if exhausted or not after:
                break
            self._delay()
        return out

    # ------------------------------------------------------------------

    def _to_idea(self, sub: str, post: dict) -> Optional[CompanyIdea]:
        title_raw = post.get("title") or ""
        title = self._clean(html.unescape(title_raw))
        if not title:
            return None

        company, one_liner = self._parse_title(title)

        selftext = post.get("selftext") or None
        if selftext:
            selftext = self._clean(
                html.unescape(self._TAG_RE.sub(" ", selftext))
            )
            # Reddit selftext can be very long; cap at 4000 chars so DB stays
            # sane. Full text remains in the JSONL if anyone needs it.
            if selftext and len(selftext) > 4000:
                selftext = selftext[:4000] + "…"

        created_utc = int(post.get("created_utc") or 0)
        batch = (
            datetime.fromtimestamp(created_utc, timezone.utc).strftime("%Y-%m")
            if created_utc
            else None
        )

        # url_overridden_by_dest is the external link when it's a link post;
        # self-posts have this pointing back at reddit.com — filter those out.
        external_url = post.get("url_overridden_by_dest") or post.get("url")
        if external_url:
            lower = external_url.lower()
            if any(d in lower for d in self._URL_SKIP_DOMAINS):
                external_url = None

        # Self-posts (the common case on these subs) don't have an external
        # link URL — but the author usually pastes the product URL in the
        # body. Pull the first non-Reddit http(s) link as the product site.
        if not external_url and selftext:
            for m in self._URL_RE.finditer(selftext):
                u = m.group(0).rstrip(".,;:!?")
                low = u.lower()
                if any(d in low for d in self._URL_SKIP_DOMAINS):
                    continue
                external_url = u
                break

        permalink = post.get("permalink") or ""
        if not permalink:
            return None
        source_url = f"https://www.reddit.com{permalink}"

        tags: List[str] = []
        flair = post.get("link_flair_text")
        if flair:
            flair = self._clean(flair)
            if flair:
                tags.append(flair)

        return CompanyIdea(
            program=f"Reddit r/{sub}",
            company=company or "",
            one_liner=one_liner,
            long_description=selftext,
            tags=tags,
            company_website=external_url,
            batch=batch,
            source_url=source_url,
            raw={
                "id": post.get("id"),
                "author": post.get("author"),
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "created_utc": created_utc,
                "subreddit": sub,
                "is_self": post.get("is_self"),
                "link_flair_text": flair,
                "original_title": title,
            },
        )

    def _parse_title(self, title: str) -> tuple[Optional[str], Optional[str]]:
        """Best-effort split of a Reddit title into (company, one_liner).

        Reddit titles are very free-form. We only extract a name when it's
        unambiguous — bracketed-tag followed by "<Name> - <pitch>". Otherwise
        the full title becomes the one_liner and company stays empty.
        """
        m = self._BRACKET_TAG_RE.match(title)
        body = m.group(2).strip() if m else title
        sep = self._SEP_RE.search(body)
        if not sep:
            return None, self._clean(body)
        head = self._clean(body[: sep.start()])
        tail = self._clean(body[sep.end() :])
        # Guard: only treat the head as a company name if it looks like one
        # (1–4 tokens, no sentence punctuation). Otherwise keep the full body
        # as one_liner rather than splitting mid-sentence.
        if head and 1 <= len(head.split()) <= 4 and not re.search(r"[.!?]", head):
            return head, tail
        return None, self._clean(body)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser()
    p.add_argument(
        "--subs",
        nargs="+",
        default=list(DEFAULT_SUBREDDITS),
        help=f"Subreddits to scrape (default: {' '.join(DEFAULT_SUBREDDITS)})",
    )
    p.add_argument("--max", type=int, default=None)
    p.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Stop paging once posts are older than N days ago",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max pages per subreddit (100 posts/page; Reddit caps ~1000 total)",
    )
    p.add_argument("--jsonl", type=str, default=None)
    args = p.parse_args()

    s = RedditIdeasScraper(subreddits=args.subs, max_records=args.max)

    jsonl_fh = open(args.jsonl, "a", buffering=1) if args.jsonl else None

    def _write(rec: CompanyIdea):
        d = asdict(rec)
        d["scraped_at"] = rec.scraped_at.isoformat()
        if jsonl_fh:
            jsonl_fh.write(
                json.dumps(d, default=str, ensure_ascii=False) + "\n"
            )

    stop_before_ts = None
    if args.since_days:
        stop_before_ts = int(
            (
                datetime.now(timezone.utc) - timedelta(days=args.since_days)
            ).timestamp()
        )

    try:
        records = s.scrape(
            on_record=_write,
            stop_before_ts=stop_before_ts,
            max_pages_per_sub=args.max_pages,
        )
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
        f"\n=> {len(records)} records from Reddit ({', '.join(args.subs)})"
        + (f"; wrote to {args.jsonl}" if args.jsonl else "")
    )
