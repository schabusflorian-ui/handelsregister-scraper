"""
Tests for Database operations.

Tests CRUD operations, search functionality, relationships,
and data integrity constraints.
"""

import pytest
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from persistence.database import Database, Company


class TestDatabaseCompanyCRUD:
    """Test company create, read, update, delete operations."""

    def test_insert_company_returns_id(self, test_db):
        """Insert returns company ID."""
        company_id = test_db.insert_company(
            company_number="HRB10001",
            name="Test AI GmbH",
            source="test",
        )
        assert company_id is not None
        assert company_id > 0

    def test_insert_company_duplicate_fails(self, test_db):
        """Unique constraint on company_number works."""
        test_db.insert_company(
            company_number="HRB10002",
            name="First Company GmbH",
            source="test",
        )

        with pytest.raises(Exception):  # sqlite3.IntegrityError
            test_db.insert_company(
                company_number="HRB10002",  # Same number
                name="Second Company GmbH",
                source="test",
            )

    def test_get_company_by_id(self, test_db, sample_company):
        """Retrieves company by ID."""
        company_id = test_db.insert_company(**sample_company)

        result = test_db.get_company(company_id)
        assert result is not None
        assert result['name'] == sample_company['name']
        assert result['company_number'] == sample_company['company_number']

    def test_get_company_by_number(self, test_db, sample_company):
        """Retrieves company by company_number."""
        test_db.insert_company(**sample_company)

        result = test_db.get_company_by_number(sample_company['company_number'])
        assert result is not None
        assert result['name'] == sample_company['name']

    def test_get_company_by_native_number(self, test_db):
        """Retrieves company by native_company_number."""
        test_db.insert_company(
            company_number="HRB10003",
            name="Native Number Test GmbH",
            source="test",
            native_company_number="DE-HRB-10003",
        )

        result = test_db.get_company_by_native_number("DE-HRB-10003")
        assert result is not None
        assert result['name'] == "Native Number Test GmbH"

    def test_get_nonexistent_company_returns_none(self, test_db):
        """Nonexistent company returns None."""
        assert test_db.get_company(99999) is None
        assert test_db.get_company_by_number("NONEXISTENT") is None

    def test_update_company(self, test_db, sample_company):
        """Fields updated correctly."""
        company_id = test_db.insert_company(**sample_company)

        test_db.update_company(company_id, city="Munich", capital_amount=50000.0)

        result = test_db.get_company(company_id)
        assert result['city'] == "Munich"
        assert result['capital_amount'] == 50000.0

    def test_update_sets_last_updated(self, test_db, sample_company):
        """Update sets last_updated timestamp."""
        company_id = test_db.insert_company(**sample_company)
        original = test_db.get_company(company_id)

        test_db.update_company(company_id, city="Hamburg")

        updated = test_db.get_company(company_id)
        assert updated['last_updated'] != original['last_updated']

    def test_upsert_new_company(self, test_db):
        """Upsert inserts new company."""
        company_id, is_new = test_db.upsert_company(
            company_number="HRB10004",
            name="Upsert New GmbH",
            source="test",
        )

        assert is_new is True
        assert company_id > 0

        result = test_db.get_company(company_id)
        assert result['name'] == "Upsert New GmbH"

    def test_upsert_existing_company(self, test_db, sample_company):
        """Upsert updates existing company."""
        original_id = test_db.insert_company(**sample_company)

        company_id, is_new = test_db.upsert_company(
            company_number=sample_company['company_number'],
            name="Updated Name GmbH",
            source="test",
        )

        assert is_new is False
        assert company_id == original_id

        result = test_db.get_company(company_id)
        assert result['name'] == "Updated Name GmbH"


