"""
AD (Abdruck) PDF capture and parsing.

The Handelsregister portal exposes two useful company-level resources:

  VÖ (Veröffentlichungen) — listing of public announcements (löschung,
      umwandlung, einreichung, sonstiges). Does NOT include Stammkapital
      or Gegenstand body text for new-registration entries.

  AD (Abdruck)             — the structured register excerpt, delivered
      as a PDF. Contains every field we actually need for profiling a
      company: Firma, Sitz, Geschäftsanschrift, Gegenstand, Stammkapital,
      Vertretungsregelung, eingetragen am.

This module drives the AD path: fetch the PDF, extract text with pypdf,
parse it with Handelsregister-specific regexes, and backfill the company
row. Budget: 1 request per company (the AD POST).

Usage:
    from processing.ad_capture import capture_ad_for_company
    capture_ad_for_company(db, source, company_id, search_result, rate_limiter)

`source` must be the same BundesAPISource that produced `search_result` —
AD fetching requires the search results session to still be live (same
constraint as `fetch_announcements`).
"""

from __future__ import annotations

import io
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from persistence.database import Database
    from scheduler.rate_limiter import PersistentRateLimiter
    from sources.bundesapi import BundesAPISource, SearchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PDF → text
# ---------------------------------------------------------------------------


def _extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    """Extract text from an AD PDF using pypdf. Returns None on any failure."""
    try:
        import pypdf
    except ImportError:
        logger.warning("pypdf not installed — AD PDF extraction disabled")
        return None

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001 — pypdf raises a variety of things
        logger.debug("pypdf extract failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Regex-based field extractors
# ---------------------------------------------------------------------------

# Capital amount — "Stammkapital" (GmbH/UG) or "Grundkapital" (AG) followed by
# a German-formatted number and a currency. The number is usually on the next
# line after the label.
_CAPITAL_RE = re.compile(
    r"(?:Stammkapital|Grundkapital|Stamm-\s*/\s*Grundkapital)"
    r"\s*[:\s]*\s*([\d][\d\.]*(?:,\d+)?)\s*(EUR|€|DM)",
    re.IGNORECASE,
)

# Fallback: number + currency anywhere within 200 chars after the label.
_CAPITAL_FALLBACK_RE = re.compile(
    r"(?:Stammkapital|Grundkapital)[^\d]{0,200}"
    r"([\d][\d\.]*(?:,\d+)?)\s*(EUR|€|DM)",
    re.IGNORECASE | re.DOTALL,
)

# Business purpose (Gegenstand) — captures everything up to the next labelled
# section. The "3. Grund- oder Stammkapital" header marks the end on most
# Berlin excerpts; other courts use "d)", "e)", etc. sub-sections.
_PURPOSE_RE = re.compile(
    r"Gegenstand(?:\s+des\s+Unternehmens)?\s*:?\s*"
    r"(.+?)"
    r"(?=\s*(?:\d\.\s*(?:Grund|Stamm|Vertretungs|Prokura|Geschäftsf|Vorstand|"
    r"Aufsichtsrat|Rechtsform|Tag)|"
    r"d\)|e\)|f\)|4\.|5\.|"
    r"Allgemeine\s+Vertretungsregelung|Stammkapital|Grundkapital|"
    r"Prokura|eingetragen\s+am|Zweigniederlassung))",
    re.IGNORECASE | re.DOTALL,
)

# Registered address — matches "<street-word> <number>, <5-digit-postal> <city>"
# on a single line, anywhere in the PDF text. Using MULTILINE + line-anchors
# keeps us from crossing newlines. IMPORTANT: we use `[ \t\-]` (literal space /
# tab / hyphen) rather than `\s` — `\s` includes `\n`, which lets the match
# span multiple lines and pull in the previous "Sitz" line's city.
_ADDRESS_RE = re.compile(
    r"^"                                                # line start
    r"[ \t]*"
    r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\.\-]{2,}"                # first word of street
    r"(?:[ \t\-][A-ZÄÖÜa-zäöüß\.]+){0,3}?)"            # optional 0–3 more words
    r"[ \t]+(\d+[a-zA-Z]?(?:[-/]\d+[a-zA-Z]?)?)"       # house number (95, 12a, 3-5)
    r"[ \t]*,[ \t]*"
    r"(\d{5})"                                          # postal code
    r"[ \t]+"
    r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-\.]+(?:[ \t][A-ZÄÖÜa-zäöüß\-\.]+){0,3})"  # city
    r"[ \t]*$",                                         # line end
    re.MULTILINE,
)

# First registered date — the AD excerpt uses multiple phrasings:
#   "Tag der letzten Eintragung" (Berlin, often),
#   "eingetragen am DD.MM.YYYY",
#   "Ersteintragung" (rare),
#   "Gesellschaftsvertrag vom" (contract date — fallback only, for brand-new
#     UGs where the last-entry date may be empty).
_FIRST_REG_DATE_RE = re.compile(
    r"(?:Tag\s+der\s+letzten\s+Eintragung|eingetragen\s+am|eingetragen:|"
    r"Ersteintragung|Gesellschaftsvertrag\s+vom)"
    r"\s*[:\s]*\s*(\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE,
)

