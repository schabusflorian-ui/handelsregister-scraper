"""
Ex-employer watchlist — DACH-focused.

Harmonic's explicit methodology: "cohort-based refresh for priority
organizations". They don't scan the whole internet; they watch
alumni of a known-high-signal employer list. We adopt the same
approach, tuned for DACH scouting:

  Tier 1 — Elite global AI / foundational-tech alumni. Any departure
           here with a stealth bio is strong signal regardless of
           geography (DACH founders often come from these shops).
  Tier 2 — DACH scaleups + global peers. Departure + stealth bio is
           strong, especially when the person's geo stays DACH.
  Tier 3 — Larger "incubator" employers whose alumni frequently
           found. Weaker alone — needs cross-source corroboration.

The joiner reads this during founder_confidence computation and
adds a `watchlist_tier` boost. We also expose `watchlist_tier` on
stealth_entities so UI can filter to "ex-Tier-1-only" shortlists.

Catalog is intentionally hand-curated — algorithmic "signal company"
detection is a research problem; a manually maintained ~100-entry
list is more useful day-one.
"""

from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Catalog — grouped by tier
# ---------------------------------------------------------------------------

# Tier 1 — Global elite (AI research labs, hyperscaler frontier teams,
# iconic infrastructure/dev-tools companies). High founding rate per alum.
TIER_1_EMPLOYERS = {
    # AI research labs
    "openai",
    "anthropic",
    "google deepmind",
    "deepmind",
    "mistral",
    "mistral ai",
    "cohere",
    "inflection ai",
    "character ai",
    "runway",
    "runway ml",
    "pika",
    "pika labs",
    "midjourney",
    "stability ai",
    "adept",
    "adept ai",
    "aleph alpha",  # Heidelberg — the one DACH AI lab in Tier 1
    "xai",
    "x.ai",  # Musk's xAI
    # Elite infra / dev-tools
    "stripe",
    "databricks",
    "snowflake",
    "mongodb",
    "hashicorp",
    "vercel",
    "supabase",
    "neon",
    "neon tech",
    "figma",
    "linear",
    "notion",
    "palantir",
    "scale ai",
    "cursor",
    "anysphere",
    # Frontier hardware / autonomy
    "spacex",
    "waymo",
    "tesla",
    "tesla autopilot",
    "boston dynamics",
    "neuralink",
    # Big Tech frontier research
    "google x",
    "google research",
    "meta ai",
    "fair",
    "microsoft research",
    "apple ai",
}

# Tier 2 — DACH scaleups + global peers where many alumni found.
# Strong signal when combined with DACH geo.
TIER_2_EMPLOYERS = {
    # DACH scaleups (fintech, SaaS, mobility, HR tech)
    "deepl",  # Köln — frequent spinoff source
    "n26",  # Berlin fintech
    "trade republic",  # Berlin fintech
    "scalable capital",  # Munich fintech
    "celonis",  # Munich process mining
    "personio",  # Munich HR SaaS
    "forto",  # Berlin logistics
    "sennder",  # Berlin logistics
    "flink",  # Berlin quick commerce
    "gorillas",  # Berlin quick commerce (defunct — alumni everywhere)
    "wefox",  # Berlin insurtech
    "mambu",  # Berlin banking SaaS
    "contentful",  # Berlin headless CMS
    "spotify",  # Stockholm but strong DACH presence
    "klarna",  # Stockholm but DACH alumni common
    "revolut",  # London but DACH alumni common
    "wise",  # London fintech
    # Global peers whose alumni found often
    "shopify",
    "square",
    "block",
    "robinhood",
    "coinbase",
    "airbnb",
    "doordash",
    "instacart",
    "uber",
    "lyft",
    "canva",
    "miro",
    "atlassian",
    "github",
    "cloudflare",
    "datadog",
    "pagerduty",
    # German enterprise with notable spin-outs
    "sap",
    "sap labs",
    "sap signavio",
    "siemens",
    "bosch",
    "zalando",
    "delivery hero",
    # Research-heavy employers DACH founders come from
    "max planck",
    "max planck institute",
    "fraunhofer",
    "eth zürich",
    "eth zurich",
    "epfl",
    "tum",
    "technische universität münchen",
    "tu münchen",
}

# Tier 3 — Baseline watchlist. Weak signal alone; needs other evidence.
TIER_3_EMPLOYERS = {
    # Hyperscalers (lots of alumni, moderate founding rate per capita)
    "google",
    "alphabet",
    "meta",
    "facebook",
    "apple",
    "amazon",
    "microsoft",
    "nvidia",
    "intel",
    "qualcomm",
    "amd",
    # Legacy consulting / IB (many "I'm leaving to start X" founders)
    "mckinsey",
    "bain",
    "bcg",
    "boston consulting group",
    "goldman sachs",
    "jp morgan",
    "morgan stanley",
    # Legacy German corporates
    "allianz",
    "deutsche bank",
    "deutsche telekom",
    "volkswagen",
    "bmw",
    "mercedes-benz",
    "daimler",
    "basf",
    "bayer",
    "merck",
    # Rocket Internet alumni (prolific founder source)
    "rocket internet",
    "zalando",
    "home24",
    "foodpanda",
    # Earlier wave DACH exits
    "soundcloud",
    "researchgate",
    "kuaishou",
    "mytaxi",
    "freenet",
}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

# Pre-compile a single lowercase lookup — the hot path is per-signal in the
# joiner's _compute_founder_confidence, so avoid per-call normalization.
_ALL_EMPLOYERS: dict = {}
for name in TIER_1_EMPLOYERS:
    _ALL_EMPLOYERS[name.lower()] = 1
for name in TIER_2_EMPLOYERS:
    _ALL_EMPLOYERS.setdefault(name.lower(), 2)
for name in TIER_3_EMPLOYERS:
    _ALL_EMPLOYERS.setdefault(name.lower(), 3)

# Normalization applied once at lookup time. Keep legal-form stripping
# modest — "SAP SE" and "SAP AG" should both resolve to "sap".
_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(gmbh|ug|ag|se|kg|mbh|ohg|gbr|limited|ltd|inc|llc|corp|corporation|holding|group|labs)\b\.?",
    re.I,
)


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = _LEGAL_SUFFIX_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def watchlist_tier(employer: Optional[str]) -> Optional[int]:
    """Return 1, 2, 3, or None for a given employer name.

    Matches on normalized exact name AND on substring (e.g. 'SAP Signavio'
    returns Tier 2 because 'sap signavio' is listed). Preserves the
    highest tier found (lowest number).
    """
    if not employer:
        return None
    n = _norm(employer)
    if not n:
        return None
    # Exact match first
    if n in _ALL_EMPLOYERS:
        return _ALL_EMPLOYERS[n]
    # Substring fallback — needed because LinkedIn snippets often include
    # extra context: "Software Engineer @ Google DeepMind London" has
    # "deepmind" as a substring. Only match when candidate is a token
    # boundary, not a random substring.
    best = None
    for k, tier in _ALL_EMPLOYERS.items():
        if re.search(r"\b" + re.escape(k) + r"\b", n):
            if best is None or tier < best:
                best = tier
    return best


# Boost added to founder_confidence per tier (points, 0-100 scale)
TIER_BOOSTS = {1: 20, 2: 12, 3: 6}


def tier_boost(tier: Optional[int]) -> float:
    return TIER_BOOSTS.get(tier, 0.0) if tier else 0.0
