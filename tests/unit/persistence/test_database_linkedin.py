"""
Tests for database LinkedIn enrichment methods.

Tests officer table migration, enrichment queries,
and LinkedIn field updates.
"""

import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from persistence.database import Database


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def db_with_officers(test_db):
    """Database with companies and officers for LinkedIn enrichment tests."""
    # Insert companies with different AI scores
    high_score_id = test_db.insert_company(
        company_number="HRB10001",
        name="HighScore AI GmbH",
        source="test",
        city="Berlin",
        ai_robotics_score=5,
        startup_score=3,
    )
    medium_score_id = test_db.insert_company(
        company_number="HRB10002",
        name="MediumScore Tech GmbH",
        source="test",
        city="Munich",
        ai_robotics_score=2,
        startup_score=1,
    )
    zero_score_id = test_db.insert_company(
        company_number="HRB10003",
        name="ZeroScore Bakery GmbH",
        source="test",
        city="Hamburg",
        ai_robotics_score=0,
        startup_score=0,
    )

    # Insert officers
    officer1_id = test_db.insert_officer(high_score_id, "Max Mustermann", "Geschäftsführer")
    officer2_id = test_db.insert_officer(high_score_id, "Anna Schmidt", "Prokuristin")
    officer3_id = test_db.insert_officer(medium_score_id, "Jan Weber", "Geschäftsführer")
    officer4_id = test_db.insert_officer(zero_score_id, "Lisa Fischer", "Geschäftsführerin")

    return {
        'db': test_db,
        'high_score_id': high_score_id,
        'medium_score_id': medium_score_id,
        'zero_score_id': zero_score_id,
        'officer1_id': officer1_id,
        'officer2_id': officer2_id,
        'officer3_id': officer3_id,
        'officer4_id': officer4_id,
    }


# ============================================================================
# Migration Tests
# ============================================================================

class TestMigrateOfficersTable:
    """Test officer table LinkedIn column migration."""

    def test_adds_all_linkedin_columns(self, test_db):
        """All 8 LinkedIn columns are present after migration."""
        cursor = test_db.conn.cursor()
        cursor.execute("PRAGMA table_info(officers)")
        columns = [row[1] for row in cursor.fetchall()]

        expected_linkedin_columns = [
            'linkedin_url',
            'linkedin_headline',
            'linkedin_location',
            'linkedin_previous_companies',
            'linkedin_snippet',
            'linkedin_match_confidence',
            'linkedin_enriched_at',
            'linkedin_enrichment_source',
        ]

        for col in expected_linkedin_columns:
            assert col in columns, f"Missing column: {col}"

    def test_migration_idempotent(self, test_db):
        """Running migration twice doesn't fail."""
        cursor = test_db.conn.cursor()
        # Force re-run migration
        test_db._migrate_officers_table(cursor)
        # Should not raise
        cursor.execute("PRAGMA table_info(officers)")
        columns = [row[1] for row in cursor.fetchall()]
        assert 'linkedin_url' in columns


# ============================================================================
# get_officers_for_linkedin_enrichment Tests
# ============================================================================