# Representation rules — usually under "Allgemeine Vertretungsregelung"
_REP_RULES_RE = re.compile(
    r"(?:Allgemeine\s+)?Vertretungsregelung\s*(?:\n|:)\s*"
    r"(.+?)"
    r"(?=\n\s*(?:b\)|c\)|d\)|e\)|5\.|6\.|Stammkapital|Grundkapital|"
    r"Prokura|Gesch[äa]ftsf[üu]hrer|Vorstand|eingetragen))",
    re.IGNORECASE | re.DOTALL,
)


def _parse_amount(raw: str) -> Optional[float]:
    """Parse a German-formatted number string ("4.000,00") into a float."""
    if not raw:
        return None
    cleaned = raw.strip().replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_multiline(text: str, limit: int = 1500) -> str:
    """Collapse whitespace/linebreaks into a single space, trim, truncate."""
    return re.sub(r"\s+", " ", text).strip()[:limit]


def parse_ad_pdf(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Parse an AD PDF and return a dict of extracted fields suitable for
    database update. Missing fields are omitted from the dict (so an
    UPDATE ... COALESCE(...) won't clobber existing data).

    Returns keys (any subset):
      capital_amount       : float
      capital_currency     : str
      purpose              : str
      street               : str
      postal_code          : str
      city                 : str
      first_registered_date: str  (DD.MM.YYYY)
      representation_rules : str
      ad_raw_text          : str  (for audit/debug; not a DB column)
    """
    text = _extract_pdf_text(pdf_bytes)
    if not text:
        return {}

    out: Dict[str, Any] = {"ad_raw_text": text}

    # Capital
    cap_match = _CAPITAL_RE.search(text) or _CAPITAL_FALLBACK_RE.search(text)
    if cap_match:
        amount = _parse_amount(cap_match.group(1))
        currency = cap_match.group(2).upper().replace("€", "EUR")
        if amount is not None:
            out["capital_amount"] = amount
            out["capital_currency"] = currency

    # Purpose
    pm = _PURPOSE_RE.search(text)
    if pm:
        out["purpose"] = _clean_multiline(pm.group(1), limit=1500)

    # Address — regex has 4 groups: street_name, house_number, postal_code, city
    am = _ADDRESS_RE.search(text)
    if am:
        out["street"] = f"{am.group(1).strip()} {am.group(2).strip()}".strip()
        out["postal_code"] = am.group(3).strip()
        out["city"] = am.group(4).strip()

    # First registration date
    dm = _FIRST_REG_DATE_RE.search(text)
    if dm:
        out["first_registered_date"] = dm.group(1)

    # Representation rules
    rm = _REP_RULES_RE.search(text)
    if rm:
        out["representation_rules"] = _clean_multiline(rm.group(1), limit=800)

    return out


# ---------------------------------------------------------------------------
# Integration — fetch + parse + persist for one company
# ---------------------------------------------------------------------------


def capture_ad_for_company(
    db: "Database",
    source: "BundesAPISource",
    company_id: int,
    search_result: "SearchResult",
    rate_limiter: Optional["PersistentRateLimiter"] = None,
) -> bool:
    """
    Fetch the AD PDF for one company, parse it, and update the company row.

    Cost: 1 rate-limit token per invocation. Callers must already have budget
    (the fetcher acquires its own token; if `rate_limiter` is provided here we
    additionally gate the call so it respects the shared scheduler budget).

    Returns True iff at least one of (capital_amount, purpose) was updated.
    """
    if search_result is None or search_result.row_index is None:
        return False

    # Outer rate-limit gate (shared scheduler budget). The source rate limiter
    # also decrements inside fetch_ad_pdf.
    if rate_limiter is not None and not rate_limiter.acquire(count=1, block=False):
        logger.debug("AD capture skipped — rate limiter empty (outer)")
        return False

    try:
        pdf_bytes = source.fetch_ad_pdf(search_result)
    except Exception as e:  # noqa: BLE001
        logger.debug("AD fetch error for %s: %s", search_result.name, e)
        return False

    if not pdf_bytes:
        return False

    parsed = parse_ad_pdf(pdf_bytes)
    parsed.pop("ad_raw_text", None)  # don't persist the raw text (not a column)
    if not parsed:
        return False

    # Build updates dict — only include non-empty values so we don't overwrite
    # existing data with None.
    updates = {k: v for k, v in parsed.items() if v is not None and v != ""}
    if not updates:
        return False

    try:
        db.update_company(company_id, **updates)
        logger.info(
            "AD captured for %s: %s",
            search_result.name[:40],
            ", ".join(f"{k}={'…' if k in {'purpose','representation_rules'} else v}" for k, v in updates.items()),
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("update_company failed after AD capture: %s", e)
        return False

    return bool(updates.get("capital_amount") or updates.get("purpose"))
