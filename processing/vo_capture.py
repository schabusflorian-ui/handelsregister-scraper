"""
Capture a newly discovered company's Veröffentlichungen (VÖ) so we can
populate `first_registered_date` at the moment of discovery.

Called by discovery_job, backfill_job, and registration_scan_job right after
they insert a new company. The portal's session from the enclosing `search()`
call is still live, so `fetch_announcements()` works here without a new
session handshake.

Cost: 1 rate-limit token per invocation — callers must already have budget.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from persistence.database import Database
    from scheduler.rate_limiter import PersistentRateLimiter
    from sources.bundesapi import BundesAPISource, SearchResult

logger = logging.getLogger(__name__)

_OFFICER_TYPES = {"geschaeftsfuehrer", "neueintragung", "prokura"}


def capture_neueintragung(
    db: "Database",
    source: "BundesAPISource",
    company_id: int,
    search_result: "SearchResult",
    rate_limiter: Optional["PersistentRateLimiter"] = None,
) -> bool:
    """
    Fetch VÖ for a company we just inserted and:
      * persist every announcement, linked to company_id
      * extract officers from relevant announcement types
      * pull first_registered_date (+ purpose, capital, address) from the
        Neueintragung announcement and backfill the company row

    Args:
        db: open Database
        source: the BundesAPISource that produced search_result — must still
            hold the live search-results session
        company_id: freshly inserted company id
        search_result: the SearchResult with a valid row_index
        rate_limiter: optional shared limiter; if provided, one token is
            consumed before the VÖ fetch. If the limiter has no budget we
            return False without calling the portal.

    Returns:
        True iff we stored a non-empty first_registered_date.
    """
    if search_result is None or search_result.row_index is None:
        return False

    if rate_limiter is not None:
        if not rate_limiter.acquire(count=1, block=False):
            logger.debug("VÖ capture skipped — rate limiter empty")
            return False

    try:
        announcements = source.fetch_announcements(search_result)
    except Exception as e:  # noqa: BLE001 — the portal throws a lot
        logger.debug("VÖ fetch failed for %s: %s", search_result.name, e)
        return False

    if not announcements:
        return False

    # Persist every announcement (gives the announcement_job pipeline real
    # neueintragung rows linked to company_id — currently 0 on Railway).
    for ann in announcements:
        try:
            ann_id = db.insert_announcement(
                company_id=company_id,
                company_name=ann.company_name,
                native_company_number=ann.native_company_number,
                announcement_type=ann.announcement_type,
                announcement_date=ann.announcement_date,
                state=ann.state,
                registry_type=ann.registry_type,
                text=ann.text,
                capital_old=ann.capital_old,
                capital_new=ann.capital_new,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("insert_announcement failed: %s", e)
            continue

        if ann.announcement_type in _OFFICER_TYPES and ann.text:
            try:
                from processing.officer_extractor import (
                    extract_officers_from_text,
                    persist_officers,
                )

                officers = extract_officers_from_text(ann.text)
                if officers:
                    persist_officers(db, company_id, officers, ann.announcement_date)
                db.mark_announcement_processed(ann_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("officer extraction failed for ann %d: %s", ann_id, e)

    # Neueintragung → backfill company row
    neu = next(
        (a for a in announcements if a.announcement_type == "neueintragung"),
        None,
    )
    if neu is None:
        return False

    updates: dict[str, Any] = {}
    if neu.announcement_date:
        updates["first_registered_date"] = neu.announcement_date
    if neu.purpose:
        updates["purpose"] = neu.purpose
    if neu.capital_new:
        updates["capital_amount"] = neu.capital_new
    if neu.postal_code:
        updates["postal_code"] = neu.postal_code
    if neu.street:
        updates["street"] = neu.street
    rep = getattr(neu, "representation_rules", None)
    if rep:
        updates["representation_rules"] = rep

    if updates:
        try:
            db.update_company(company_id, **updates)
            logger.info(
                "Captured Neueintragung for %s: first_registered_date=%s",
                search_result.name,
                updates.get("first_registered_date"),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("update_company failed: %s", e)
            return False

    return bool(updates.get("first_registered_date"))
