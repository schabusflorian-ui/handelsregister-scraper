"""
Emergence Matcher - Connect stealth founders to new company registrations.

This module matches stealth founders from LinkedIn to newly registered
companies in the Handelsregister, enabling early detection of startup emergence.

Inspired by Specter's talent-to-company matching capabilities.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)


class EmergenceMatcher:
    """
    Matches stealth founders to newly registered companies.

    Uses fuzzy name matching between stealth founder profiles and
    company officers in recently registered companies.
    """

    def __init__(
        self,
        db,
        min_name_similarity: float = 0.85,
        lookback_days: int = 90,
    ):
        """
        Initialize the emergence matcher.

        Args:
            db: Database instance
            min_name_similarity: Minimum fuzzy match ratio (0-1) for name matching
            lookback_days: How far back to look for new companies
        """
        self.db = db
        self.min_name_similarity = min_name_similarity
        self.lookback_days = lookback_days

    def find_matches_for_founder(
        self,
        founder: Dict,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Find potential company matches for a stealth founder.

        Args:
            founder: Stealth founder record from database
            limit: Maximum matches to return

        Returns:
            List of potential matches with scores
        """
        founder_name = founder.get("name")
        if not founder_name:
            return []

        # Normalize founder name
        founder_name_normalized = self._normalize_name(founder_name)
        first_seen = founder.get("first_seen_at")

        # Get recent companies registered after founder was first seen
        cutoff_date = None
        if first_seen:
            try:
                cutoff_date = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                cutoff_date = cutoff_date - timedelta(days=30)  # Allow some buffer
            except (ValueError, AttributeError):
                pass

        # Get recent high-score companies
        recent_companies = self._get_recent_companies(cutoff_date)

        matches = []
        for company in recent_companies:
            # Get officers for this company
            officers = self.db.get_officers(company["id"])

            for officer in officers:
                officer_name = officer.get("name", "")
                officer_name_normalized = self._normalize_name(officer_name)

                # Calculate similarity
                similarity = fuzz.ratio(founder_name_normalized, officer_name_normalized) / 100.0

                if similarity >= self.min_name_similarity:
                    matches.append(
                        {
                            "company_id": company["id"],
                            "company_name": company["name"],
                            "company_city": company.get("city"),
                            "registration_date": company.get("registration_date"),
                            "ai_score": company.get("ai_robotics_score", 0),
                            "officer_name": officer_name,
                            "officer_role": officer.get("role"),
                            "name_similarity": similarity,
                            "match_type": "officer_name",
                        }
                    )

        # Sort by similarity and limit
        matches.sort(key=lambda x: x["name_similarity"], reverse=True)
        return matches[:limit]

    def find_all_emergences(
        self,
        min_confidence: float = 0.4,
        limit: int = 100,
    ) -> List[Dict]:
        """
        Find all potential founder-to-company matches.

        Args:
            min_confidence: Minimum founder confidence score
            limit: Maximum founders to process

        Returns:
            List of all matches found
        """
        # Get unemerged founders
        founders = self.db.get_unemerged_founders(min_confidence=min_confidence, limit=limit)

        logger.info(f"Checking {len(founders)} stealth founders for emergence")

        all_matches = []
        for founder in founders:
            matches = self.find_matches_for_founder(founder)
            for match in matches:
                match["founder_id"] = founder["id"]
                match["founder_name"] = founder["name"]
                match["founder_confidence"] = founder["confidence_score"]
                all_matches.append(match)

        logger.info(f"Found {len(all_matches)} potential emergence matches")
        return all_matches

    def auto_link_high_confidence_matches(
        self,
        min_name_similarity: float = 0.95,
        min_founder_confidence: float = 0.5,
    ) -> List[Dict]:
        """
        Automatically link founders to companies with very high confidence.

        Only links when name match is extremely high (>95%) to avoid false positives.

        Returns:
            List of linked founder-company pairs
        """
        matches = self.find_all_emergences(min_confidence=min_founder_confidence)

        linked = []
        for match in matches:
            if match["name_similarity"] >= min_name_similarity:
                founder_id = match["founder_id"]
                company_id = match["company_id"]

                # Check if not already linked
                founder = self.db.get_stealth_founder(founder_id)
                if founder and not founder.get("company_id"):
                    self.db.mark_founder_emerged(founder_id, company_id)
                    linked.append(match)
                    logger.info(
                        f"Auto-linked: {match['founder_name']} -> {match['company_name']} "
                        f"(similarity: {match['name_similarity']:.2f})"
                    )

        return linked

    def get_emergence_candidates(
        self,
        min_similarity: float = 0.85,
        min_founder_confidence: float = 0.4,
    ) -> List[Dict]:
        """
        Get emergence candidates for manual review.

        Returns matches that need human verification before linking.
        """
        matches = self.find_all_emergences(min_confidence=min_founder_confidence)

        # Filter to candidates needing review
        candidates = [m for m in matches if m["name_similarity"] >= min_similarity]

        return candidates

    def _normalize_name(self, name: str) -> str:
        """Normalize a name for comparison."""
        if not name:
            return ""

        # Lowercase
        name = name.lower()

        # Remove common titles and suffixes
        titles = ["dr.", "prof.", "dipl.", "ing.", "mr.", "mrs.", "ms."]
        for title in titles:
            name = name.replace(title, "")

        # Remove extra whitespace
        name = " ".join(name.split())

        return name.strip()

    def _get_recent_companies(self, cutoff_date: Optional[datetime] = None) -> List[Dict]:
        """Get recently registered companies with high AI scores."""
        cursor = self.db.conn.cursor()

        if cutoff_date:
            cutoff_str = cutoff_date.strftime("%Y-%m-%d")
            cursor.execute(
                """
                SELECT * FROM companies
                WHERE ai_robotics_score >= 1
                  AND registration_date >= ?
                ORDER BY registration_date DESC
                LIMIT 1000
            """,
                (cutoff_str,),
            )
        else:
            # Last N days
            cursor.execute(
                """
                SELECT * FROM companies
                WHERE ai_robotics_score >= 1
                  AND registration_date >= date('now', '-' || ? || ' days')
                ORDER BY registration_date DESC
                LIMIT 1000
            """,
                (self.lookback_days,),
            )

        return [dict(row) for row in cursor.fetchall()]


