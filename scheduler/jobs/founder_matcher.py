"""
Founder Matcher - Links stealth founders to Handelsregister companies.

Detects when a stealth founder "emerges" by:
1. Matching founder name to company officers
2. Matching founder name to company names (if they name it after themselves)
3. Fuzzy matching for slight name variations
"""

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """Normalize a name for matching."""
    if not name:
        return ""
    # Lowercase, remove extra spaces, common suffixes
    name = name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    # Remove common titles/suffixes
    name = re.sub(r"\b(dr|prof|ing|dipl|mba|phd|jr|sr|ii|iii)\b\.?", "", name)
    # Remove special chars except spaces and hyphens
    name = re.sub(r"[^a-zäöüß\s\-]", "", name)
    return name.strip()


def name_similarity(name1: str, name2: str) -> float:
    """Calculate similarity between two names (0.0 to 1.0)."""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)

    if not n1 or not n2:
        return 0.0

    # Exact match
    if n1 == n2:
        return 1.0

    # Check if one contains the other (partial match)
    if n1 in n2 or n2 in n1:
        return 0.9

    # Fuzzy match using sequence matcher
    return SequenceMatcher(None, n1, n2).ratio()


def match_founder_to_officers(db, founder_name: str, min_similarity: float = 0.85) -> List[Dict]:
    """
    Find company officers that match a founder name.

    Returns list of matches with company info.
    """
    cursor = db.conn.cursor()

    # Get all officers
    cursor.execute("""
        SELECT o.id, o.name, o.role, o.company_id, c.name as company_name, c.registration_date
        FROM officers o
        JOIN companies c ON o.company_id = c.id
        ORDER BY c.registration_date DESC
    """)

    matches = []
    for row in cursor.fetchall():
        officer_name = row[1]
        similarity = name_similarity(founder_name, officer_name)

        if similarity >= min_similarity:
            matches.append(
                {
                    "officer_id": row[0],
                    "officer_name": officer_name,
                    "role": row[2],
                    "company_id": row[3],
                    "company_name": row[4],
                    "registration_date": row[5],
                    "similarity": similarity,
                }
            )

    # Sort by similarity then by registration date (newest first)
    matches.sort(key=lambda x: (-x["similarity"], x["registration_date"] or ""), reverse=True)

    return matches


def match_founder_to_companies(db, founder_name: str, min_similarity: float = 0.7) -> List[Dict]:
    """
    Find companies where the founder name appears in the company name.

    Catches cases like "Max Müller GmbH" for founder "Max Müller".
    """
    cursor = db.conn.cursor()

    cursor.execute("""
        SELECT id, name, registration_date, legal_form
        FROM companies
        ORDER BY registration_date DESC
    """)

    matches = []
    normalized_founder = normalize_name(founder_name)

    for row in cursor.fetchall():
        company_name = row[1]
        normalized_company = normalize_name(company_name)

        # Check if founder name is in company name
        if normalized_founder in normalized_company:
            matches.append(
                {
                    "company_id": row[0],
                    "company_name": company_name,
                    "registration_date": row[2],
                    "legal_form": row[3],
                    "match_type": "name_in_company",
                    "similarity": 1.0,
                }
            )

    return matches


def find_emerged_founders(db, min_similarity: float = 0.85) -> List[Dict]:
    """
    Find stealth founders who have likely emerged (appear in recent companies).

    Returns list of potential matches for review.
    """
    cursor = db.conn.cursor()

    # Get all stealth founders without a company_id yet
    cursor.execute("""
        SELECT id, name, headline, linkedin_url, first_seen_at
        FROM stealth_founders
        WHERE company_id IS NULL AND emerged_at IS NULL
    """)

    emerged = []

    for row in cursor.fetchall():
        founder_id = row[0]
        founder_name = row[1]

        if not founder_name:
            continue

        # Check officers
        officer_matches = match_founder_to_officers(db, founder_name, min_similarity)

        # Check company names
        company_matches = match_founder_to_companies(db, founder_name)

        if officer_matches or company_matches:
            emerged.append(
                {
                    "founder_id": founder_id,
                    "founder_name": founder_name,
                    "headline": row[2],
                    "linkedin_url": row[3],
                    "first_seen_at": row[4],
                    "officer_matches": officer_matches[:5],  # Top 5
                    "company_matches": company_matches[:5],
                }
            )

    return emerged


def link_founder_to_company(db, founder_id: int, company_id: int):
    """Link a stealth founder to their emerged company."""
    cursor = db.conn.cursor()
    cursor.execute(
        """
        UPDATE stealth_founders
        SET company_id = ?, emerged_at = ?, profile_changed = 1
        WHERE id = ?
    """,
        (company_id, datetime.now().isoformat(), founder_id),
    )
    db.conn.commit()
    logger.info(f"Linked founder {founder_id} to company {company_id}")


def run_founder_matching(db_path: str = "handelsregister.db"):
    """
    Run the founder matching process and report results.
    """
    from persistence.database import Database

    db = Database(db_path)

    try:
        emerged = find_emerged_founders(db)

        print("\n=== Potential Emerged Founders ===\n")
        print(f"Found {len(emerged)} stealth founders with potential company matches\n")

        for e in emerged:
            print(f"FOUNDER: {e['founder_name']}")
            print(f"  Headline: {e['headline'][:50] if e['headline'] else 'N/A'}...")
            print(f"  LinkedIn: {e['linkedin_url']}")
            print(f"  First seen: {e['first_seen_at']}")

            if e["officer_matches"]:
                print("  OFFICER MATCHES:")
                for m in e["officer_matches"][:3]:
                    print(f"    - {m['officer_name']} ({m['role']}) at {m['company_name']}")
                    print(f"      Registered: {m['registration_date']} | Similarity: {m['similarity']:.0%}")

            if e["company_matches"]:
                print("  COMPANY NAME MATCHES:")
                for m in e["company_matches"][:3]:
                    print(f"    - {m['company_name']} ({m['legal_form']})")
                    print(f"      Registered: {m['registration_date']}")

            print()

        return emerged

    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_founder_matching()
