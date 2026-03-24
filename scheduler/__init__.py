"""
Scheduler module for continuous Handelsregister monitoring.

This module provides:
- Persistent rate limiting (60 requests/hour legal limit)
- Background job scheduling with APScheduler
- Discovery jobs for new company monitoring
- Backfill jobs for historical data collection
"""

from .rate_limiter import PersistentRateLimiter
from .scheduler import HandelsregisterScheduler

__all__ = ["PersistentRateLimiter", "HandelsregisterScheduler"]