class TestGetOfficersForLinkedInEnrichment:
    """Test enrichment queue query."""

    def test_returns_unenriched_officers(self, db_with_officers):
        """Returns officers that haven't been enriched yet."""
        db = db_with_officers['db']
        officers = db.get_officers_for_linkedin_enrichment(limit=10)

        # Should include officers from high-score and medium-score companies
        names = [o['name'] for o in officers]
        assert 'Max Mustermann' in names
        assert 'Anna Schmidt' in names
        assert 'Jan Weber' in names

    def test_excludes_enriched_officers(self, db_with_officers):
        """Already enriched officers are excluded."""
        db = db_with_officers['db']
        officer1_id = db_with_officers['officer1_id']

        # Enrich officer 1
        db.update_officer_linkedin(
            officer1_id,
            linkedin_url="https://linkedin.com/in/max",
            linkedin_match_confidence=0.85,
            linkedin_enrichment_source='search_snippet',
        )

        officers = db.get_officers_for_linkedin_enrichment(limit=10)
        names = [o['name'] for o in officers]

        assert 'Max Mustermann' not in names
        assert 'Anna Schmidt' in names

    def test_filters_low_ai_score(self, db_with_officers):
        """Companies with ai_robotics_score=0 are excluded."""
        db = db_with_officers['db']
        officers = db.get_officers_for_linkedin_enrichment(limit=10)

        names = [o['name'] for o in officers]
        assert 'Lisa Fischer' not in names  # Zero score company

    def test_orders_by_ai_score_desc(self, db_with_officers):
        """Higher AI score companies come first."""
        db = db_with_officers['db']
        officers = db.get_officers_for_linkedin_enrichment(limit=10)

        # Officers from high-score (5) should come before medium-score (2)
        high_score_names = {'Max Mustermann', 'Anna Schmidt'}
        medium_score_names = {'Jan Weber'}

        # Find positions
        high_positions = [i for i, o in enumerate(officers) if o['name'] in high_score_names]
        medium_positions = [i for i, o in enumerate(officers) if o['name'] in medium_score_names]

        if high_positions and medium_positions:
            assert max(high_positions) < min(medium_positions)

    def test_includes_company_data(self, db_with_officers):
        """Results include joined company data."""
        db = db_with_officers['db']
        officers = db.get_officers_for_linkedin_enrichment(limit=1)

        assert len(officers) == 1
        officer = officers[0]
        assert 'company_name' in officer
        assert 'company_city' in officer
        assert 'ai_robotics_score' in officer

    def test_respects_limit(self, db_with_officers):
        """Limit parameter is respected."""
        db = db_with_officers['db']
        officers = db.get_officers_for_linkedin_enrichment(limit=1)
        assert len(officers) == 1

    def test_only_current_officers(self, db_with_officers):
        """Only current officers (is_current=1) are returned."""
        db = db_with_officers['db']
        high_score_id = db_with_officers['high_score_id']

        # Insert a non-current officer
        db.insert_officer(high_score_id, "Old Officer", "Geschäftsführer",
                         is_current=False)

        officers = db.get_officers_for_linkedin_enrichment(limit=20)
        names = [o['name'] for o in officers]
        assert 'Old Officer' not in names


# ============================================================================
# update_officer_linkedin Tests
# ============================================================================

class TestUpdateOfficerLinkedIn:
    """Test LinkedIn field updates."""

    def test_sets_fields(self, db_with_officers):
        """URL, headline, confidence stored correctly."""
        db = db_with_officers['db']
        officer_id = db_with_officers['officer1_id']
        company_id = db_with_officers['high_score_id']

        db.update_officer_linkedin(
            officer_id,
            linkedin_url="https://linkedin.com/in/max-mustermann",
            linkedin_headline="Co-Founder & CEO at TechBot",
            linkedin_location="Berlin, Germany",
            linkedin_previous_companies='["Google", "N26"]',
            linkedin_match_confidence=0.92,
            linkedin_enrichment_source='search_snippet',
        )

        # Read back
        officers = db.get_officers(company_id)
        officer = next(o for o in officers if o['id'] == officer_id)

        assert officer['linkedin_url'] == "https://linkedin.com/in/max-mustermann"
        assert officer['linkedin_headline'] == "Co-Founder & CEO at TechBot"
        assert officer['linkedin_location'] == "Berlin, Germany"
        assert officer['linkedin_previous_companies'] == '["Google", "N26"]'
        assert officer['linkedin_match_confidence'] == 0.92
        assert officer['linkedin_enrichment_source'] == 'search_snippet'

    def test_sets_timestamp(self, db_with_officers):
        """linkedin_enriched_at is automatically set."""
        db = db_with_officers['db']
        officer_id = db_with_officers['officer1_id']
        company_id = db_with_officers['high_score_id']

        db.update_officer_linkedin(
            officer_id,
            linkedin_match_confidence=0.0,
            linkedin_enrichment_source='no_match',
        )

        officers = db.get_officers(company_id)
        officer = next(o for o in officers if o['id'] == officer_id)

        assert officer['linkedin_enriched_at'] is not None
        # Should be an ISO timestamp string
        assert 'T' in officer['linkedin_enriched_at']

    def test_no_kwargs_noop(self, db_with_officers):
        """Empty kwargs → no update (no crash)."""
        db = db_with_officers['db']
        officer_id = db_with_officers['officer1_id']

        # Should not raise
        db.update_officer_linkedin(officer_id)

    def test_no_match_marking(self, db_with_officers):
        """Marking as no_match excludes from future enrichment queries."""
        db = db_with_officers['db']
        officer_id = db_with_officers['officer1_id']

        # Mark as no_match
        db.update_officer_linkedin(
            officer_id,
            linkedin_match_confidence=0.0,
            linkedin_enrichment_source='no_match',
        )

        # Should no longer appear in enrichment queue
        officers = db.get_officers_for_linkedin_enrichment(limit=10)
        ids = [o['id'] for o in officers]
        assert officer_id not in ids
