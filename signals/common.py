"""
Shared helpers for the signals pipeline:

- SIGNAL_TYPES + per-type weighting
- Entity-key normalization (domain, name, github handle)
- Fuzzy name match
- Composite scoring (type weights × multiplicity bonus × recency decay)

The adapters write raw signal rows and the joiner reads them back — these
functions are the contract between the two sides.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Signal catalogue + weights
# ---------------------------------------------------------------------------

# Weights used by the joiner. Higher = stronger evidence a stealth company
# actually exists. These calibrate against each other (not absolute) — the
# multi-signal bonus below is what makes a 2+ signal entity score far above
# a single-signal one, which is the whole point.
SIGNAL_WEIGHTS = {
    "handelsregister_new": 30.0,  # legal entity exists — strongest fact
    "handelsregister_holding_formed": 25.0,  # DACH pre-launch: Beteiligungs/Verwaltungs/i.G. — leading indicator
    "founder_match": 35.0,  # LinkedIn ↔ officer exact match — strongest person link
    "linkedin_stealth": 22.0,  # person claims stealth right now
    "linkedin_transition": 28.0,  # person RECENTLY went stealth (delta)
    "crt_domain_new": 18.0,  # product has a website
    "github_org_new": 18.0,  # code is being written
    "news_mention": 15.0,  # press picked them up
    "cohort_announcement": 30.0,  # accelerator vetted — strong signal (YC/Speedrun/EWOR/SPRIND/EF)
    "job_posting_stealth": 12.0,  # 1-5p stealth company hiring
}

# Everything the joiner should iterate over
SIGNAL_TYPES = tuple(SIGNAL_WEIGHTS.keys())


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_GENERIC_HOSTS = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "github.com",
        "www.github.com",
        "twitter.com",
        "x.com",
        "www.twitter.com",
        "crunchbase.com",
        "www.crunchbase.com",
        "news.ycombinator.com",
        "ycombinator.com",
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "medium.com",
        "www.medium.com",
        "handelsregister.de",
        "www.handelsregister.de",
        "unternehmensregister.de",
        "www.unternehmensregister.de",
        "northdata.de",
        "www.northdata.de",
        "producthunt.com",
        "www.producthunt.com",
        "google.com",
        "www.google.com",
        "facebook.com",
        "www.facebook.com",
        "youtube.com",
        "www.youtube.com",
    }
)

# Legal-form noise that must come OFF entity_name before fuzzy match
_LEGAL_FORMS = re.compile(
    r"\b(gmbh|ug|ag|kg|mbh|se|ohg|gbr|limited|ltd|inc|llc|co|corp|corporation|"
    r"company|holding|holdings|group|labs|lab|technologies|technology|tech|"
    r"solutions|systems|ventures|capital|partners|associates|services|"
    r"international|global|haftungsbeschränkt|haftungsbeschrankt|i\.g\.|i\. g\.)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RUN = re.compile(r"[^\w\s]+")
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_domain(url_or_host: Optional[str]) -> Optional[str]:
    """lower-case, strip scheme / www. / trailing slash, drop generic hosts.

    Returns None for generic social/wiki hosts and non-web schemes (mailto,
    tel, sms) — those shouldn't be a join key.
    """
    if not url_or_host:
        return None
    s = url_or_host.strip().lower()
    if not s:
        return None
    # Explicitly reject non-web schemes before adding our own
    for bad in ("mailto:", "tel:", "sms:", "javascript:", "data:"):
        if s.startswith(bad):
            return None
    # Add scheme if bare host
    if "://" not in s:
        s = "http://" + s
    try:
        host = urlparse(s).hostname or ""
    except ValueError:
        return None
    if not host:
        return None
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    if host in _GENERIC_HOSTS:
        return None
    # Must look like a real domain (has a dot, no spaces)
    if "." not in host or " " in host:
        return None
    return host


def normalize_name(name: Optional[str]) -> Optional[str]:
    """Lowercase, strip accents, drop legal forms + punctuation + runs of ws.

    Used as a fuzzy join key — not for display.
    """
    if not name:
        return None
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = _LEGAL_FORMS.sub(" ", s)
    s = _PUNCT_RUN.sub(" ", s)
    s = _WHITESPACE_RUN.sub(" ", s).strip()
    return s or None


def normalize_github(handle_or_url: Optional[str]) -> Optional[str]:
    """Return the github org/user slug, lower-case."""
    if not handle_or_url:
        return None
    s = handle_or_url.strip().lower()
    if not s:
        return None
    # strip URL prefix if present
    s = re.sub(r"^https?://(www\.)?github\.com/", "", s)
    # First path segment only (owner), drop /repo if present
    s = s.split("/", 1)[0]
    s = s.strip("@/")
    if not s or " " in s:
        return None
    return s


# Tokens shorter than this are ignored in similarity (noise like "ai", "it")
_MIN_TOKEN_LEN = 3

# Common tokens that don't carry identity info even after legal_forms are stripped.
# Includes cities, generic business words, and category labels that will match
# across many unrelated entities.
_NAME_STOPWORDS = frozenset(
    {
        "and",
        "the",
        "for",
        "with",
        "a",
        "an",
        "ai",
        "ml",
        "io",
        "app",
        "web",
        "und",
        "der",
        "die",
        "das",
        # cities (false-match magnets in the news corpus)
        "berlin",
        "münchen",
        "muenchen",
        "hamburg",
        "köln",
        "koeln",
        "frankfurt",
        "stuttgart",
        "düsseldorf",
        "duesseldorf",
        "leipzig",
        "wien",
        "zürich",
        "zurich",
        "london",
        "paris",
        "amsterdam",
        # generic business-word nouns that routinely appear in names
        "startup",
        "startups",
        "company",
        "firm",
        "project",
        "team",
        "studio",
        "studios",
        "works",
    }
)


def _tokens(s: str) -> set:
    return {t for t in s.split() if len(t) >= _MIN_TOKEN_LEN and t not in _NAME_STOPWORDS}


def name_similarity(a: Optional[str], b: Optional[str], *, strict: bool = False) -> float:
    """Hybrid name similarity (0.0 … 1.0).

    Combines two measures and takes the max:

    1. **Jaccard** — classic |A ∩ B| / |A ∪ B|, good for similar-length names.
    2. **Overlap coefficient on the shorter side** — |A ∩ B| / min(|A|, |B|).
       Gives 1.0 when the shorter name's tokens are a subset of the longer
       one. Needed to catch news-extracted "FooCo" matching
       Handelsregister's "FooCo Automation GmbH". Requires ≥ 2 shared
       meaningful tokens so short "ai"/"the" junk doesn't false-match.

    `strict=True` disallows the single-distinctive-token path entirely —
    the overlap branch requires ≥ 2 shared meaningful tokens. Used by the
    joiner when bridging ACROSS signal types (so e.g. "M.C - Ecosystem GmbH"
    cannot merge with "Bozorg Kalantar – Open Ecosystem" on the shared token
    "ecosystem" alone).
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _tokens(na), _tokens(nb)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    jaccard = len(inter) / len(ta | tb)
    overlap = 0.0
    # Overlap-coefficient branch catches "FooCo" ⊂ "FooCo Automation GmbH".
    # Risk: a short extracted name ("Berlin") false-matching dozens of
    # "Berlin Robotics"-style legal names. Guard: require EITHER 2+ shared
    # tokens, OR a single shared token that is distinctive (≥ 6 chars).
    # City/business noise is already filtered via stopwords. In strict
    # mode the single-token path is disabled.
    distinctive_single = not strict and len(inter) == 1 and max(len(t) for t in inter) >= 6
    if len(inter) >= 2 or distinctive_single:
        overlap = len(inter) / min(len(ta), len(tb))
    return max(jaccard, overlap)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def recency_multiplier(last_signal_at: Optional[str], now: Optional[datetime] = None) -> float:
    """Older signals matter less. Rough half-life-ish decay."""
    dt = _parse_dt(last_signal_at)
    if dt is None:
        return 0.5
    if now is None:
        now = datetime.now(timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400.0)
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.85
    if days <= 180:
        return 0.65
    if days <= 365:
        return 0.45
    return 0.25


def composite_score(
    signal_types: Iterable[str], last_signal_at: Optional[str], now: Optional[datetime] = None
) -> float:
    """0 – 100 composite. The multi-signal bonus is the key insight: a single
    signal in isolation scores low, two signals cross-confirming each other
    score high, three near max.
    """
    types = list({t for t in signal_types if t})
    if not types:
        return 0.0

    base = sum(SIGNAL_WEIGHTS.get(t, 10.0) for t in types)
    # Multi-signal bonus: linear up to 3 types, then flat
    distinct = len(types)
    if distinct >= 2:
        base += 15.0
    if distinct >= 3:
        base += 15.0
    if distinct >= 4:
        base += 10.0

    base *= recency_multiplier(last_signal_at, now=now)
    return float(min(100.0, round(base, 2)))


# ---------------------------------------------------------------------------
# Signal row helpers
# ---------------------------------------------------------------------------


def ensure_json(x) -> Optional[str]:
    """Serialize a list/dict for storage; None stays None."""
    if x is None:
        return None
    if isinstance(x, str):
        return x
    try:
        return json.dumps(x, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