class FounderRechecker:
    """
    Re-checks stealth founders to detect profile changes and emergence.
    """

    def __init__(self, db, scraper=None):
        """
        Initialize the founder rechecker.

        Args:
            db: Database instance
            scraper: Optional LinkedInProfileScraper instance
        """
        self.db = db
        self.scraper = scraper

    def get_founders_needing_recheck(
        self,
        days_since_check: int = 7,
        min_confidence: float = 0.4,
        limit: int = 50,
    ) -> List[Dict]:
        """Get founders that need to be re-scraped."""
        return self.db.get_stealth_founders_for_recheck(days_since_check=days_since_check, limit=limit)

    def recheck_founder(self, founder: Dict) -> Optional[Dict]:
        """
        Re-scrape a founder's LinkedIn profile and detect changes.

        Returns:
            Dict with changes if any detected, None otherwise
        """
        if not self.scraper:
            logger.warning("No scraper configured for rechecker")
            return None

        linkedin_url = founder.get("linkedin_url")
        if not linkedin_url:
            return None

        # Scrape current profile
        try:
            profile = self.scraper.scrape_profile(linkedin_url)
            if not profile:
                return None
        except Exception as e:
            logger.error(f"Failed to re-scrape {linkedin_url}: {e}")
            return None

        # Compare and update
        changes = self.db.update_stealth_founder(
            founder_id=founder["id"],
            headline=profile.headline,
            summary=profile.summary,
            current_company=profile.current_company,
            location=profile.location,
            confidence_score=profile.confidence_score,
        )

        if changes:
            logger.info(f"Detected {len(changes)} changes for {founder.get('name')}")
            return {
                "founder_id": founder["id"],
                "founder_name": founder.get("name"),
                "changes": changes,
            }

        return None

    def run_recheck_batch(
        self,
        days_since_check: int = 7,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Run a batch of founder re-checks.

        Returns list of founders with detected changes.
        """
        founders = self.get_founders_needing_recheck(days_since_check=days_since_check, limit=limit)

        logger.info(f"Re-checking {len(founders)} founders")

        results = []
        for founder in founders:
            result = self.recheck_founder(founder)
            if result:
                results.append(result)

        return results


def run_emergence_detection(db, auto_link: bool = False) -> Dict:
    """
    Run full emergence detection pipeline.

    Args:
        db: Database instance
        auto_link: Whether to auto-link high-confidence matches

    Returns:
        Summary of detection results
    """
    matcher = EmergenceMatcher(db)

    results = {
        "candidates": [],
        "auto_linked": [],
        "total_founders_checked": 0,
    }

    # Find all candidates
    candidates = matcher.get_emergence_candidates()
    results["candidates"] = candidates
    results["total_founders_checked"] = len(set(c["founder_id"] for c in candidates))

    # Auto-link if enabled
    if auto_link:
        linked = matcher.auto_link_high_confidence_matches()
        results["auto_linked"] = linked

    logger.info(
        f"Emergence detection complete: {len(candidates)} candidates, {len(results['auto_linked'])} auto-linked"
    )

    return results
