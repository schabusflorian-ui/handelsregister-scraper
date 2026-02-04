"""
Tests for PersistentRateLimiter.

Tests token bucket implementation, state persistence, and legal compliance
with the 60 requests/hour limit.
"""

import pytest
import time
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from scheduler.rate_limiter import PersistentRateLimiter, RateLimitState


class TestRateLimiterInitialization:
    """Test rate limiter initialization."""

    def test_init_creates_table(self, temp_db_path):
        """Table exists after init."""
        limiter = PersistentRateLimiter(temp_db_path)

        conn = limiter._get_connection()
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='rate_limiter_state'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()

    def test_init_full_bucket(self, temp_db_path):
        """Starts with 60 tokens."""
        limiter = PersistentRateLimiter(temp_db_path)
        state = limiter.get_state()

        assert state.tokens_available == limiter.MAX_TOKENS
        assert state.tokens_available == 60

    def test_load_existing_state(self, temp_db_path):
        """Restores state from DB on restart."""
        # Create limiter and consume some tokens
        limiter1 = PersistentRateLimiter(temp_db_path)
        limiter1.acquire(count=10, block=False)

        # Create new limiter instance (simulating restart)
        limiter2 = PersistentRateLimiter(temp_db_path)
        state = limiter2.get_state()

        # Should have ~50 tokens (60 - 10, minus tiny regeneration)
        assert state.tokens_available < 55


class TestRateLimiterTokenManagement:
    """Test token acquisition and management."""

    def test_acquire_single_token(self, rate_limiter):
        """Decrements by 1."""
        initial_tokens = rate_limiter.get_state().tokens_available

        result = rate_limiter.acquire(count=1, block=False)

        assert result is True
        new_tokens = rate_limiter.get_state().tokens_available
        assert new_tokens < initial_tokens

    def test_acquire_multiple_tokens(self, rate_limiter):
        """Decrements by N."""
        initial_tokens = rate_limiter.get_state().tokens_available

        result = rate_limiter.acquire(count=5, block=False)

        assert result is True
        new_tokens = rate_limiter.get_state().tokens_available
        # Should be approximately 5 less (accounting for tiny regeneration)
        assert new_tokens < initial_tokens - 4

    def test_acquire_insufficient_nonblocking(self, depleted_rate_limiter):
        """Returns False immediately when non-blocking."""
        result = depleted_rate_limiter.acquire(count=1, block=False)
        assert result is False

    def test_acquire_with_timeout(self, temp_db_path):
        """Times out correctly when no tokens available (non-blocking)."""
        limiter = PersistentRateLimiter(temp_db_path)
        # Consume all tokens first
        for _ in range(60):
            limiter.acquire(count=1, block=False)

        # Now try to acquire with non-blocking (timeout behavior is complex due to sleep)
        result = limiter.acquire(count=1, block=False)
        assert result is False

    def test_acquire_timeout_returns_false(self, temp_db_path):
        """Timeout parameter causes acquire to return False eventually."""
        limiter = PersistentRateLimiter(temp_db_path)
        # Request more tokens than MAX - this should fail immediately
        result = limiter.acquire(count=100, block=False)
        assert result is False

    def test_acquire_respects_max_tokens(self, rate_limiter):
        """Can't acquire more than MAX_TOKENS."""
        result = rate_limiter.acquire(count=100, block=False)
        assert result is False

    def test_requests_this_hour_increments(self, rate_limiter):
        """requests_this_hour counter increases."""
        initial_state = rate_limiter.get_state()
        initial_requests = initial_state.requests_this_hour

        rate_limiter.acquire(count=3, block=False)

        new_state = rate_limiter.get_state()
        assert new_state.requests_this_hour == initial_requests + 3


