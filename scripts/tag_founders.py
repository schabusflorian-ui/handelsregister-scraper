#!/usr/bin/env python3
"""
Auto-tag stealth founders from existing data (headline, signals, location).

Computes:
  - founder_role:      founder, co-founder, cto, ceo, engineer, investor, other
  - ex_company_tier:   faang, unicorn, top_vc, other, null
  - ex_companies:      JSON list of parsed ex-company names
  - sector_tags:       JSON list: ai, fintech, climate, saas, health, proptech, etc.
  - stealth_strength:  confirmed, likely, weak
  - data_quality:      clean, partial, junk
  - geo_region:        berlin, munich, hamburg, frankfurt, nrw, dach_other, etc.

Usage:
    python scripts/tag_founders.py                # Tag all untagged founders
    python scripts/tag_founders.py --retag        # Re-tag all founders
    python scripts/tag_founders.py --stats        # Show tag distribution
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DB_PATH = str(Path(__file__).parent.parent / "handelsregister.db")

# ============================================================
# Company tier classification
# ============================================================

FAANG = {
    'google', 'alphabet', 'meta', 'facebook', 'amazon', 'apple', 'microsoft',
    'netflix', 'deepmind', 'openai', 'googlex', 'google x',
}

UNICORNS = {
    'n26', 'revolut', 'stripe', 'klarna', 'adyen', 'checkout.com',
    'celonis', 'personio', 'flixbus', 'wefox', 'trade republic',
    'gorillas', 'contentful', 'mambu', 'sennder', 'forto', 'scalable capital',
    'grammarly', 'notion', 'figma', 'canva', 'databricks', 'snowflake',
    'palantir', 'coinbase', 'robinhood', 'plaid', 'wise', 'transferwise',
    'delivery hero', 'hellofresh', 'zalando', 'auto1', 'about you',
    'sumup', 'billie', 'raisin', 'smava', 'taxfix', 'agicap',
    'tier', 'getir', 'bolt', 'wolt',
    'spotify', 'airbnb', 'uber', 'lyft', 'doordash', 'instacart',
    'slack', 'twilio', 'shopify', 'atlassian', 'salesforce',
    'bytedance', 'tiktok',
}

TOP_VCS = {
    'sequoia', 'a16z', 'andreessen', 'accel', 'benchmark', 'greylock',
    'kkr', 'softbank', 'tiger global', 'insight partners',
    'index ventures', 'atomico', 'balderton', 'lakestar', 'earlybird',
    'hv capital', 'project a', 'cherry ventures', 'point nine',
    'speedinvest', 'creandum', 'northzone', 'ey', 'ef',
    'antler', 'entrepreneur first', 'y combinator', 'yc', 'techstars',
    'rocket internet',
}

TOP_CONSULTING = {
    'mckinsey', 'bain', 'bcg', 'boston consulting', 'roland berger',
    'oliver wyman', 'strategy&', 'kearney', 'deloitte', 'pwc',
    'ernst & young', 'kpmg', 'accenture',
}

# ============================================================
# Sector keywords
# ============================================================

SECTOR_PATTERNS = {
    'ai': r'\b(ai|artificial intelligence|machine learning|ml|deep learning|llm|gpt|neural|nlp|computer vision|generative ai|künstliche intelligenz)\b',
    'fintech': r'\b(fintech|banking|payment|neobank|insurtech|defi|crypto|blockchain|trading|wealth|lending|finance tech)\b',
    'climate': r'\b(climate|cleantech|greentech|carbon|sustainability|renewable|energy transition|solar|wind energy|circular economy|co2)\b',
    'saas': r'\b(saas|b2b software|enterprise software|cloud platform|developer tool|devtool|api platform)\b',
    'health': r'\b(health|healthtech|medtech|biotech|pharma|therapeut|diagnostic|digital health|mental health|femtech)\b',
    'proptech': r'\b(proptech|real estate|immobilien|construction tech|contech)\b',
    'mobility': r'\b(mobility|autonomous|self-driving|ev |electric vehicle|logistics|fleet|last.mile)\b',
    'security': r'\b(cybersecurity|cyber|infosec|security platform|identity|authentication)\b',
    'foodtech': r'\b(foodtech|food tech|agritech|agtech|farming|alternative protein)\b',
    'edtech': r'\b(edtech|education|learning platform|e-learning|upskilling)\b',
    'robotics': r'\b(robot|robotics|robotik|automation|industrial automation|cobot)\b',
    'hr': r'\b(hr tech|hrtech|recruiting|talent|people analytics|workforce)\b',
    'impact': r'\b(impact|social enterprise|social impact|impact tech)\b',
}

# ============================================================
# Geo normalization
# ============================================================

GEO_MAP = {
    'berlin': ['berlin'],
    'munich': ['münchen', 'munich', 'bavaria', 'bayern'],
    'hamburg': ['hamburg'],
    'frankfurt': ['frankfurt', 'hesse', 'hessen'],
    'nrw': ['düsseldorf', 'köln', 'cologne', 'dortmund', 'essen', 'bonn',
             'nordrhein', 'north rhine', 'nrw'],
    'stuttgart': ['stuttgart', 'württemberg', 'baden-württemberg', 'baden württemberg'],
    'zurich': ['zürich', 'zurich', 'zug'],
    'vienna': ['wien', 'vienna'],
    'switzerland': ['schweiz', 'swiss', 'switzerland', 'bern', 'genf', 'geneva',
                     'basel', 'lausanne', 'st. gallen', 'luzern', 'lucerne'],
    'austria': ['österreich', 'austria', 'graz', 'linz', 'salzburg', 'innsbruck'],
}


def classify_geo(location: str) -> str:
    """Normalize location to a region tag."""
    if not location:
        return None
    loc_lower = location.lower()
    for region, keywords in GEO_MAP.items():
        for kw in keywords:
            if kw in loc_lower:
                return region
    # Check broad country matches
    if 'deutschland' in loc_lower or 'germany' in loc_lower or 'german' in loc_lower:
        return 'germany_other'
    return 'other'


def classify_role(headline: str, signals: list) -> str:
    """Determine the founder's role from headline."""
    if not headline:
        # Fall back to signals
        signal_set = set(s.lower() for s in signals)
        if 'co-founder' in signal_set or 'mitgründer' in signal_set:
            return 'co-founder'
        if 'founder' in signal_set or 'gründer' in signal_set:
            return 'founder'
        if 'ceo' in signal_set or 'chief executive' in signal_set:
            return 'ceo'
        return 'other'

    h = headline.lower()

    # Check from most specific to least
    if re.search(r'\b(co-?founder|mitgründer|co-?gründer)\b', h):
        return 'co-founder'
    if re.search(r'\b(cto|chief technology)\b', h):
        return 'cto'
    if re.search(r'\b(ceo|chief executive|geschäftsführer)\b', h):
        return 'ceo'
    if re.search(r'\b(founder|gründer|founded|founding)\b', h):
        return 'founder'
    if re.search(r'\b(angel investor|investor|vc |venture)\b', h):
        return 'investor'
    if re.search(r'\b(engineer|developer|architect|coder|hacker)\b', h):
        return 'engineer'
    if re.search(r'\b(director|vp |head of|manager|lead)\b', h):
        return 'executive'

    # Fall back to signals
    signal_set = set(s.lower() for s in signals)
    if 'co-founder' in signal_set:
        return 'co-founder'
    if 'founder' in signal_set or 'gründer' in signal_set:
        return 'founder'
    if 'ceo' in signal_set:
        return 'ceo'
    if 'angel investor' in signal_set:
        return 'investor'

    return 'other'


