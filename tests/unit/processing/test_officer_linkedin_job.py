"""
Tests for officer LinkedIn enrichment job.

Tests state persistence, run_once/run_batch behavior,
rate limit handling, and failed officer skipping.
All search calls are mocked — no actual HTTP requests.
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from processing.officer_linkedin_search import OfficerLinkedInMatch, RateLimitedError
from scheduler.jobs.officer_linkedin_job import OfficerLinkedInEnrichmentJob

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_db():
    """Mock database with basic officer/company data."""
    db = MagicMock()
    db.get_officers_for_linkedin_enrichment.return_value = [
        {
            "id": 1,
            "name": "Max Mustermann",
            "role": "Geschäftsführer",
            "company_id": 100,
            "company_name": "TechBot GmbH",
            "company_city": "Berlin",
            "ai_robotics_score": 5,
            "startup_score": 3,
            "is_current": 1,
            "linkedin_enriched_at": None,
        },
        {
            "id": 2,
            "name": "Anna Schmidt",
            "role": "Geschäftsführerin",
            "company_id": 101,
            "company_name": "RoboVision AG",
            "company_city": "Munich",
            "ai_robotics_score": 4,
            "startup_score": 2,
            "is_current": 1,
            "linkedin_enriched_at": None,
        },
    ]
    return db


@pytest.fixture
def state_file(tmp_path):
    """Temporary state file path."""
    return str(tmp_path / "test_officer_linkedin_state.json")


@pytest.fixture
def job(mock_db, state_file):
    """Job instance with mocked DB and temp state file."""
    return OfficerLinkedInEnrichmentJob(
        db=mock_db,
        state_file=state_file,
        search_delay=0,  # No delay in tests
        jitter=0,
    )


def _make_match(name="Max Mustermann", confidence=0.85):
    """Helper to create a mock OfficerLinkedInMatch."""
    return OfficerLinkedInMatch(
        linkedin_url=f"https://linkedin.com/in/{name.lower().replace(' ', '-')}",
        name_from_search=name,
        headline="CEO at TechBot",
        location="Berlin, Germany",
        snippet="Experienced tech leader",
        previous_companies=["Google", "N26"],
        match_confidence=confidence,
        source="search_snippet",
    )


# ============================================================================
# State Persistence Tests
# ============================================================================


class TestStatePersistence:
    """Test state save/load behavior."""

    def test_state_defaults(self, job):
        """Fresh job has correct default state."""
        assert job.state["total_searches"] == 0
        assert job.state["total_enriched"] == 0
        assert job.state["total_no_match"] == 0
        assert job.state["last_search_at"] is None
        assert job.state["failed_officer_ids"] == []

    def test_state_saves_and_loads(self, mock_db, state_file):
        """State persists across job instances."""
        job1 = OfficerLinkedInEnrichmentJob(
            db=mock_db,
            state_file=state_file,
            search_delay=0,
            jitter=0,
        )
        job1.state["total_searches"] = 42
        job1.state["total_enriched"] = 10
        job1._save_state()

        # New instance reads persisted state
        job2 = OfficerLinkedInEnrichmentJob(
            db=mock_db,
            state_file=state_file,
            search_delay=0,
            jitter=0,
        )
        assert job2.state["total_searches"] == 42
        assert job2.state["total_enriched"] == 10

    def test_state_file_created(self, job, state_file):
        """State file is created on save."""
        assert not os.path.exists(state_file)
        job._save_state()
        assert os.path.exists(state_file)

        with open(state_file) as f:
            data = json.load(f)
        assert "total_searches" in data


# ============================================================================
# run_once Tests
# ============================================================================


class TestRunOnce:
    """Test single officer enrichment."""

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_with_match(self, mock_search, job, mock_db):
        """Successful match → DB updated, stats correct."""
        match = _make_match()
        mock_search.return_value = match

        stats = job.run_once()

        assert stats["action"] == "search"
        assert stats["success"] is True
        assert stats["details"]["linkedin_url"] == match.linkedin_url
        assert stats["details"]["confidence"] == 0.85

        # Verify DB was updated
        mock_db.update_officer_linkedin.assert_called_once()
        call_kwargs = mock_db.update_officer_linkedin.call_args
        assert call_kwargs[0][0] == 1  # officer_id
        assert call_kwargs[1]["linkedin_url"] == match.linkedin_url
        assert call_kwargs[1]["linkedin_match_confidence"] == 0.85

        # State updated
        assert job.state["total_enriched"] == 1
        assert job.state["total_searches"] == 1

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_no_match(self, mock_search, job, mock_db):
        """No match found → marked as no_match."""
        mock_search.return_value = None

        stats = job.run_once()

        assert stats["action"] == "search"
        assert stats["success"] is False
        assert stats["details"]["reason"] == "no_confident_match"

        # DB marked as no_match
        mock_db.update_officer_linkedin.assert_called_once()
        call_kwargs = mock_db.update_officer_linkedin.call_args
        assert call_kwargs[1]["linkedin_enrichment_source"] == "no_match"
        assert call_kwargs[1]["linkedin_match_confidence"] == 0.0

        # State updated
        assert job.state["total_no_match"] == 1
        assert job.state["total_searches"] == 1

    def test_no_officers_idle(self, job, mock_db):
        """No officers to enrich → action='idle'."""
        mock_db.get_officers_for_linkedin_enrichment.return_value = []

        stats = job.run_once()

        assert stats["action"] == "idle"
        assert stats["details"]["reason"] == "no_officers_to_enrich"

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_rate_limited(self, mock_search, job, mock_db):
        """RateLimitedError → action='rate_limited', officer NOT failed."""
        mock_search.side_effect = RateLimitedError("DDG 202")

        stats = job.run_once()

        assert stats["action"] == "rate_limited"
        assert stats["details"]["error"] == "rate_limited"

        # Officer should NOT be in failed list (we want to retry later)
        assert 1 not in job.state.get("failed_officer_ids", [])

        # DB should NOT be updated (officer not marked)
        mock_db.update_officer_linkedin.assert_not_called()

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_generic_error(self, mock_search, job, mock_db):
        """Generic exception → officer added to failed list."""
        mock_search.side_effect = Exception("Network error")

        stats = job.run_once()

        assert stats["action"] == "search"
        assert stats["success"] is False
        assert "error" in stats["details"]

        # Officer should be in failed list
        assert 1 in job.state["failed_officer_ids"]


# ============================================================================
# run_batch Tests
# ============================================================================


class TestRunBatch:
    """Test batch enrichment behavior."""

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_stops_on_consecutive_rate_limits(self, mock_search, job, mock_db):
        """Batch stops after 2 consecutive rate limits."""
        mock_search.side_effect = RateLimitedError("DDG 202")

        batch_stats = job.run_batch(batch_size=5)

        # Should stop after 2 rate limits, not try all 5
        assert batch_stats["errors"] >= 2
        assert mock_search.call_count <= 3  # At most 3 attempts (2 rate limits + stop)

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_resets_rate_limit_counter_on_success(self, mock_search, job, mock_db):
        """Success between rate limits resets the consecutive counter."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RateLimitedError("DDG 202")
            elif call_count[0] == 2:
                return _make_match()  # Success
            elif call_count[0] == 3:
                raise RateLimitedError("DDG 202")
            elif call_count[0] == 4:
                return _make_match()  # Success
            return None

        mock_search.side_effect = side_effect

        batch_stats = job.run_batch(batch_size=5)

        # Should process more than 2 since counter resets
        assert batch_stats["officers_enriched"] >= 1

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_batch_processes_all(self, mock_search, job, mock_db):
        """Batch processes batch_size officers when all succeed."""
        mock_search.return_value = _make_match()

        batch_stats = job.run_batch(batch_size=2)

        assert batch_stats["officers_processed"] == 2
        assert batch_stats["officers_enriched"] == 2