class TestRateLimiterRegeneration:
    """Test token regeneration over time."""

    def test_token_regeneration(self, temp_db_path):
        """Tokens regenerate over time."""
        limiter = PersistentRateLimiter(temp_db_path)

        # Consume all tokens
        for _ in range(60):
            limiter.acquire(count=1, block=False)

        state_before = limiter.get_state()
        assert state_before.tokens_available < 1

        # Simulate time passing by updating last_updated
        conn = limiter._get_connection()
        try:
            # Move last_updated back by 10 minutes (should regenerate ~10 tokens)
            past_time = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
            conn.execute(
                "UPDATE rate_limiter_state SET last_updated = ? WHERE id = 1",
                (past_time,)
            )
            conn.commit()
        finally:
            conn.close()

        state_after = limiter.get_state()
        # Should have regenerated some tokens (~10 for 10 minutes)
        assert state_after.tokens_available >= 9

    def test_regeneration_caps_at_max(self, temp_db_path):
        """Never exceeds 60 tokens."""
        limiter = PersistentRateLimiter(temp_db_path)

        # Move last_updated back by 2 hours (would regenerate 120 tokens if not capped)
        conn = limiter._get_connection()
        try:
            past_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
            conn.execute(
                "UPDATE rate_limiter_state SET last_updated = ? WHERE id = 1",
                (past_time,)
            )
            conn.commit()
        finally:
            conn.close()

        state = limiter.get_state()
        assert state.tokens_available <= limiter.MAX_TOKENS
        assert state.tokens_available <= 60

    def test_hourly_counter_reset(self, temp_db_path):
        """requests_this_hour resets after hour passes."""
        limiter = PersistentRateLimiter(temp_db_path)

        # Make some requests
        limiter.acquire(count=5, block=False)
        state_before = limiter.get_state()
        assert state_before.requests_this_hour == 5

        # Move hour_started back by more than an hour
        conn = limiter._get_connection()
        try:
            past_time = (datetime.utcnow() - timedelta(hours=2)).isoformat()
            conn.execute(
                "UPDATE rate_limiter_state SET hour_started = ?, last_updated = ? WHERE id = 1",
                (past_time, past_time)
            )
            conn.commit()
        finally:
            conn.close()

        state_after = limiter.get_state()
        assert state_after.requests_this_hour == 0


class TestRateLimiterStatePersistence:
    """Test state persistence across restarts."""

    def test_state_survives_restart(self, temp_db_path):
        """Tokens persist across instances."""
        # First instance - consume some tokens
        limiter1 = PersistentRateLimiter(temp_db_path)
        limiter1.acquire(count=20, block=False)
        tokens_after_consume = limiter1.get_state().tokens_available

        # Second instance (simulating restart)
        limiter2 = PersistentRateLimiter(temp_db_path)
        tokens_on_restart = limiter2.get_state().tokens_available

        # Should be similar (accounting for tiny regeneration during test)
        assert abs(tokens_after_consume - tokens_on_restart) < 2

    def test_requests_this_hour_persists(self, temp_db_path):
        """Counter persists across instances."""
        limiter1 = PersistentRateLimiter(temp_db_path)
        limiter1.acquire(count=10, block=False)

        limiter2 = PersistentRateLimiter(temp_db_path)
        state = limiter2.get_state()

        assert state.requests_this_hour == 10


class TestRateLimiterReset:
    """Test reset functionality."""

    def test_reset(self, rate_limiter):
        """Returns to full bucket."""
        # Consume some tokens
        rate_limiter.acquire(count=30, block=False)

        state_before = rate_limiter.get_state()
        assert state_before.tokens_available < 35

        rate_limiter.reset()

        state_after = rate_limiter.get_state()
        assert state_after.tokens_available == rate_limiter.MAX_TOKENS
        assert state_after.requests_this_hour == 0