def classify_ex_companies(headline: str, signals: list) -> tuple:
    """Extract ex-company names and classify tier."""
    companies_found = []
    tier = None

    # Extract from signals (most reliable)
    for signal in signals:
        s = signal.lower()
        if s.startswith('ex-'):
            company_name = signal[3:]  # Keep original casing
            companies_found.append(company_name)

    # Also parse headline for "ex-X" and "Ex X" patterns
    if headline:
        for match in re.finditer(r'(?:ex[- ])(\w[\w& .]*?)(?:\s*[|,\-–]|\s+(?:Senior|Director|VP|Head|Manager|Engineer|Architect|Founder|CEO|CTO|at )|\s*$)', headline, re.IGNORECASE):
            company = match.group(1).strip().rstrip('.')
            if company.lower() not in [c.lower() for c in companies_found] and len(company) > 1:
                companies_found.append(company)

    # Classify tier
    for company in companies_found:
        c = company.lower().strip()
        if c in FAANG or any(f in c for f in FAANG):
            tier = 'faang'
            break
        if c in UNICORNS or any(u in c for u in UNICORNS if len(u) > 3):
            if tier != 'faang':
                tier = 'unicorn'
        if c in TOP_VCS or any(v in c for v in TOP_VCS if len(v) > 3):
            if tier not in ('faang', 'unicorn'):
                tier = 'top_vc'
        if c in TOP_CONSULTING or any(t in c for t in TOP_CONSULTING if len(t) > 3):
            if tier not in ('faang', 'unicorn', 'top_vc'):
                tier = 'consulting'

    if companies_found and not tier:
        tier = 'other'

    return companies_found if companies_found else None, tier


