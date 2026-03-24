"""
Officer extraction from Handelsregister announcement text.

Parses German legal announcements to extract officer names, roles,
and whether they were appointed or dismissed.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Role patterns (German -> normalized role string)
ROLE_PATTERNS = [
    (r"geschäftsführerin", "Geschäftsführer"),
    (r"geschäftsführer", "Geschäftsführer"),
    (r"vorstandsvorsitzende[r]?", "Vorstand"),
    (r"vorstandsmitglied", "Vorstand"),
    (r"vorstand", "Vorstand"),
    (r"prokuristin", "Prokurist"),
    (r"prokurist", "Prokurist"),
    (r"liquidator(?:in)?", "Liquidator"),
]

# Name pattern: optional title + capitalized words (handles umlauts, hyphens, nobility prefixes)
_TITLE = r"(?:(?:Dr\.\s*(?:med\.\s*|jur\.\s*|rer\.\s*nat\.\s*|ing\.\s*)?|Prof\.\s*(?:Dr\.\s*)?|Dipl\.-\w+\s+))?"
_NAME_WORD = r"[A-ZÄÖÜ][a-zäöüß]+"
_NAME_CONNECTOR = r"(?:von|van|de|zu|vom|zum|zur|el|al|bin|den|der|ten|ter)"
_NAME_PART = rf"(?:{_NAME_CONNECTOR}\s+)?{_NAME_WORD}"
_FULL_NAME = rf"{_TITLE}{_NAME_WORD}(?:[-\s]+{_NAME_PART})+"

# Suffixes to strip from extracted names
_STRIP_SUFFIXES = re.compile(
    r"(?:"
    r",?\s*geb(?:oren|\.)\s*\d{2}\.\d{2}\.\d{4}"  # birth date
    r"|,?\s*\*\s*\d{2}\.\d{2}\.\d{4}"  # * 01.01.1980
    r"|,\s+[A-ZÄÖÜ][a-zäöüß]+$"  # trailing city after comma: ", München"
    r")\s*",
    re.IGNORECASE,
)


@dataclass
class ExtractedOfficer:
    """An officer extracted from announcement text."""

    name: str
    role: str  # Normalized: Geschäftsführer, Prokurist, Vorstand, Liquidator
    action: str  # 'appointed', 'dismissed', 'listed'
    is_current: bool


def _clean_name(raw: str) -> Optional[str]:
    """Clean an extracted name string."""
    name = raw.strip().rstrip(".,;:")

    # Strip birth date and city suffixes
    name = _STRIP_SUFFIXES.sub("", name).strip().rstrip(".,;:")

    # Must have at least 2 parts (first + last)
    parts = name.split()
    if len(parts) < 2:
        return None

    # Reject if too short or too long
    if len(name) < 4 or len(name) > 80:
        return None

    return name


def _split_names(text: str) -> List[str]:
    """Split a string containing multiple officer names."""
    # Split on semicolons, " und ", " and ", or numbered lists
    parts = re.split(r"\s*;\s*|\s+und\s+|\s+and\s+|\s*\d+\.\s+", text)
    names = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Also split on comma if what follows starts with a capital letter (new name)
        # but not if it looks like "von der Heide, Hamburg" (city after name)
        sub_parts = re.split(r",\s*(?=[A-ZÄÖÜ](?:r\.|rof\.)?\s*[A-ZÄÖÜ])", part)
        names.extend(p.strip() for p in sub_parts if p.strip())
    return names


def extract_officers_from_text(text: str) -> List[ExtractedOfficer]:
    """
    Extract officers from German Handelsregister announcement text.

    Returns list of ExtractedOfficer with name, role, action, and is_current.
    """
    if not text:
        return []

    officers = []
    seen = set()  # (name_lower, role) dedup

    def _add(name_raw: str, role: str, action: str):
        name = _clean_name(name_raw)
        if not name:
            return
        key = (name.lower(), role)
        if key in seen:
            return
        seen.add(key)
        is_current = action != "dismissed"
        officers.append(
            ExtractedOfficer(
                name=name,
                role=role,
                action=action,
                is_current=is_current,
            )
        )

    # Build role alternation for patterns
    role_alt = "|".join(r[0] for r in ROLE_PATTERNS)
    # End-of-segment: period+space+uppercase (new sentence), excluding abbreviations like Dr./Prof./geb.
    # We use a greedy match and extract names from it, rather than relying on a tricky end-anchor
    _END = r"(?=(?<!Dr)(?<!Prof)(?<!geb)(?<!Dipl)(?<!med)(?<!jur)(?<!rer)(?<!ing)(?<!nat)\.\s+[A-ZÄÖÜ]|\.\s*$|\n\n|$)"

    # Pattern 1: "Bestellt als Geschäftsführer: Name" or "Bestellt zum Geschäftsführer: Name"
    for m in re.finditer(
        rf"(?:bestellt\s+(?:als|zum?)\s+)({role_alt})\s*:\s*(.+?){_END}",
        text,
        re.IGNORECASE,
    ):
        role_raw, names_str = m.group(1), m.group(2)
        role = _normalize_role(role_raw)
        for name_raw in _split_names(names_str):
            name_match = re.match(rf"({_FULL_NAME})", name_raw)
            if name_match:
                _add(name_match.group(1), role, "appointed")

    # Pattern 2: "Abberufen/Nicht mehr Geschäftsführer: Name"
    for m in re.finditer(
        rf"(?:abberufen|nicht\s+mehr|ausgeschieden)\s*(?:als\s+)?({role_alt})\s*:\s*(.+?){_END}",
        text,
        re.IGNORECASE,
    ):
        role_raw, names_str = m.group(1), m.group(2)
        role = _normalize_role(role_raw)
        for name_raw in _split_names(names_str):
            name_match = re.match(rf"({_FULL_NAME})", name_raw)
            if name_match:
                _add(name_match.group(1), role, "dismissed")

    # Pattern 3: "Geschäftsführer: Name1; Name2" (listing without action verb)
    # Dedup via `seen` set handles overlap with patterns 1 & 2
    for m in re.finditer(
        rf"({role_alt})\s*:\s*(.+?){_END}",
        text,
        re.IGNORECASE,
    ):
        role_raw, names_str = m.group(1), m.group(2)
        role = _normalize_role(role_raw)
        for name_raw in _split_names(names_str):
            name_match = re.match(rf"({_FULL_NAME})", name_raw)
            if name_match:
                _add(name_match.group(1), role, "listed")

    return officers


def _normalize_role(role_raw: str) -> str:
    """Normalize a German role string to a consistent form."""
    role_lower = role_raw.lower().strip()
    for pattern, normalized in ROLE_PATTERNS:
        if re.match(pattern, role_lower):
            return normalized
    return role_raw.strip()


def persist_officers(
    db,
    company_id: int,
    extracted_officers: List[ExtractedOfficer],
    announcement_date: Optional[str] = None,
) -> int:
    """
    Write extracted officers to database, skipping duplicates.

    Returns count of officers inserted.
    """
    count = 0
    for officer in extracted_officers:
        if db.officer_exists(company_id, officer.name):
            continue
        db.insert_officer(
            company_id=company_id,
            name=officer.name,
            role=officer.role,
            start_date=announcement_date if officer.action == "appointed" else None,
            end_date=announcement_date if officer.action == "dismissed" else None,
            is_current=officer.is_current,
        )
        count += 1

    if count:
        logger.info("Persisted %d officers for company %d", count, company_id)
    return count


def backfill_officers_from_announcements(
    db,
    batch_size: int = 200,
) -> Dict[str, int]:
    """
    Process existing announcements that haven't been processed for officers.

    Returns stats dict with keys: announcements_processed, officers_added.
    """
    stats = {"announcements_processed": 0, "officers_added": 0}

    types = ["geschaeftsfuehrer", "neueintragung", "prokura"]
    announcements = db.get_unprocessed_announcements(
        announcement_types=types,
        limit=batch_size,
    )

    for ann in announcements:
        company_id = ann.get("company_id")
        text = ann.get("text", "")
        ann_date = ann.get("announcement_date")

        if not company_id or not text:
            db.mark_announcement_processed(ann["id"])
            stats["announcements_processed"] += 1
            continue

        officers = extract_officers_from_text(text)
        if officers:
            added = persist_officers(db, company_id, officers, ann_date)
            stats["officers_added"] += added

        db.mark_announcement_processed(ann["id"])
        stats["announcements_processed"] += 1

    if stats["officers_added"]:
        logger.info(
            "Officer backfill: %d announcements processed, %d officers added",
            stats["announcements_processed"],
            stats["officers_added"],
        )

    return stats