# ============================================================================
# Failed Officer Skipping Tests
# ============================================================================


class TestFailedOfficerSkipping:
    """Test that previously failed officers are skipped."""

    @patch("processing.officer_linkedin_search.search_officer_linkedin")
    def test_skips_failed_officers(self, mock_search, job, mock_db):
        """Officers in failed_officer_ids are skipped."""
        # Mark officer 1 as failed
        job.state["failed_officer_ids"] = [1]

        mock_search.return_value = _make_match(name="Anna Schmidt")

        stats = job.run_once()

        # Should have searched for officer 2 (Anna Schmidt), not officer 1
        assert stats["action"] == "search"
        assert stats["details"]["officer"] == "Anna Schmidt"

    def test_all_officers_failed(self, job, mock_db):
        """If all officers are in failed list, returns idle."""
        job.state["failed_officer_ids"] = [1, 2]

        stats = job.run_once()

        assert stats["action"] == "idle"


# ============================================================================
# get_stats Tests
# ============================================================================


class TestGetStats:
    """Test statistics reporting."""

    def test_stats_returns_dict(self, job, mock_db):
        """get_stats returns a dict with expected keys."""
        # Mock the cursor queries
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [(5,), (3,), (10,)]
        mock_db.conn.cursor.return_value = mock_cursor

        stats = job.get_stats()

        assert "total_attempted" in stats
        assert "with_linkedin_url" in stats
        assert "remaining" in stats
        assert "total_searches" in stats
        assert "total_enriched" in stats
        assert "failed_count" in stats