def classify_sectors(headline: str, signals: list) -> list:
    """Extract sector tags from headline and signals."""
    text = ' '.join([headline or ''] + signals).lower()
    sectors = []
    for sector, pattern in SECTOR_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            sectors.append(sector)
    return sectors if sectors else None


def classify_stealth_strength(headline: str, signals: list) -> str:
    """How strong is the stealth signal?"""
    signal_set = set(s.lower() for s in signals)
    h = (headline or '').lower()

    # Confirmed: explicitly says stealth
    if 'stealth startup' in signal_set or 'stealth mode' in signal_set:
        return 'confirmed'
    if re.search(r'\bstealth\b', h):
        return 'confirmed'

    # Likely: building something, pre-seed, coming soon, etc.
    likely_signals = {
        'building something', 'something new', 'working on something',
        'new venture', 'next chapter', 'im aufbau', 'coming soon',
        'pre-seed', 'exploring opportunities',
    }
    if signal_set & likely_signals:
        return 'likely'
    if re.search(r'\b(building|launching|starting|creating)\b.*\b(new|next|stealth|startup)\b', h):
        return 'likely'

    # Weak: just has founder signals
    return 'weak'


def classify_data_quality(name: str, headline: str) -> str:
    """Assess data quality of this record."""
    if not name or len(name) < 2:
        return 'junk'

    # Name contains " - " suggesting it's multiple people mashed together
    if name and (' - ' in name or len(name) > 80):
        return 'junk'

    if not headline:
        return 'partial'

    # Headline too long = multiple search snippets mashed
    if len(headline) > 180:
        return 'junk'

    # Headline contains names of OTHER people (pattern: "Name Lastname - ")
    other_people = re.findall(r'[A-Z][a-z]+ [A-Z][a-z]+ [-–]', headline)
    if len(other_people) >= 2:
        return 'junk'

    # Very short or generic headline
    if len(headline) < 10:
        return 'partial'

    return 'clean'


def tag_founder(row: dict) -> dict:
    """Compute all tags for a single founder."""
    signals = []
    try:
        signals = json.loads(row['stealth_signals']) if row.get('stealth_signals') else []
    except (json.JSONDecodeError, TypeError):
        signals = []

    headline = row.get('headline') or ''
    name = row.get('name') or ''
    location = row.get('location') or ''

    role = classify_role(headline, signals)
    ex_companies, ex_tier = classify_ex_companies(headline, signals)
    sectors = classify_sectors(headline, signals)
    stealth = classify_stealth_strength(headline, signals)
    quality = classify_data_quality(name, headline)
    geo = classify_geo(location)

    return {
        'founder_role': role,
        'ex_company_tier': ex_tier,
        'ex_companies': json.dumps(ex_companies) if ex_companies else None,
        'sector_tags': json.dumps(sectors) if sectors else None,
        'stealth_strength': stealth,
        'data_quality': quality,
        'geo_region': geo,
    }


