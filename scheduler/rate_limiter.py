"""
Persistent Token Bucket Rate Limiter

Implements rate limiting that persists across restarts, ensuring
compliance with the 60 requests/hour legal limit (§303a, b StGB).

The state is stored in SQLite so that:
1. Rate limit state survives scheduler restarts
2. Multiple processes can share the same rate limit
3. We never exceed the legal limit even after crashes
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Current state of the rate limiter."""

    tokens_available: float
    last_updated: datetime
    requests_this_hour: int
    can_request: bool
    wait_seconds: float


class PersistentRateLimiter:
    """
    Token bucket rate limiter with SQLite persistence.

    Legal limit: 60 requests per hour for handelsregister.de

    Implementation:
    - Tokens regenerate at rate of 60/hour (1 per minute)
    - Maximum bucket size: 60 tokens (1 hour of requests)
    - State persisted to SQLite for crash recovery
    """

    # Legal limit per §303a, b StGB
    REQUESTS_PER_HOUR = 60
    TOKENS_PER_SECOND = REQUESTS_PER_HOUR / 3600  # ~0.0167 tokens/sec
    MAX_TOKENS = 60  # 1 hour worth of requests

    def __init__(self, db_path: str):
        """
        Initialize rate limiter with SQLite backend.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_table()
        self._load_or_initialize_state()

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_table(self):
        """Create rate limiter table if it doesn't exist."""
        conn = self._get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_limiter_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    tokens_available REAL NOT NULL,
                    last_updated TEXT NOT NULL,
                    requests_this_hour INTEGER DEFAULT 0,
                    hour_started TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _load_or_initialize_state(self):
        """Load existing state or initialize fresh state."""
        conn = self._get_connection()
        try:
            row = conn.execute("SELECT * FROM rate_limiter_state WHERE id = 1").fetchone()

            if row is None:
                # Initialize with full bucket
                now = datetime.utcnow()
                conn.execute(
                    """
                    INSERT INTO rate_limiter_state
                    (id, tokens_available, last_updated, requests_this_hour, hour_started)
                    VALUES (1, ?, ?, 0, ?)
                """,
                    (self.MAX_TOKENS, now.isoformat(), now.isoformat()),
                )
                conn.commit()
                logger.info("Initialized rate limiter with %d tokens", self.MAX_TOKENS)
            else:
                logger.info(
                    "Loaded rate limiter state: %.1f tokens, %d requests this hour",
                    row["tokens_available"],
                    row["requests_this_hour"],
                )
        finally:
            conn.close()

    def _update_tokens(self, conn: sqlite3.Connection) -> Tuple[float, int]:
        """
        Update token count based on elapsed time.

        Returns:
            Tuple of (current_tokens, requests_this_hour)
        """
        row = conn.execute("SELECT * FROM rate_limiter_state WHERE id = 1").fetchone()

        last_updated = datetime.fromisoformat(row["last_updated"])
        hour_started = datetime.fromisoformat(row["hour_started"]) if row["hour_started"] else last_updated
        now = datetime.utcnow()

        # Calculate tokens regenerated since last update
        elapsed_seconds = (now - last_updated).total_seconds()
        tokens_regenerated = elapsed_seconds * self.TOKENS_PER_SECOND

        # Cap at maximum
        current_tokens = min(row["tokens_available"] + tokens_regenerated, self.MAX_TOKENS)

        # Reset hourly counter if hour has passed
        requests_this_hour = row["requests_this_hour"]
        if (now - hour_started).total_seconds() >= 3600:
            requests_this_hour = 0
            hour_started = now

        # Update state
        conn.execute(
            """
            UPDATE rate_limiter_state
            SET tokens_available = ?,
                last_updated = ?,
                requests_this_hour = ?,
                hour_started = ?
            WHERE id = 1
        """,
            (current_tokens, now.isoformat(), requests_this_hour, hour_started.isoformat()),
        )

        return current_tokens, requests_this_hour

    def get_state(self) -> RateLimitState:
        """
        Get current rate limiter state.

        Returns:
            RateLimitState with current token count and wait time
        """
        conn = self._get_connection()
        try:
            tokens, requests_this_hour = self._update_tokens(conn)
            conn.commit()

            can_request = tokens >= 1.0
            wait_seconds = 0.0
            if not can_request:
                # Calculate time until 1 token is available
                tokens_needed = 1.0 - tokens
                wait_seconds = tokens_needed / self.TOKENS_PER_SECOND

            return RateLimitState(
                tokens_available=tokens,
                last_updated=datetime.utcnow(),
                requests_this_hour=requests_this_hour,
                can_request=can_request,
                wait_seconds=wait_seconds,
            )
        finally:
            conn.close()

    def acquire(self, count: int = 1, block: bool = True, timeout: Optional[float] = None) -> bool:
        """
        Acquire tokens for making requests.

        Args:
            count: Number of tokens to acquire (default 1)
            block: If True, wait for tokens to become available
            timeout: Maximum time to wait in seconds (None = wait forever)

        Returns:
            True if tokens were acquired, False if timed out
        """
        start_time = time.time()

        while True:
            conn = self._get_connection()
            try:
                tokens, requests_this_hour = self._update_tokens(conn)

                if tokens >= count:
                    # Consume tokens
                    conn.execute(
                        """
                        UPDATE rate_limiter_state
                        SET tokens_available = tokens_available - ?,
                            requests_this_hour = requests_this_hour + ?
                        WHERE id = 1
                    """,
                        (count, count),
                    )
                    conn.commit()

                    logger.debug("Acquired %d token(s), %.1f remaining", count, tokens - count)
                    return True

                conn.commit()
            finally:
                conn.close()

            if not block:
                return False

            # Check timeout
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    logger.warning("Timed out waiting for rate limit tokens")
                    return False

            # Calculate wait time
            tokens_needed = count - tokens
            wait_time = tokens_needed / self.TOKENS_PER_SECOND

            # Add small buffer to avoid tight loops
            wait_time = min(wait_time + 0.5, 60)

            logger.info("Rate limited - waiting %.1f seconds for tokens (have %.1f, need %d)", wait_time, tokens, count)
            time.sleep(wait_time)

    def available_tokens(self) -> float:
        """Get current number of available tokens."""
        return self.get_state().tokens_available

    def requests_remaining_this_hour(self) -> int:
        """Get estimated requests remaining this hour."""
        state = self.get_state()
        return self.REQUESTS_PER_HOUR - state.requests_this_hour

    def reset(self):
        """Reset rate limiter to full tokens (for testing only)."""
        conn = self._get_connection()
        try:
            now = datetime.utcnow()
            conn.execute(
                """
                UPDATE rate_limiter_state
                SET tokens_available = ?,
                    last_updated = ?,
                    requests_this_hour = 0,
                    hour_started = ?
                WHERE id = 1
            """,
                (self.MAX_TOKENS, now.isoformat(), now.isoformat()),
            )
            conn.commit()
            logger.info("Rate limiter reset to %d tokens", self.MAX_TOKENS)
        finally:
            conn.close()


# Convenience function for command-line status
def print_rate_limit_status(db_path: str):
    """Print current rate limit status."""
    limiter = PersistentRateLimiter(db_path)
    state = limiter.get_state()

    print("Rate Limiter Status")
    print("=" * 40)
    print(f"Tokens available: {state.tokens_available:.1f} / {limiter.MAX_TOKENS}")
    print(f"Requests this hour: {state.requests_this_hour} / {limiter.REQUESTS_PER_HOUR}")
    print(f"Can make request: {'Yes' if state.can_request else 'No'}")
    if not state.can_request:
        print(f"Wait time: {state.wait_seconds:.1f} seconds")
