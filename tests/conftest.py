"""
Shared pytest fixtures for Handelsregister scraper tests.

Provides:
- Temporary database fixtures
- Filter instance fixtures
- Rate limiter fixtures
- Sample data helpers
"""

import os
import sys
import pytest
import tempfile
import sqlite3
from pathlib import Path

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from persistence.database import Database
from processing.filters import AIRoboticsFilter, FilterConfig
from scheduler.rate_limiter import PersistentRateLimiter


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture
def temp_db_path(tmp_path):
    """Provide a temporary SQLite database path."""
    return str(tmp_path / "test_handelsregister.db")


@pytest.fixture
def test_db(temp_db_path):
    """Provide an initialized Database instance with temp path."""
    db = Database(temp_db_path)
    yield db
    db.close()


@pytest.fixture
def test_db_with_companies(test_db):
    """Database pre-populated with sample companies."""
    from tests.fixtures.sample_data import SAMPLE_COMPANIES_DB

    for company_data in SAMPLE_COMPANIES_DB:
        test_db.insert_company(**company_data)

    yield test_db


@pytest.fixture
def memory_db():
    """In-memory database for fast tests."""
    db = Database(":memory:")
    yield db
    db.close()


# ============================================================================
# Filter Fixtures
# ============================================================================

@pytest.fixture
def filter_instance():
    """Default AIRoboticsFilter instance."""
    return AIRoboticsFilter()


@pytest.fixture
def strict_filter():
    """Filter with higher minimum score requirement."""
    config = FilterConfig(min_relevance_score=3)
    return AIRoboticsFilter(config)


@pytest.fixture
def lenient_filter():
    """Filter with lower minimum score requirement."""
    config = FilterConfig(min_relevance_score=1)
    return AIRoboticsFilter(config)


# ============================================================================
# Rate Limiter Fixtures
# ============================================================================

@pytest.fixture
def rate_limiter(temp_db_path):
    """Fresh rate limiter with full tokens."""
    limiter = PersistentRateLimiter(temp_db_path)
    limiter.reset()  # Ensure full bucket
    return limiter


@pytest.fixture
def depleted_rate_limiter(temp_db_path):
    """Rate limiter with no tokens available."""
    limiter = PersistentRateLimiter(temp_db_path)
    # Consume all tokens
    conn = limiter._get_connection()
    try:
        conn.execute("""
            UPDATE rate_limiter_state
            SET tokens_available = 0.0,
                requests_this_hour = 60
            WHERE id = 1
        """)
        conn.commit()
    finally:
        conn.close()
    return limiter


# ============================================================================
# Helper Fixtures
# ============================================================================

@pytest.fixture
def sample_company():
    """Return a sample company dict."""
    return {
        "company_number": "HRB99999",
        "name": "Test AI Company GmbH",
        "source": "test",
        "city": "Berlin",
        "ai_robotics_score": 5,
        "current_status": "active",
        "capital_amount": 25000.0,
    }


@pytest.fixture
def sample_officer():
    """Return a sample officer dict."""
    return {
        "name": "Max Mustermann",
        "role": "Geschäftsführer",
        "is_current": True,
    }


@pytest.fixture
def sample_capital_event():
    """Return a sample capital event dict."""
    return {
        "event_type": "increase",
        "event_date": "2024-01-15",
        "previous_amount": 25000.0,
        "new_amount": 100000.0,
        "change_amount": 75000.0,
        "currency": "EUR",
        "confidence_score": 0.9,
    }