class TestDatabaseSearch:
    """Test company search functionality."""

    def test_search_by_name_pattern(self, test_db_with_companies):
        """LIKE query works."""
        results = test_db_with_companies.search_companies(name_pattern="AI")
        assert len(results) >= 1
        assert any("AI" in r['name'] for r in results)

    def test_search_by_city(self, test_db_with_companies):
        """City filter works."""
        results = test_db_with_companies.search_companies(city="Berlin")
        assert len(results) >= 1
        assert all(r['city'] == "Berlin" for r in results)

    def test_search_by_min_ai_score(self, test_db_with_companies):
        """Score filter works."""
        results = test_db_with_companies.search_companies(min_ai_score=4)
        assert len(results) >= 1
        assert all(r['ai_robotics_score'] >= 4 for r in results)

    def test_search_pagination(self, test_db_with_companies):
        """Limit and offset work."""
        all_results = test_db_with_companies.search_companies(limit=100)
        page_1 = test_db_with_companies.search_companies(limit=1, offset=0)
        page_2 = test_db_with_companies.search_companies(limit=1, offset=1)

        assert len(page_1) == 1
        assert len(page_2) == 1
        if len(all_results) > 1:
            assert page_1[0]['id'] != page_2[0]['id']

    def test_search_multiple_filters(self, test_db_with_companies):
        """Multiple filters combine with AND."""
        results = test_db_with_companies.search_companies(
            city="Berlin",
            min_ai_score=3,
        )
        for r in results:
            assert r['city'] == "Berlin"
            assert r['ai_robotics_score'] >= 3

    def test_count_companies(self, test_db_with_companies):
        """Count works correctly."""
        count = test_db_with_companies.count_companies()
        assert count >= 3  # Sample data has 3 companies

    def test_count_companies_by_source(self, test_db_with_companies):
        """Count by source works."""
        count = test_db_with_companies.count_companies(source="bundesapi")
        assert count >= 3


class TestDatabaseOfficers:
    """Test officer operations."""

    def test_insert_officer(self, test_db, sample_company, sample_officer):
        """Officer added correctly."""
        company_id = test_db.insert_company(**sample_company)
        officer_id = test_db.insert_officer(company_id, **sample_officer)

        assert officer_id > 0

    def test_get_officers(self, test_db, sample_company, sample_officer):
        """Get officers for company."""
        company_id = test_db.insert_company(**sample_company)
        test_db.insert_officer(company_id, **sample_officer)

        officers = test_db.get_officers(company_id)
        assert len(officers) == 1
        assert officers[0]['name'] == sample_officer['name']

    def test_get_officers_current_only(self, test_db, sample_company):
        """Current flag respected."""
        company_id = test_db.insert_company(**sample_company)

        # Add current officer
        test_db.insert_officer(company_id, name="Current CEO", is_current=True)
        # Add former officer
        test_db.insert_officer(company_id, name="Former CEO", is_current=False)

        all_officers = test_db.get_officers(company_id, current_only=False)
        current_officers = test_db.get_officers(company_id, current_only=True)

        assert len(all_officers) == 2
        assert len(current_officers) == 1
        assert current_officers[0]['name'] == "Current CEO"

    def test_clear_officers(self, test_db, sample_company, sample_officer):
        """All officers removed."""
        company_id = test_db.insert_company(**sample_company)
        test_db.insert_officer(company_id, **sample_officer)
        test_db.insert_officer(company_id, name="Second Officer")

        test_db.clear_officers(company_id)

        officers = test_db.get_officers(company_id)
        assert len(officers) == 0


class TestDatabaseCapitalEvents:
    """Test capital event operations."""

    def test_insert_capital_event(self, test_db, sample_company, sample_capital_event):
        """Event stored correctly."""
        company_id = test_db.insert_company(**sample_company)
        event_id = test_db.insert_capital_event(company_id, **sample_capital_event)

        assert event_id > 0

    def test_get_capital_events(self, test_db, sample_company, sample_capital_event):
        """Events retrieved for company."""
        company_id = test_db.insert_company(**sample_company)
        test_db.insert_capital_event(company_id, **sample_capital_event)

        events = test_db.get_capital_events(company_id)
        assert len(events) == 1
        assert events[0]['event_type'] == sample_capital_event['event_type']
        assert events[0]['new_amount'] == sample_capital_event['new_amount']

    def test_capital_events_ordered_by_date(self, test_db, sample_company):
        """Events ordered by date DESC."""
        company_id = test_db.insert_company(**sample_company)

        test_db.insert_capital_event(company_id, event_type="initial", event_date="2023-01-01")
        test_db.insert_capital_event(company_id, event_type="increase", event_date="2024-01-01")

        events = test_db.get_capital_events(company_id)
        assert events[0]['event_date'] == "2024-01-01"  # Most recent first


