"""
Founder auto-tagger — computes structured tags from scraped profile data.

Extracts from headline + signals (which we reliably get from Google scraping):
  - founder_role:      founder, co-founder, cto, ceo, engineer, investor, other
  - ex_company_tier:   faang, unicorn, top_vc, consulting, other, None
  - ex_companies:      JSON list of parsed ex-company names
  - sector_tags:       JSON list: ai, fintech, climate, saas, health, etc.
  - stealth_strength:  confirmed, likely, weak
  - data_quality:      clean, partial, junk
  - geo_region:        berlin, munich, hamburg, zurich, vienna, etc.

All classification works from headline + signals text — no LinkedIn API needed.
"""

import json
import re
from typing import Dict, List, Optional, Tuple


# ============================================================
# Company tier classification
# ============================================================

FAANG = {
    "google", "alphabet", "meta", "facebook", "amazon", "apple",
    "microsoft", "netflix", "deepmind", "openai",
}

UNICORNS = {
    "n26", "revolut", "stripe", "klarna", "adyen", "checkout.com",
    "celonis", "personio", "flixbus", "wefox", "trade republic",
    "contentful", "mambu", "scalable capital", "wise", "transferwise",
    "delivery hero", "hellofresh", "zalando", "auto1", "about you",
    "spotify", "airbnb", "uber", "shopify", "databricks", "snowflake",
    "palantir", "figma", "canva", "notion",
}

TOP_VCS = {
    "sequoia", "a16z", "andreessen", "accel", "benchmark", "greylock",
    "index ventures", "atomico", "balderton", "lakestar", "earlybird",
    "hv capital", "project a", "cherry ventures", "point nine",
    "speedinvest", "creandum", "northzone", "y combinator", "yc",
    "techstars", "entrepreneur first", "ef", "antler",
}

TOP_CONSULTING = {
    "mckinsey", "bain", "bcg", "boston consulting", "roland berger",
    "oliver wyman", "deloitte", "pwc", "kpmg", "accenture",
}

# ============================================================
# Sector keywords
# ============================================================

SECTOR_PATTERNS = {
    "ai": r"\b(ai|artificial intelligence|machine learning|ml|deep learning|llm|gpt|neural|nlp|computer vision|generative ai|künstliche intelligenz)\b",
    "fintech": r"\b(fintech|banking|payment|neobank|insurtech|defi|crypto|blockchain|trading|wealth|lending)\b",
    "climate": r"\b(climate|cleantech|greentech|carbon|sustainability|renewable|energy transition|solar|wind energy|circular economy|co2)\b",
    "saas": r"\b(saas|b2b software|enterprise software|cloud platform|developer tool|devtool|api platform)\b",
    "health": r"\b(health|healthtech|medtech|biotech|pharma|digital health|mental health|femtech)\b",
    "proptech": r"\b(proptech|real estate|immobilien|construction tech|contech)\b",
    "mobility": r"\b(mobility|autonomous|self-driving|ev |electric vehicle|logistics|fleet)\b",
    "security": r"\b(cybersecurity|cyber|infosec|security platform|identity|authentication)\b",
    "foodtech": r"\b(foodtech|food tech|agritech|agtech|alternative protein)\b",
    "edtech": r"\b(edtech|education|learning platform|e-learning|upskilling)\b",
    "robotics": r"\b(robot|robotics|robotik|automation|industrial automation|cobot)\b",
    "hr": r"\b(hr tech|hrtech|recruiting|talent|people analytics|workforce)\b",
}

# ============================================================
# Geo normalization
# ============================================================

GEO_MAP = {
    "berlin": ["berlin"],
    "munich": ["münchen", "munich", "bavaria", "bayern"],
    "hamburg": ["hamburg"],
    "frankfurt": ["frankfurt", "hesse", "hessen"],
    "nrw": ["düsseldorf", "köln", "cologne", "dortmund", "essen", "bonn", "nordrhein", "nrw"],
    "stuttgart": ["stuttgart", "württemberg", "baden-württemberg"],
    "zurich": ["zürich", "zurich", "zug"],
    "vienna": ["wien", "vienna"],
    "switzerland": ["schweiz", "swiss", "switzerland", "bern", "genf", "geneva", "basel", "lausanne"],
    "austria": ["österreich", "austria", "graz", "linz", "salzburg", "innsbruck"],
}


# ============================================================
# Classification functions
# ============================================================


def classify_geo(location: Optional[str]) -> Optional[str]:
    """Normalize location to a region tag."""
    if not location:
        return None
    loc_lower = location.lower()
    for region, keywords in GEO_MAP.items():
        for kw in keywords:
            if kw in loc_lower:
                return region
    if "deutschland" in loc_lower or "germany" in loc_lower:
        return "germany_other"
    return "other"