def run_tagger(db_path: str, retag: bool = False):
    """Tag all founders in the database."""
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    # Trigger migration to add new columns
    from persistence.database import Database
    migration_db = Database(db_path)
    migration_db.close()

    # Reopen with fresh connection
    db.close()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    if retag:
        founders = db.execute(
            "SELECT * FROM stealth_founders"
        ).fetchall()
    else:
        founders = db.execute(
            "SELECT * FROM stealth_founders WHERE data_quality IS NULL"
        ).fetchall()

    print(f"Tagging {len(founders)} founders...")

    tagged = 0
    for row in founders:
        tags = tag_founder(dict(row))
        db.execute("""
            UPDATE stealth_founders SET
                founder_role = ?,
                ex_company_tier = ?,
                ex_companies = ?,
                sector_tags = ?,
                stealth_strength = ?,
                data_quality = ?,
                geo_region = ?
            WHERE id = ?
        """, (
            tags['founder_role'],
            tags['ex_company_tier'],
            tags['ex_companies'],
            tags['sector_tags'],
            tags['stealth_strength'],
            tags['data_quality'],
            tags['geo_region'],
            row['id'],
        ))
        tagged += 1

    db.commit()
    print(f"Tagged {tagged} founders.")
    db.close()


def show_stats(db_path: str):
    """Show tag distribution."""
    db = sqlite3.connect(db_path)

    total = db.execute("SELECT COUNT(*) FROM stealth_founders").fetchone()[0]
    print(f"\n{'='*50}")
    print(f"STEALTH FOUNDER TAGS ({total} total)")
    print(f"{'='*50}")

    for col, label in [
        ('data_quality', 'Data Quality'),
        ('founder_role', 'Role'),
        ('stealth_strength', 'Stealth Strength'),
        ('ex_company_tier', 'Ex-Company Tier'),
        ('geo_region', 'Region'),
    ]:
        print(f"\n--- {label} ---")
        rows = db.execute(f"""
            SELECT COALESCE({col}, '(none)') as val, COUNT(*) as c
            FROM stealth_founders GROUP BY val ORDER BY c DESC
        """).fetchall()
        for row in rows:
            bar = '#' * (row[1] * 30 // total)
            print(f"  {row[0]:20s} {row[1]:4d}  {bar}")

    # Sector tags (JSON, need to unpack)
    print(f"\n--- Sector Tags ---")
    rows = db.execute("SELECT sector_tags FROM stealth_founders WHERE sector_tags IS NOT NULL").fetchall()
    sector_counts = {}
    for row in rows:
        try:
            for tag in json.loads(row[0]):
                sector_counts[tag] = sector_counts.get(tag, 0) + 1
        except:
            pass
    for tag, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        bar = '#' * (count * 30 // total)
        print(f"  {tag:20s} {count:4d}  {bar}")

    # Ex-companies (top ones)
    print(f"\n--- Top Ex-Companies ---")
    rows = db.execute("SELECT ex_companies FROM stealth_founders WHERE ex_companies IS NOT NULL").fetchall()
    company_counts = {}
    for row in rows:
        try:
            for c in json.loads(row[0]):
                company_counts[c.lower()] = company_counts.get(c.lower(), 0) + 1
        except:
            pass
    for company, count in sorted(company_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  {company:25s} {count:4d}")

    db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Tag stealth founders")
    parser.add_argument('--retag', action='store_true', help='Re-tag all founders')
    parser.add_argument('--stats', action='store_true', help='Show tag distribution')
    parser.add_argument('--db', default=DB_PATH, help='Database path')
    args = parser.parse_args()

    if args.stats:
        show_stats(args.db)
    else:
        run_tagger(args.db, retag=args.retag)
        show_stats(args.db)