class TestDatabaseEnrichmentQueue:
    """Test enrichment queue operations."""

    def test_add_to_queue(self, test_db, sample_company):
        """Company queued correctly."""
        company_id = test_db.insert_company(**sample_company)
        test_db.add_to_enrichment_queue(company_id, priority=1, reason="test")

        size = test_db.get_enrichment_queue_size()
        assert size == 1

    def test_get_companies_for_enrichment(self, test_db, sample_company):
        """Priority ordering works."""
        # Insert companies with different priorities
        id1 = test_db.insert_company(company_number="HRB20001", name="Low Priority", source="test")
        id2 = test_db.insert_company(company_number="HRB20002", name="High Priority", source="test")

        test_db.add_to_enrichment_queue(id1, priority=5)  # Lower priority
        test_db.add_to_enrichment_queue(id2, priority=1)  # Higher priority

        companies = test_db.get_companies_for_enrichment(limit=2)
        assert len(companies) == 2
        assert companies[0]['company_number'] == "HRB20002"  # High priority first

    def test_mark_enriched_success(self, test_db, sample_company):
        """Status updated and removed from queue."""
        company_id = test_db.insert_company(**sample_company)
        test_db.add_to_enrichment_queue(company_id)

        test_db.mark_enriched(company_id, success=True)

        company = test_db.get_company(company_id)
        assert company['enrichment_status'] == 'enriched'

        queue_size = test_db.get_enrichment_queue_size()
        assert queue_size == 0

    def test_mark_enriched_failure(self, test_db, sample_company):
        """Failed status set correctly."""
        company_id = test_db.insert_company(**sample_company)
        test_db.add_to_enrichment_queue(company_id)

        test_db.mark_enriched(company_id, success=False)

        company = test_db.get_company(company_id)
        assert company['enrichment_status'] == 'failed'


class TestDatabaseChangeLog:
    """Test change log operations."""

    def test_log_change(self, test_db, sample_company):
        """Change recorded correctly."""
        company_id = test_db.insert_company(**sample_company)
        test_db.log_change(
            company_id,
            change_type="capital_increase",
            field_name="capital_amount",
            old_value="25000",
            new_value="50000",
        )

        changes = test_db.get_unnotified_changes()
        assert len(changes) == 1
        assert changes[0]['change_type'] == "capital_increase"

    def test_get_unnotified_changes(self, test_db, sample_company):
        """Filters notified=0."""
        company_id = test_db.insert_company(**sample_company)
        test_db.log_change(company_id, change_type="update")

        unnotified = test_db.get_unnotified_changes()
        assert len(unnotified) == 1

    def test_mark_changes_notified(self, test_db, sample_company):
        """Updates notified flag."""
        company_id = test_db.insert_company(**sample_company)
        test_db.log_change(company_id, change_type="update")

        changes = test_db.get_unnotified_changes()
        change_ids = [c['id'] for c in changes]

        test_db.mark_changes_notified(change_ids)

        unnotified = test_db.get_unnotified_changes()
        assert len(unnotified) == 0


class TestDatabaseAnnouncements:
    """Test announcement operations."""

    def test_insert_announcement(self, test_db):
        """Announcement stored correctly."""
        ann_id = test_db.insert_announcement(
            company_name="Test Company GmbH",
            announcement_type="neueintragung",
            announcement_date="2024-01-15",
        )
        assert ann_id > 0

    def test_get_announcements(self, test_db):
        """Announcements retrieved with filters."""
        test_db.insert_announcement(
            company_name="New Company GmbH",
            announcement_type="neueintragung",
            announcement_date="2024-01-15",
        )
        test_db.insert_announcement(
            company_name="Capital Company GmbH",
            announcement_type="kapitalerhoehung",
            announcement_date="2024-01-16",
        )

        all_ann = test_db.get_announcements()
        assert len(all_ann) == 2

        filtered = test_db.get_announcements(announcement_type="neueintragung")
        assert len(filtered) == 1
        assert filtered[0]['announcement_type'] == "neueintragung"

    def test_link_announcement_to_company(self, test_db, sample_company):
        """Link works correctly."""
        company_id = test_db.insert_company(**sample_company)
        ann_id = test_db.insert_announcement(
            company_name=sample_company['name'],
            announcement_type="neueintragung",
        )

        test_db.link_announcement_to_company(ann_id, company_id)

        announcements = test_db.get_announcements()
        assert announcements[0]['company_id'] == company_id

    def test_announcement_stats(self, test_db):
        """Grouped counts work."""
        test_db.insert_announcement(company_name="A", announcement_type="neueintragung")
        test_db.insert_announcement(company_name="B", announcement_type="neueintragung")
        test_db.insert_announcement(company_name="C", announcement_type="kapitalerhoehung")

        stats = test_db.get_announcement_stats()
        assert stats.get("neueintragung") == 2
        assert stats.get("kapitalerhoehung") == 1