def classify_role(headline: Optional[str], signals: list) -> str:
    """Determine the founder's role from headline + signals."""
    if not headline:
        signal_set = {s.lower() for s in signals}
        if "co-founder" in signal_set or "mitgründer" in signal_set:
            return "co-founder"
        if "founder" in signal_set or "gründer" in signal_set:
            return "founder"
        if "ceo" in signal_set:
            return "ceo"
        return "other"

    h = headline.lower()
    if re.search(r"\b(co-?founder|mitgründer|co-?gründer)\b", h):
        return "co-founder"
    if re.search(r"\b(cto|chief technology)\b", h):
        return "cto"
    if re.search(r"\b(ceo|chief executive|geschäftsführer)\b", h):
        return "ceo"
    if re.search(r"\b(founder|gründer|founded|founding)\b", h):
        return "founder"
    if re.search(r"\b(angel investor|investor|vc |venture)\b", h):
        return "investor"
    if re.search(r"\b(engineer|developer|architect|coder)\b", h):
        return "engineer"
    if re.search(r"\b(director|vp |head of|manager|lead)\b", h):
        return "executive"

    signal_set = {s.lower() for s in signals}
    if "founder" in signal_set or "gründer" in signal_set:
        return "founder"
    return "other"


def classify_ex_companies(headline: Optional[str], signals: list) -> Tuple[Optional[List[str]], Optional[str]]:
    """Extract ex-company names and classify tier."""
    companies_found = []
    tier = None

    for signal in signals:
        s = signal.lower()
        if s.startswith("ex-"):
            companies_found.append(signal[3:])

    if headline:
        for match in re.finditer(
            r"(?:ex[- ])(\w[\w& .]*?)(?:\s*[|,\-–]|\s+(?:Senior|Director|VP|Head|Manager|Engineer|Founder|CEO|CTO|at )|\s*$)",
            headline, re.IGNORECASE,
        ):
            company = match.group(1).strip().rstrip(".")
            if company.lower() not in [c.lower() for c in companies_found] and len(company) > 1:
                companies_found.append(company)

    for company in companies_found:
        c = company.lower().strip()
        if c in FAANG or any(f in c for f in FAANG):
            tier = "faang"
            break
        if c in UNICORNS or any(u in c for u in UNICORNS if len(u) > 3):
            if tier != "faang":
                tier = "unicorn"
        if c in TOP_VCS or any(v in c for v in TOP_VCS if len(v) > 3):
            if tier not in ("faang", "unicorn"):
                tier = "top_vc"
        if c in TOP_CONSULTING or any(t in c for t in TOP_CONSULTING if len(t) > 3):
            if tier not in ("faang", "unicorn", "top_vc"):
                tier = "consulting"

    if companies_found and not tier:
        tier = "other"

    return (companies_found or None), tier


def classify_sectors(headline: Optional[str], signals: list) -> Optional[List[str]]:
    """Extract sector tags from headline and signals."""
    text = " ".join([headline or ""] + signals).lower()
    sectors = [sector for sector, pattern in SECTOR_PATTERNS.items()
               if re.search(pattern, text, re.IGNORECASE)]
    return sectors or None


def classify_stealth_strength(headline: Optional[str], signals: list) -> str:
    """How strong is the stealth signal?"""
    signal_set = {s.lower() for s in signals}
    h = (headline or "").lower()

    if "stealth startup" in signal_set or "stealth mode" in signal_set:
        return "confirmed"
    if re.search(r"\bstealth\b", h):
        return "confirmed"

    likely_signals = {
        "building something", "something new", "working on something",
        "new venture", "next chapter", "im aufbau", "coming soon",
        "pre-seed", "exploring opportunities",
    }
    if signal_set & likely_signals:
        return "likely"
    if re.search(r"\b(building|launching|starting|creating)\b.*\b(new|next|stealth|startup)\b", h):
        return "likely"

    return "weak"


def classify_data_quality(name: Optional[str], headline: Optional[str]) -> str:
    """Assess data quality of this record."""
    if not name or len(name) < 2:
        return "junk"
    if " - " in (name or "") or len(name or "") > 80:
        return "junk"
    if not headline:
        return "partial"
    if len(headline) > 180:
        return "junk"
    if len(headline) < 10:
        return "partial"
    return "clean"


def tag_founder(
    name: Optional[str],
    headline: Optional[str],
    location: Optional[str],
    stealth_signals: Optional[str],
) -> Dict[str, any]:
    """
    Compute all tags for a founder from their scraped data.

    Args:
        name: Founder name
        headline: LinkedIn headline
        location: LinkedIn location
        stealth_signals: JSON string of signals dict or list

    Returns:
        Dict of tag columns to update in the database.
    """
    # Parse signals
    signals_list = []
    if stealth_signals:
        try:
            parsed = json.loads(stealth_signals)
            if isinstance(parsed, dict):
                for v in parsed.values():
                    if isinstance(v, list):
                        signals_list.extend(v)
            elif isinstance(parsed, list):
                signals_list = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    ex_companies, ex_tier = classify_ex_companies(headline, signals_list)

    return {
        "founder_role": classify_role(headline, signals_list),
        "ex_company_tier": ex_tier,
        "ex_companies": json.dumps(ex_companies) if ex_companies else None,
        "sector_tags": json.dumps(classify_sectors(headline, signals_list)) if classify_sectors(headline, signals_list) else None,
        "stealth_strength": classify_stealth_strength(headline, signals_list),
        "data_quality": classify_data_quality(name, headline),
        "geo_region": classify_geo(location),
    }