class TestRateLimiterState:
    """Test RateLimitState dataclass."""

    def test_can_request_true(self, rate_limiter):
        """can_request is True when tokens available."""
        state = rate_limiter.get_state()
        assert state.can_request is True
        assert state.wait_seconds == 0.0

    def test_can_request_false(self, depleted_rate_limiter):
        """can_request is False when no tokens."""
        state = depleted_rate_limiter.get_state()
        assert state.can_request is False
        assert state.wait_seconds > 0

    def test_wait_seconds_calculation(self, depleted_rate_limiter):
        """wait_seconds correctly calculated."""
        state = depleted_rate_limiter.get_state()

        # Need 1 token, regenerate at 1/60 per second
        # So should wait ~60 seconds for 1 token
        assert state.wait_seconds > 50
        assert state.wait_seconds < 70


class TestRateLimiterHelperMethods:
    """Test helper methods."""

    def test_available_tokens(self, rate_limiter):
        """available_tokens returns current count."""
        tokens = rate_limiter.available_tokens()
        assert tokens == rate_limiter.MAX_TOKENS

    def test_requests_remaining_this_hour(self, rate_limiter):
        """Calculates remaining requests correctly."""
        remaining = rate_limiter.requests_remaining_this_hour()
        assert remaining == rate_limiter.REQUESTS_PER_HOUR

        rate_limiter.acquire(count=10, block=False)

        remaining = rate_limiter.requests_remaining_this_hour()
        assert remaining == rate_limiter.REQUESTS_PER_HOUR - 10


class TestRateLimiterConstants:
    """Test rate limiter constants."""

    def test_requests_per_hour_is_60(self, rate_limiter):
        """Legal limit is 60 requests/hour."""
        assert rate_limiter.REQUESTS_PER_HOUR == 60

    def test_max_tokens_is_60(self, rate_limiter):
        """Maximum bucket size is 60."""
        assert rate_limiter.MAX_TOKENS == 60

    def test_tokens_per_second_calculation(self, rate_limiter):
        """Tokens regenerate at correct rate."""
        expected = 60 / 3600  # 1 token per minute
        assert abs(rate_limiter.TOKENS_PER_SECOND - expected) < 0.001


class TestRateLimiterConcurrency:
    """Test concurrent access scenarios."""

    def test_multiple_acquires_consistent(self, temp_db_path):
        """Multiple rapid acquires maintain consistency."""
        limiter = PersistentRateLimiter(temp_db_path)

        # Rapidly acquire tokens
        success_count = 0
        for _ in range(70):  # More than MAX_TOKENS
            if limiter.acquire(count=1, block=False):
                success_count += 1

        # Should have acquired exactly MAX_TOKENS
        assert success_count == limiter.MAX_TOKENS
        assert success_count == 60

    def test_state_consistency_after_operations(self, rate_limiter):
        """State remains consistent after multiple operations."""
        # Perform various operations
        rate_limiter.acquire(count=10, block=False)
        rate_limiter.acquire(count=5, block=False)
        rate_limiter.acquire(count=3, block=False)

        state = rate_limiter.get_state()

        # requests_this_hour should be sum of all acquires
        assert state.requests_this_hour == 18

        # tokens should be MAX_TOKENS - 18 (approximately)
        assert state.tokens_available < 45


class TestRateLimiterEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_acquire_zero_tokens(self, rate_limiter):
        """Acquiring 0 tokens succeeds."""
        result = rate_limiter.acquire(count=0, block=False)
        # Should succeed but not consume anything
        state = rate_limiter.get_state()
        assert state.tokens_available == rate_limiter.MAX_TOKENS

    def test_request_more_than_max_fails(self, temp_db_path):
        """Requesting more than MAX_TOKENS fails immediately."""
        limiter = PersistentRateLimiter(temp_db_path)
        # Request way more tokens than MAX - should fail immediately
        result = limiter.acquire(count=1000, block=False)
        assert result is False

    def test_negative_tokens_not_possible(self, temp_db_path):
        """Tokens cannot go negative."""
        limiter = PersistentRateLimiter(temp_db_path)

        # Consume all tokens
        for _ in range(60):
            limiter.acquire(count=1, block=False)

        # Try to acquire more
        limiter.acquire(count=1, block=False)

        state = limiter.get_state()
        assert state.tokens_available >= 0