class TestDatabaseTransactions:
    """Test transaction handling."""

    def test_transaction_rollback(self, test_db):
        """Rollback on error works (using raw SQL to avoid auto-commit)."""
        try:
            with test_db.transaction():
                # Use raw SQL to avoid insert_company's auto-commit
                cursor = test_db.conn.cursor()
                cursor.execute('''
                    INSERT INTO companies (company_number, name, source, first_seen_date, last_updated, enrichment_status)
                    VALUES (?, ?, ?, datetime('now'), datetime('now'), 'pending')
                ''', ("HRB30001", "Will Be Rolled Back", "test"))
                raise ValueError("Simulated error")
        except ValueError:
            pass

        # Company should not exist due to rollback
        result = test_db.get_company_by_number("HRB30001")
        assert result is None

    def test_transaction_commit(self, test_db):
        """Successful transaction commits."""
        with test_db.transaction():
            test_db.insert_company(
                company_number="HRB30002",
                name="Will Be Committed",
                source="test",
            )

        result = test_db.get_company_by_number("HRB30002")
        assert result is not None


class TestDatabaseJSONFields:
    """Test JSON field serialization."""

    def test_matched_keywords_stored_as_json(self, test_db):
        """matched_keywords stored as JSON string."""
        keywords = ["machine learning", "deep learning"]
        company_id = test_db.insert_company(
            company_number="HRB40001",
            name="JSON Test GmbH",
            source="test",
            matched_keywords=keywords,
        )

        result = test_db.get_company(company_id)
        # Should be stored as JSON string
        assert result['matched_keywords'] is not None
        parsed = json.loads(result['matched_keywords'])
        assert parsed == keywords

    def test_tech_categories_stored_as_json(self, test_db):
        """tech_categories stored as JSON string."""
        categories = ["ml_analytics", "robotics"]
        company_id = test_db.insert_company(
            company_number="HRB40002",
            name="Categories Test GmbH",
            source="test",
            tech_categories=categories,
        )

        result = test_db.get_company(company_id)
        parsed = json.loads(result['tech_categories'])
        assert parsed == categories


class TestDatabaseStatistics:
    """Test statistics and reporting."""

    def test_get_statistics(self, test_db_with_companies):
        """Returns all stat categories."""
        stats = test_db_with_companies.get_statistics()

        assert 'total_companies' in stats
        assert 'companies_by_source' in stats
        assert 'companies_by_enrichment' in stats
        assert 'total_officers' in stats
        assert 'total_capital_events' in stats
        assert 'enrichment_queue_size' in stats

        assert stats['total_companies'] >= 3


class TestDatabaseForeignKeys:
    """Test foreign key relationships."""

    def test_cascade_delete_officers(self, test_db, sample_company, sample_officer):
        """Officers deleted when company deleted."""
        company_id = test_db.insert_company(**sample_company)
        test_db.insert_officer(company_id, **sample_officer)

        # Delete company directly via SQL
        cursor = test_db.conn.cursor()
        cursor.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        test_db.conn.commit()

        # Officers should be cascade deleted
        officers = test_db.get_officers(company_id)
        assert len(officers) == 0

    def test_cascade_delete_capital_events(self, test_db, sample_company, sample_capital_event):
        """Capital events deleted when company deleted."""
        company_id = test_db.insert_company(**sample_company)
        test_db.insert_capital_event(company_id, **sample_capital_event)

        cursor = test_db.conn.cursor()
        cursor.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        test_db.conn.commit()

        events = test_db.get_capital_events(company_id)
        assert len(events) == 0
