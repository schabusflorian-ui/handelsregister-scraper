"""
Slow continuous stealth founder scraper.

Runs at 1 request per minute to avoid rate limiting.
Designed to run continuously in the background, gradually
building up a database of stealth founders over time.
"""

import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from processing.founder_tagger import tag_founder

logger = logging.getLogger(__name__)


# Search queries to cycle through (DACH region: Germany, Austria, Switzerland)
STEALTH_QUERIES = [
    # === GERMANY - Core stealth signals (highest yield) ===
    "site:linkedin.com/in stealth founder germany",
    "site:linkedin.com/in stealth founder berlin",
    "site:linkedin.com/in stealth founder munich",
    "site:linkedin.com/in stealth founder hamburg",
    "site:linkedin.com/in stealth founder frankfurt",
    "site:linkedin.com/in stealth founder cologne",
    "site:linkedin.com/in stealth founder stuttgart",
    "site:linkedin.com/in stealth founder dusseldorf",
    "site:linkedin.com/in stealth co-founder germany",
    "site:linkedin.com/in stealth co-founder berlin",
    'site:linkedin.com/in "stealth mode" founder germany',
    'site:linkedin.com/in "stealth startup" founder germany',
    'site:linkedin.com/in "stealth" "gründer" deutschland',
    # === GERMANY - "Building something new" (high yield) ===
    'site:linkedin.com/in "building something new" founder germany',
    'site:linkedin.com/in "building something new" founder berlin',
    'site:linkedin.com/in "building something new" entrepreneur germany',
    'site:linkedin.com/in "working on something new" founder germany',
    'site:linkedin.com/in "working on something new" founder berlin',
    'site:linkedin.com/in "working on something exciting" founder germany',
    'site:linkedin.com/in "working on something exciting" founder berlin',  # city variant
    'site:linkedin.com/in "building something exciting" founder berlin',
    'site:linkedin.com/in "building my next company" germany',
    'site:linkedin.com/in "building my next company" berlin',  # city variant
    'site:linkedin.com/in "working on my next thing" founder germany',
    # === GERMANY - Transition/new venture phrases ===
    'site:linkedin.com/in "next chapter" founder germany',
    'site:linkedin.com/in "next chapter" founder berlin',
    'site:linkedin.com/in "next adventure" founder germany',
    'site:linkedin.com/in "new venture" founder germany',
    'site:linkedin.com/in "new venture" founder berlin',
    'site:linkedin.com/in "exploring opportunities" founder germany',
    'site:linkedin.com/in "exploring opportunities" founder berlin',  # city variant of top producer
    'site:linkedin.com/in "between ventures" germany',
    'site:linkedin.com/in "figuring out" founder germany',
    # === GERMANY - Pre-launch/unannounced ===
    'site:linkedin.com/in "pre-launch" founder germany',
    'site:linkedin.com/in "pre-seed" founder germany',
    'site:linkedin.com/in "pre-seed" founder berlin',
    'site:linkedin.com/in "unannounced" founder germany',
    'site:linkedin.com/in "coming soon" founder germany',
    'site:linkedin.com/in "launching soon" founder germany',
    'site:linkedin.com/in "confidential" founder germany',
    # === GERMANY - Ex-FAANG founders ===
    'site:linkedin.com/in "ex-google" founder germany',
    'site:linkedin.com/in "ex-google" founder berlin',
    # 'site:linkedin.com/in "ex-meta" founder germany',  # zero yield — removed
    'site:linkedin.com/in "ex-amazon" founder germany',
    'site:linkedin.com/in "ex-microsoft" founder germany',
    'site:linkedin.com/in "ex-apple" founder germany',
    'site:linkedin.com/in "ex-stripe" founder germany',
    'site:linkedin.com/in "ex-uber" founder germany',
    'site:linkedin.com/in "ex-airbnb" founder germany',
    # === GERMANY - Ex-European unicorns ===
    'site:linkedin.com/in "ex-n26" founder',
    'site:linkedin.com/in "ex-zalando" founder',
    'site:linkedin.com/in "ex-delivery hero" founder',
    'site:linkedin.com/in "ex-celonis" founder',
    'site:linkedin.com/in "ex-personio" founder',
    'site:linkedin.com/in "ex-flixbus" founder',
    'site:linkedin.com/in "ex-trade republic" founder',
    # === GERMANY - Entrepreneur keywords ===
    'site:linkedin.com/in "serial entrepreneur" germany',
    'site:linkedin.com/in "serial entrepreneur" berlin',
    'site:linkedin.com/in "repeat founder" germany',
    'site:linkedin.com/in "2x founder" germany',
    'site:linkedin.com/in "entrepreneur in residence" germany',
    # === GERMANY - German language ===
    'site:linkedin.com/in "gründer" stealth deutschland',
    'site:linkedin.com/in "mitgründer" stealth',
    'site:linkedin.com/in "im aufbau" gründer',
    'site:linkedin.com/in "neugründung" gründer',
    # === AUSTRIA ===
    "site:linkedin.com/in stealth founder austria",
    "site:linkedin.com/in stealth founder vienna",
    "site:linkedin.com/in stealth founder wien",
    'site:linkedin.com/in "building something new" founder austria',
    'site:linkedin.com/in "working on something new" vienna',
    'site:linkedin.com/in "serial entrepreneur" austria',
    'site:linkedin.com/in "serial entrepreneur" vienna',
    # 'site:linkedin.com/in stealth co-founder graz',  # zero yield — removed
    'site:linkedin.com/in "exploring opportunities" founder austria',  # top query pattern
    'site:linkedin.com/in "next venture" founder austria',
    'site:linkedin.com/in "ex-bitpanda" founder',
    # === SWITZERLAND ===
    "site:linkedin.com/in stealth founder switzerland",
    "site:linkedin.com/in stealth founder zurich",
    "site:linkedin.com/in stealth founder zürich",
    'site:linkedin.com/in "building something new" founder switzerland',
    'site:linkedin.com/in "working on something new" zurich',
    'site:linkedin.com/in "serial entrepreneur" switzerland',
    'site:linkedin.com/in "serial entrepreneur" zurich',
    "site:linkedin.com/in stealth co-founder geneva",
    'site:linkedin.com/in "crypto" founder zug',
    'site:linkedin.com/in "web3" founder switzerland',
    'site:linkedin.com/in "exploring opportunities" founder switzerland',  # top query pattern
    'site:linkedin.com/in "next venture" founder zurich',
    # === PROGRAMS — talent-first accelerators / fellowships (ADDED, not replacing) ===
    # Being in one of these is itself a founder signal: vetted, actively building,
    # on a finite runway — even higher precision than generic "stealth" keywords.
    # EWOR — €500k pre-idea fellowship, Berlin-focused
    'site:linkedin.com/in "EWOR" founder',
    'site:linkedin.com/in "EWOR Fellow"',
    'site:linkedin.com/in "EWOR Fellowship"',
    'site:linkedin.com/in "EWOR" germany',
    # Entrepreneur First — talent-first accelerator (Berlin, London, Paris)
    'site:linkedin.com/in "entrepreneur first" berlin',
    'site:linkedin.com/in "entrepreneur first" germany',
    'site:linkedin.com/in "entrepreneur first" founder',
    'site:linkedin.com/in "@joinEF" founder',
    'site:linkedin.com/in "EF Berlin" founder',
    'site:linkedin.com/in "EF London" founder',
    # EF cohort naming: city-code + 2-digit year (LD25 = London 2025 start, etc)
    'site:linkedin.com/in "LD25" OR "LD24" founder',
    'site:linkedin.com/in "BE25" OR "BE24" founder',
    # SCHUB by UnternehmerTUM — Munich deep-tech accelerator
    'site:linkedin.com/in "SCHUB" "UnternehmerTUM"',
    'site:linkedin.com/in "SCHUB Fellowship"',
    'site:linkedin.com/in "SCHUB" TUM founder',
    'site:linkedin.com/in "SCHUB" munich gründer',
    # UnternehmerTUM XPLORE — Munich deep-tech / corporate-innovation program
    'site:linkedin.com/in "UnternehmerTUM XPLORE"',
    'site:linkedin.com/in "XPLORE" "UnternehmerTUM"',
    'site:linkedin.com/in "XPLORE" founder munich',
    'site:linkedin.com/in "XPLORE" TUM gründer',
]


class SlowStealthScraper:
    """
    Slow, continuous scraper that runs at ~1 request per minute.

    Designed to avoid rate limiting by:
    - Long delays between requests (60+ seconds)
    - Random jitter on delays
    - Rotating through queries
    - Persisting state for resume
    - Exponential backoff on rate limits
    """

    # Backoff settings
    MAX_CONSECUTIVE_FAILURES = 5
    BACKOFF_MULTIPLIER = 2
    MAX_BACKOFF_MINUTES = 60

    def __init__(
        self,
        db,
        state_file: str = "data/stealth_scraper_state.json",
        search_delay: int = 60,  # Seconds between search requests
        scrape_delay: int = 90,  # Seconds between LinkedIn scrapes
        jitter: float = 0.3,  # Random jitter (0.3 = ±30%)
        search_engine: str = "brave",  # 'brave', 'ddg', or 'rotate'
        fresh_mode: bool = False,  # Only find recently indexed profiles
        include_officers: bool = True,  # Cross-reference new GmbH officers with LinkedIn
    ):
        self.db = db
        self.state_file = state_file
        self.search_delay = search_delay
        self.scrape_delay = scrape_delay
        self.jitter = jitter
        self.search_engine = search_engine
        self.fresh_mode = fresh_mode
        self.include_officers = include_officers
        self.consecutive_failures = 0

        self.state = self._load_state()
        self._ensure_schema()

    def _ensure_schema(self):
        """Ensure stealth_founders table exists.

        Schema is defined in persistence/database.py — single source of truth.
        """
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='stealth_founders'")
        if not cursor.fetchone():
            logger.warning("stealth_founders table missing — creating via Database migration")
            self.db._create_tables()

    def _calculate_backoff(self) -> int:
        """Calculate backoff delay based on consecutive failures."""
        if self.consecutive_failures == 0:
            return 0

        # Exponential backoff: 2^failures minutes, capped at MAX_BACKOFF_MINUTES
        backoff_minutes = min(self.BACKOFF_MULTIPLIER**self.consecutive_failures, self.MAX_BACKOFF_MINUTES)
        return backoff_minutes * 60  # Return seconds

    def _load_state(self) -> Dict[str, Any]:
        """Load scraper state from file."""
        default_state = {
            "query_index": 0,
            "pending_urls": [],
            "processed_urls": [],
            "last_search_at": None,
            "last_scrape_at": None,
            "total_searches": 0,
            "total_scrapes": 0,
            "total_founders_found": 0,
            "skipped_non_german": 0,
            "skipped_low_confidence": 0,
            "query_stats": {},  # {query: {runs, total_results, new_results, last_run}}
        }

        try:
            if os.path.exists(self.state_file):
                with open(self.state_file) as f:
                    state = json.load(f)
                    # Merge with defaults for any missing keys
                    for key, value in default_state.items():
                        if key not in state:
                            state[key] = value
                    return state
        except Exception as e:
            logger.warning(f"Could not load state: {e}")

        return default_state

    def _save_state(self):
        """Save scraper state to file."""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save state: {e}")

    def _get_delay(self, base_delay: int) -> float:
        """Get delay with random jitter."""
        jitter_amount = base_delay * self.jitter
        return base_delay + random.uniform(-jitter_amount, jitter_amount)

    def _get_founders_to_recheck(self, days_old: int = 7, limit: int = 1) -> List[Dict]:
        """Get founders that haven't been checked in X days."""
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            SELECT id, linkedin_url, name, headline
            FROM stealth_founders
            WHERE last_checked_at IS NULL
               OR datetime(last_checked_at) < datetime('now', ?)
            ORDER BY last_checked_at ASC NULLS FIRST
            LIMIT ?
        """,
            (f"-{days_old} days", limit),
        )

        return [{"id": row[0], "url": row[1], "name": row[2], "headline": row[3]} for row in cursor.fetchall()]

    def _update_founder_check(self, founder_id: int, new_headline: str = None, changed: bool = False):
        """Update last_checked_at and detect changes."""
        cursor = self.db.conn.cursor()
        cursor.execute(
            """
            UPDATE stealth_founders
            SET last_checked_at = ?,
                profile_changed = CASE WHEN ? THEN 1 ELSE profile_changed END,
                headline = CASE WHEN ? IS NOT NULL THEN ? ELSE headline END
            WHERE id = ?
        """,
            (datetime.now().isoformat(), changed, new_headline, new_headline, founder_id),
        )
        self.db.conn.commit()

    def _get_existing_urls(self) -> set:
        """Get URLs already in database."""
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT linkedin_url FROM stealth_founders")
        return {row[0] for row in cursor.fetchall()}

    def _parse_search_title(self, title: str, url: str = None, snippet: str = None) -> tuple:
        """
        Parse LinkedIn search result to extract name and headline.

        Strategy:
        1. Handle DDG multi-profile results ("Name1 – Headline1 Name2 – Headline2")
        2. Try to parse clean "Name - Headline" format (DuckDuckGo)
        3. For messy titles (Brave), extract name from URL
        4. Extract headline from snippet if available
        """
        import re
        from urllib.parse import unquote

        headline = None
        name = None

        # Remove common suffixes
        clean_title = title.replace(" | LinkedIn", "").replace(" - LinkedIn", "").strip()

        # Handle DDG multi-profile results: "Name1 – Headline1 Name2 – Headline2 ..."
        # The en-dash (–) separates name from headline within each profile
        if " \u2013 " in clean_title:
            parts = clean_title.split(" \u2013 ")
            name = parts[0].strip()
            if len(parts) > 1:
                # Headline is everything up to the next person's name or "..."
                raw_headline = parts[1].strip()
                # Cut at "..." or at what looks like the next person's name
                # (next name starts with a capital letter after whitespace)
                for cut_marker in ["...", "\u2026"]:
                    if cut_marker in raw_headline:
                        raw_headline = raw_headline.split(cut_marker)[0].strip()
                        break
                headline = raw_headline[:100] if raw_headline else None
        # Check if title is messy (Brave-style with linkedin.com or ›)
        elif "linkedin.com" in title.lower() or "\u203a" in title:
            # Messy format - extract name from URL
            if url and "/in/" in url:
                username = unquote(url.split("/in/")[-1].rstrip("/"))
                name = username.replace("-", " ").title()
                # Remove trailing numbers (like "john-doe-123")
                name = re.sub(r"\s+\d+$", "", name)
                # Remove trailing hex-like IDs (like "54173729B")
                name = re.sub(r"\s+[0-9A-Fa-f]{5,}[A-Za-z]?\s*$", "", name)
        elif " - " in clean_title:
            # Clean format: "Name - Headline"
            parts = clean_title.split(" - ", 1)
            name = parts[0].strip()
            headline = parts[1].strip() if len(parts) > 1 else None

        # For URL-extracted names, also clean hex IDs
        if name and not headline:
            name = re.sub(r"\s+[0-9A-Fa-f]{5,}[A-Za-z]?\s*$", "", name)

        # Try to extract headline from snippet if we don't have one
        if not headline and snippet:
            # Common patterns in snippets
            # "Name - Headline | LinkedIn" or "Experience: Title"
            if " - " in snippet:
                parts = snippet.split(" - ", 1)
                if len(parts) > 1 and len(parts[1]) > 3:
                    headline = parts[1].split("|")[0].split("\u00b7")[0].strip()[:100]
            elif "Experience:" in snippet:
                match = re.search(r"Experience:\s*([^\u00b7|]+)", snippet)
                if match:
                    headline = match.group(1).strip()

        # Fallback name from title if still empty
        if not name:
            name = clean_title[:50] if clean_title else "Unknown"

        return name, headline

    def _calculate_snippet_confidence(
        self,
        title: str,
        snippet: str,
        search_query: str = None,
        headline: str = None,
    ) -> tuple:
        """
        Calculate confidence score from search snippet without scraping LinkedIn.

        Core principle: the "stealth" / "working on something new" redaction pattern
        is THE primary signal. Someone whose LinkedIn headline is literally just
        "Stealth" or "Stealth Startup" in a DACH city is the exact profile we're
        hunting — they've stripped their role title because they can't share it.
        Score them as top-tier, not as noise.

        Scoring:
          - Stealth keywords:        +0.15 each, cap = 0.45 if DACH else 0.25
          - Founder keywords:        +0.10 each, cap = 0.20
          - Combo (stealth+founder): +0.10
          - Headline = pure stealth: +0.25  (redaction pattern — very specific)
          - Implicit founder credit: +0.25 total if stealth+DACH but no founder keyword
                                     (treats redacted headlines as implicit founders)
          - Ex-FAANG/unicorn:        +0.15 each
          - DACH location:           +0.05 (one-time, query-aware)
          - Anti-signals:            −0.25 each (ex-stealth, investor/advisor-only)

        Args:
            title: Search result title from DDG
            snippet: Search result snippet from DDG
            search_query: The search query used (to strip echoed DACH terms)
            headline: Already-parsed headline from _parse_search_title (optional).
                     When provided, enables exact-headline redaction-pattern bonus.

        Returns: (confidence_score, detected_signals)
        """
        import re

        # Build the text buffer from all available fields. Including the headline
        # is essential: for re-scores of existing DB rows, `title` is just the
        # person's name (no keywords), so the stealth/founder words only appear
        # in headline + snippet. Skipping headline here is what caused conf 0.5
        # "Founder @Stealth" rows to drop to 0.25 on rescore.
        text = " ".join(filter(None, [title, headline, snippet])).lower()
        signals = []
        score = 0.0

        # ==================== ANTI-SIGNALS ====================
        # Block false positives where "stealth" appears but person isn't a founder.
        # Apply as penalties so a borderline case can still pass threshold on merit.
        anti_patterns = [
            # Past stealth: "ex-stealth", "formerly stealth", "previously stealth"
            (r"\bex[- ]stealth\b", "anti:ex-stealth"),
            (r"\bformerly\s+(at\s+)?stealth\b", "anti:formerly-stealth"),
            (r"\bpreviously\s+(at\s+)?stealth\b", "anti:previously-stealth"),
            (r"\bworked\s+(at|in)\s+stealth\b", "anti:worked-at-stealth"),
            # Employee-language around stealth (not founder)
            (r"\b(developer|engineer|designer|pm|product manager|recruiter|hr|hiring)\s+at\s+stealth\b", "anti:employee-at-stealth"),
            # Investor/advisor/consultant-only roles with no founder keyword nearby
            (r"\b(angel investor|venture partner|vc partner|general partner|managing partner)\b", "anti:investor-only"),
            (r"\b(advisor|consultant|coach)\b(?!.*founder)(?!.*stealth)", "anti:advisor-only"),
        ]
        for pattern, label in anti_patterns:
            if re.search(pattern, text):
                signals.append(label)
                score -= 0.25

        # ==================== STEALTH KEYWORDS ====================
        # Primary signal — the redaction pattern. Cap is raised when DACH location
        # is also present (computed below), so stealth-heavy DACH profiles score
        # what they deserve instead of being capped at 0.25.
        stealth_words = [
            # Literal stealth
            "stealth",
            "stealth mode",
            "stealth startup",
            "stealth company",
            # "Building / working on something X" — the other redaction pattern
            "building something",
            "something new",
            "something exciting",
            "working on something",
            "working on my next",
            "building my next",
            "my next company",
            "my next thing",
            "next company",
            # Pre-launch signals
            "pre-launch",
            "pre-seed",
            "unannounced",
            "confidential",
            "coming soon",
            "launching soon",
            "secret project",
            # Transition phrases (weaker but still relevant)
            "next chapter",
            "next adventure",
            "next venture",
            "new venture",
            "between ventures",
            "taking time",
            "sabbatical",
            "figuring out",
            "what's next",
            "exploring opportunities",
            # German
            "im aufbau",
            "neugründung",
        ]
        stealth_score = 0.0
        for word in stealth_words:
            if word in text:
                signals.append(word)
                stealth_score += 0.15

        # ==================== FOUNDER KEYWORDS ====================
        # Note: "angel investor" and "venture partner" moved to anti-signals above
        # (they're not founder signals).
        founder_words = [
            "founder",
            "co-founder",
            "cofounder",
            "mitgründer",
            "gründer",
            "ceo",
            "chief executive",
            "entrepreneur",
            "unternehmer",
            "serial entrepreneur",
            "repeat founder",
            "2x founder",
            "3x founder",
            "4x founder",
            "eir",
            "entrepreneur in residence",
        ]
        founder_score = 0.0
        for word in founder_words:
            if word in text:
                signals.append(word)
                founder_score += 0.1

        # ==================== PROGRAM AFFILIATIONS ====================
        # Named talent-first accelerators / fellowships. Being in one of these
        # is itself a strong founder signal — participants are vetted, actively
        # building, and on a finite runway.
        #
        # Patterns are regex to avoid false positives:
        #  - "ef" alone matches too many things, so we require "entrepreneur first",
        #    "@joinEF", or "EF <city>"
        #  - "schub" alone is German for "push" — require UnternehmerTUM/TUM/Munich
        #    context within 80 chars in either direction
        #  - "xplore" is also generic — same context guard as schub
        program_patterns = [
            # EWOR — distinctive 4-letter acronym, safe to match directly
            (r"\bewor\b",                           "prog:ewor",              0.30),
            # Entrepreneur First variants
            (r"\bentrepreneur first\b",             "prog:entrepreneur-first", 0.30),
            (r"@joinef\b",                          "prog:ef-handle",          0.30),
            (r"\bef\s+(berlin|london|paris|bangalore|singapore|toronto|nyc|new york)\b",
                                                    "prog:ef-city",            0.25),
            # EF cohort naming: city-code + 2-digit year (LD25, BE24, PA23, etc).
            # Gated on EF context within 100 chars either side to avoid matching
            # unrelated "LD25" strings (e.g., NJ legislative district 25).
            (r"\b(?:ld|be|pa|sf|sg|bl|to|ny|ba|tor)\d{2}\b.{0,100}(entrepreneur first|@joinef|\bef\s+(?:berlin|london))",
                                                    "prog:ef-cohort",          0.15),
            (r"(entrepreneur first|@joinef|\bef\s+(?:berlin|london)).{0,100}\b(?:ld|be|pa|sf|sg|bl|to|ny|ba|tor)\d{2}\b",
                                                    "prog:ef-cohort",          0.15),
            # SCHUB by UnternehmerTUM — "schub" alone is German for "push",
            # so we require explicit TUM/UnternehmerTUM context, OR the exact
            # phrase "SCHUB Fellowship" which is unambiguous.
            (r"\bschub\s+fellowship\b",             "prog:schub",              0.30),
            (r"\bschub\b.{0,100}\b(unternehmertum|tum)\b",
                                                    "prog:schub",              0.30),
            (r"\b(unternehmertum|tum)\b.{0,100}\bschub\b",
                                                    "prog:schub",              0.30),
            # UnternehmerTUM XPLORE — same story: "xplore" is too generic alone.
            # Require TUM/UnternehmerTUM context.
            (r"\bunternehmertum\s+xplore\b",        "prog:xplore",             0.30),
            (r"\bxplore\b.{0,100}\b(unternehmertum|tum)\b",
                                                    "prog:xplore",             0.25),
            (r"\b(unternehmertum|tum)\b.{0,100}\bxplore\b",
                                                    "prog:xplore",             0.25),
        ]
        program_score = 0.0
        program_hits = set()  # dedupe by label — prevents double-counting EF + EF-city
        for pattern, label, weight in program_patterns:
            if label in program_hits:
                continue
            if re.search(pattern, text):
                signals.append(label)
                program_score += weight
                program_hits.add(label)

        # Program staff (event/community/program managers, interns) are NOT founders —
        # they work FOR the program. Revoke program credit when employee roles are detected
        # alongside program keywords.
        if program_hits:
            staff_roles = (
                r"\b("
                r"program(me)?\s+manager|"
                r"event\s+manager|"
                r"community\s+manager|"
                r"operations\s+manager|"
                r"office\s+manager|"
                r"communications?\s+manager|"
                r"program(me)?\s+coordinator|"
                r"recruiter|"
                r"intern\b|"
                r"program(me)?\s+(and|&)\s+event|"
                r"venture\s+partner|"
                r"head\s+of\s+(programs?|community|events|marketing)|"
                r"(bd|business\s+development)\s+at"
                r")"
            )
            if re.search(staff_roles, text):
                signals.append("anti:program-staff")
                program_score = 0.0
                program_hits = set()

        # ==================== DACH LOCATION ====================
        # Extended city list — the old list missed Düsseldorf, Stuttgart, Chur, etc.
        # and was dropping good leads because their location didn't match.
        dach_indicators = [
            # DE — countries + top 20 cities
            "germany", "deutschland",
            "berlin", "munich", "münchen", "hamburg", "frankfurt",
            "cologne", "köln", "stuttgart", "düsseldorf", "dusseldorf",
            "dortmund", "essen", "leipzig", "bremen", "dresden",
            "hannover", "nürnberg", "nuremberg", "bonn", "münster", "muenster",
            "karlsruhe", "mannheim", "augsburg", "wiesbaden", "heidelberg",
            "freiburg", "mainz", "kiel", "aachen", "regensburg", "potsdam",
            # AT — country + top 7 cities
            "austria", "österreich", "oesterreich",
            "vienna", "wien", "graz", "linz", "salzburg", "innsbruck", "klagenfurt",
            # CH — country + top cities
            "switzerland", "schweiz", "suisse",
            "zurich", "zürich", "geneva", "genf", "basel", "zug", "bern",
            "lausanne", "luzern", "lucerne", "winterthur", "chur", "st. gallen",
            "st.gallen", "sankt gallen",
        ]

        # Strip DACH terms that came from the search query (DDG echoes them back)
        if search_query:
            from sources.linkedin_scraper import _extract_dach_terms_from_query

            query_dach_terms = _extract_dach_terms_from_query(search_query)
            if query_dach_terms:
                dach_check_text = text
                for term in sorted(query_dach_terms, key=len, reverse=True):
                    dach_check_text = re.sub(r"\b" + re.escape(term) + r"\b", "", dach_check_text)
            else:
                dach_check_text = text
        else:
            dach_check_text = text

        has_dach = False
        for loc in dach_indicators:
            if loc in dach_check_text:
                signals.append(f"loc:{loc}")
                score += 0.05
                has_dach = True
                break

        # ==================== APPLY CAPS (conditional on DACH) ====================
        # When DACH is confirmed, lift the stealth cap — we've already filtered to
        # the right geography, so stacking stealth keywords isn't noise.
        stealth_cap = 0.45 if has_dach else 0.25
        score += min(stealth_score, stealth_cap)
        score += min(founder_score, 0.20)

        # Programs cap — prevents stacking (EF + EF-city + EF-cohort → only 0.35 total)
        score += min(program_score, 0.35)

        # ==================== COMBO BONUS ====================
        # Stealth OR program signal combined with a founder keyword → combo.
        if (stealth_score > 0 or program_score > 0) and founder_score > 0:
            score += 0.10

        # ==================== IMPLICIT FOUNDER (redaction pattern OR program) ====================
        # Someone whose LinkedIn headline is redacted to "Stealth" in a DACH city,
        # OR who's in a vetted founder program like EWOR / EF / SCHUB, is almost
        # certainly a founder. Without this, they score like job-seekers because
        # the literal word "founder" may be absent from their bio.
        if founder_score == 0 and (
            (stealth_score > 0 and has_dach) or program_score > 0
        ):
            if program_score > 0:
                signals.append("implicit:founder_from_program")
            else:
                signals.append("implicit:founder_from_stealth")
            score += 0.15  # implicit founder credit
            score += 0.10  # implicit combo bonus

        # ==================== HEADLINE REDACTION BONUS ====================
        # Exact-match bonus: headline is _just_ the redaction phrase, nothing else.
        # This is the cleanest positive signal — an intentionally-minimal headline
        # that says "I'm a stealth founder but I can't tell you more yet".
        if headline:
            hl = re.sub(r"[|·•\-/,\s]+", " ", headline.lower()).strip()
            redaction_headlines = {
                "stealth",
                "stealth mode",
                "stealth startup",
                "stealth company",
                "stealth ai startup",
                "stealth ai",
                "startup in stealth mode",
                "startup in stealth",
                "working on something new",
                "working on something exciting",
                "building something new",
                "building something exciting",
                "building my next company",
                "working on my next",
                "im aufbau",
                "neugründung",
            }
            # Exact match or redaction phrase bounded by word edges
            if hl in redaction_headlines or any(
                hl.startswith(p + " ") or hl.endswith(" " + p) or p in hl.split("  ")
                for p in redaction_headlines
                if len(p) > 4  # avoid matching just "stealth" in any context
            ):
                signals.append(f"redaction:{hl[:40]}")
                score += 0.25

        # ==================== EX-FAANG / UNICORN BACKGROUND ====================
        companies = [
            "google", "meta", "facebook", "amazon", "stripe", "microsoft",
            "apple", "uber", "airbnb", "netflix", "twitter", "linkedin",
            "salesforce",
            "n26", "zalando", "delivery hero", "celonis", "personio",
            "flixbus", "trade republic", "contentful", "mambu", "sennder",
            "gorillas", "bitpanda", "refurbed", "wefox", "scalable capital",
        ]
        for company in companies:
            if (
                f"ex-{company}" in text
                or f"ex {company}" in text
                or f"former {company}" in text
                or f"previously {company}" in text
            ):
                signals.append(f"ex-{company}")
                score += 0.15

        # Clamp to [0, 1]
        return max(0.0, min(score, 1.0)), signals

    def cleanup_existing_founders(self):
        """
        Retroactively clean up existing stealth_founders entries:
        1. Re-parse multi-person names (DDG concatenated results) → keep first person only
        2. Strip hex-like IDs from names
        3. Re-evaluate DACH location (query-aware — delete false positives)
        4. Recalculate confidence with new scoring (query-aware)
        Note: Low-confidence entries are kept (not deleted) for manual review.
        """
        import re

        from sources.linkedin_scraper import is_dach_from_search_result

        cursor = self.db.conn.cursor()
        cursor.execute("""
            SELECT id, name, headline, linkedin_url, summary, stealth_signals,
                   search_query, location, detection_source
            FROM stealth_founders
        """)
        rows = cursor.fetchall()

        stats = {
            "fixed_names": 0,
            "fixed_hex": 0,
            "rescored": 0,
            "deleted": 0,
            "deleted_non_dach": 0,
            "location_updated": 0,
            "total": len(rows),
        }

        for row in rows:
            fid, name, headline, url, summary, signals_json, search_query, location, detection_source = row
            original_name = name
            new_headline = headline
            changed = False

            # 1. Fix multi-person names
            # a) Contains en-dash – (DDG profile separator)
            if name and " \u2013 " in name:
                parts = name.split(" \u2013 ")
                name = parts[0].strip()
                if len(parts) > 1 and not new_headline:
                    raw_hl = parts[1].strip()
                    for cut in ["...", "\u2026"]:
                        if cut in raw_hl:
                            raw_hl = raw_hl.split(cut)[0].strip()
                            break
                    new_headline = raw_hl[:100] if raw_hl else None
                stats["fixed_names"] += 1
                changed = True

            # b) Contains " ... " (DDG concatenation of multiple profiles)
            if name and " ... " in name:
                name = name.split(" ... ")[0].strip()
                # If that part still has " - ", take name before it
                if " - " in name:
                    parts = name.split(" - ", 1)
                    name = parts[0].strip()
                    if not new_headline and len(parts) > 1:
                        new_headline = parts[1].strip()[:100]
                stats["fixed_names"] += 1
                changed = True

            # c) Contains " - " (DDG "Name - Headline OtherName" concatenation)
            # These look like: "Name1 - Headline1 stuff Name2 - Headline2 Name3"
            # Split on first " - " and take name before it, headline after it (trimmed)
            if name and " - " in name:
                parts = name.split(" - ", 1)
                first_name = parts[0].strip()
                rest = parts[1].strip() if len(parts) > 1 else ""

                # Check if the rest contains another person's name (multi-profile)
                # Heuristic 1: many capitalized words or very long = likely multi-person
                # Heuristic 2: regex detects "FirstName LastName" pattern after initial text
                cap_words = [
                    w
                    for w in rest.split()
                    if w
                    and w[0].isupper()
                    and w not in ("CEO", "CTO", "COO", "CFO", "VP", "MD", "PhD", "AI", "ML", "IT")
                ]
                has_second_name = bool(re.search(r"(?:^.{5,})\s([A-Z][a-zäöüß]+\s+[A-Z][a-zäöüß]+)", rest))
                if len(cap_words) >= 5 or len(rest) > 80 or has_second_name:
                    # Likely multi-person — just keep first name
                    name = first_name
                    # Try to extract a clean headline from beginning of rest
                    # Cut at the first capitalized "First Last" pattern that looks like another name
                    headline_candidate = rest
                    # Truncate at common break points
                    for marker in [". ", " | ", "\u00b7"]:
                        if marker in headline_candidate:
                            headline_candidate = headline_candidate.split(marker)[0].strip()
                            break
                    if headline_candidate and len(headline_candidate) < 80:
                        new_headline = new_headline or headline_candidate
                    stats["fixed_names"] += 1
                    changed = True

            # 2. Strip hex-like IDs from names
            if name:
                cleaned = re.sub(r"\s+[0-9A-Fa-f]{5,}[A-Za-z]?\s*$", "", name)
                if cleaned != name:
                    name = cleaned
                    stats["fixed_hex"] += 1
                    changed = True

            # 3. Re-evaluate DACH location (query-aware)
            # Use search_query stored with the founder to strip echoed DACH terms
            # For entries with headline from the title (not snippet), use it
            is_dach, extracted_location = is_dach_from_search_result(
                title=original_name or "",
                snippet=summary or "",
                url=url or "",
                search_query=search_query or "",
                headline=new_headline or headline,
            )

            if not is_dach:
                cursor.execute("DELETE FROM stealth_founders WHERE id = ?", (fid,))
                stats["deleted"] += 1
                stats["deleted_non_dach"] += 1
                logger.info(f"  Deleting non-DACH: {name} (query: {search_query})")
                continue

            # Update location if we extracted one and there wasn't one before
            new_location = location
            if extracted_location and not location:
                new_location = extracted_location
                stats["location_updated"] += 1
                changed = True

            # 4. Recalculate confidence with new query-aware scoring
            # Pass headline so the redaction-pattern bonus can fire.
            search_text_title = original_name or ""
            search_text_snippet = summary or ""
            new_confidence, new_signals = self._calculate_snippet_confidence(
                search_text_title,
                search_text_snippet,
                search_query=search_query,
                headline=new_headline or headline,
            )

            # Update entry (low-confidence entries are kept for manual review)
            cursor.execute(
                """
                UPDATE stealth_founders
                SET name = ?, headline = COALESCE(?, headline),
                    location = COALESCE(?, location),
                    confidence_score = ?, stealth_signals = ?
                WHERE id = ?
            """,
                (
                    name,
                    new_headline,
                    new_location,
                    new_confidence,
                    json.dumps(new_signals) if new_signals else signals_json,
                    fid,
                ),
            )
            stats["rescored"] += 1

        self.db.conn.commit()

        print(f"\n{'=' * 60}")
        print("CLEANUP RESULTS")
        print(f"{'=' * 60}")
        print(f"  Total entries:     {stats['total']}")
        print(f"  Names fixed:       {stats['fixed_names']} (multi-person → first person)")
        print(f"  Hex IDs stripped:  {stats['fixed_hex']}")
        print(f"  Non-DACH deleted:  {stats['deleted_non_dach']} (false positives from query echo)")
        print(f"  Total deleted:     {stats['deleted']}")
        print(f"  Locations added:   {stats['location_updated']}")
        print(f"  Rescored:          {stats['rescored']}")
        print(f"  Remaining:         {stats['total'] - stats['deleted']}")
        print()

        return stats

    def _store_from_search_result(self, result, search_query: str):
        """
        Store a founder directly from search snippet - NO LinkedIn scrape needed!

        This avoids 999 blocks by extracting info from DuckDuckGo results.
        """
        from sources.linkedin_scraper import is_dach_from_search_result

        name, headline = self._parse_search_title(result.title, result.url, result.snippet)
        confidence, signals = self._calculate_snippet_confidence(
            result.title, result.snippet, search_query=search_query, headline=headline
        )

        # Check if genuinely DACH (query-aware: strips echoed terms)
        is_dach, extracted_location = is_dach_from_search_result(
            title=result.title,
            snippet=result.snippet,
            url=result.url,
            search_query=search_query,
            headline=headline,
        )

        # Skip if not DACH or too low confidence
        if not is_dach:
            logger.debug(f"  Skipping non-DACH: {name}")
            self.state["skipped_non_german"] = self.state.get("skipped_non_german", 0) + 1
            return

        if confidence < 0.2:
            logger.debug(f"  Skipping low confidence: {name} ({confidence:.2f})")
            self.state["skipped_low_confidence"] = self.state.get("skipped_low_confidence", 0) + 1
            return

        # Store in database
        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute(
            """
            INSERT OR IGNORE INTO stealth_founders (
                linkedin_url, name, headline, location, summary,
                detection_source, search_query, stealth_signals,
                confidence_score, first_seen_at, last_checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                result.url,
                name,
                headline,
                extracted_location,  # Store the extracted DACH location
                result.snippet,  # Use snippet as summary
                "search_snippet",
                search_query,
                json.dumps(signals) if signals else None,
                confidence,
                now,
                now,
                now,
            ),
        )

        if cursor.rowcount > 0:
            self.db.conn.commit()
            self.state["total_founders_found"] = self.state.get("total_founders_found", 0) + 1
            logger.info(f"  Stored from snippet: {name} (conf={confidence:.2f}, signals={signals})")

    def _store_founder(self, profile, search_query: str):
        """Store or update a founder in the database."""
        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        # Build combined signals JSON
        all_signals = {}
        for attr, key in [
            ("stealth_signals", "stealth"), ("founder_signals", "founder"),
            ("transition_signals", "transition"), ("urgency_signals", "urgency"),
            ("traction_signals", "traction"), ("cofounder_signals", "cofounder"),
            ("high_value_background", "background"),
        ]:
            val = getattr(profile, attr, None)
            if val:
                all_signals[key] = val
        signals_json = json.dumps(all_signals) if all_signals else None

        # Check if exists — use INSERT OR UPDATE to preserve first_seen_at
        cursor.execute("SELECT id FROM stealth_founders WHERE linkedin_url = ?", (profile.url,))
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """
                UPDATE stealth_founders SET
                    name = COALESCE(?, name),
                    headline = COALESCE(?, headline),
                    location = COALESCE(?, location),
                    summary = COALESCE(?, summary),
                    current_company = COALESCE(?, current_company),
                    stealth_signals = ?,
                    confidence_score = ?,
                    primary_function = COALESCE(?, primary_function),
                    company_tier = MAX(COALESCE(?, 0), COALESCE(company_tier, 0)),
                    is_repeat_founder = MAX(COALESCE(?, 0), COALESCE(is_repeat_founder, 0)),
                    looking_for_cofounder = MAX(COALESCE(?, 0), COALESCE(looking_for_cofounder, 0)),
                    last_checked_at = ?,
                    profile_changed = CASE
                        WHEN headline != ? OR confidence_score != ? THEN 1
                        ELSE profile_changed
                    END
                WHERE linkedin_url = ?
                """,
                (
                    profile.name, profile.headline, profile.location,
                    profile.summary, getattr(profile, "current_company", None),
                    signals_json, profile.confidence_score,
                    getattr(profile, "primary_function", None),
                    getattr(profile, "company_tier", 0),
                    1 if getattr(profile, "is_repeat_founder", False) else 0,
                    1 if getattr(profile, "looking_for_cofounder", False) else 0,
                    now, profile.headline, profile.confidence_score, profile.url,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO stealth_founders (
                    linkedin_url, name, headline, location, summary,
                    current_company, detection_source, search_query,
                    stealth_signals, confidence_score,
                    primary_function, company_tier,
                    is_repeat_founder, looking_for_cofounder,
                    first_seen_at, last_checked_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.url, profile.name, profile.headline,
                    profile.location, profile.summary,
                    getattr(profile, "current_company", None),
                    "slow_scraper", search_query,
                    signals_json, profile.confidence_score,
                    getattr(profile, "primary_function", None),
                    getattr(profile, "company_tier", 0),
                    1 if getattr(profile, "is_repeat_founder", False) else 0,
                    1 if getattr(profile, "looking_for_cofounder", False) else 0,
                    now, now, now,
                ),
            )

        self.db.conn.commit()
        logger.info(f"Stored founder: {profile.name} (conf={profile.confidence_score:.2f})")

        # Auto-tag with structured metadata
        try:
            tags = tag_founder(profile.name, profile.headline,
                               getattr(profile, "location", None), signals_json)
            cursor.execute("SELECT id FROM stealth_founders WHERE linkedin_url = ?", (profile.url,))
            row = cursor.fetchone()
            if row and tags:
                set_parts = [f"{k} = ?" for k in tags.keys()]
                cursor.execute(
                    f"UPDATE stealth_founders SET {', '.join(set_parts)} WHERE id = ?",
                    list(tags.values()) + [row[0]],
                )
                self.db.conn.commit()
        except Exception as e:
            logger.debug(f"Auto-tagging failed for {profile.name}: {e}")

    def _build_exclusion_query(self, base_query: str, max_exclusions: int = 5) -> str:
        """
        Append -"Name" exclusions to a query to push past already-seen results.

        DDG returns the same ranked results for the same query every time.
        By excluding names we've already found, we force it to show the next tier.
        """
        cursor = self.db.conn.cursor()
        # Get names of founders already found by this exact query
        cursor.execute(
            """
            SELECT name FROM stealth_founders
            WHERE search_query = ? AND name IS NOT NULL AND name != ''
            ORDER BY confidence_score DESC
            LIMIT ?
        """,
            (base_query, max_exclusions),
        )

        names = [row[0] for row in cursor.fetchall()]
        if not names:
            return base_query

        # Build exclusion string: -"Firstname Lastname"
        exclusions = " ".join(f'-"{name}"' for name in names)
        enhanced = f"{base_query} {exclusions}"

        # DDG has a ~500 char query limit; trim if needed
        if len(enhanced) > 480:
            # Reduce exclusions until we fit
            while names and len(enhanced) > 480:
                names.pop()
                exclusions = " ".join(f'-"{name}"' for name in names)
                enhanced = f"{base_query} {exclusions}"

        logger.info(f"  Excluding {len(names)} known names to surface new profiles")
        return enhanced

    def _search_new_officers(self, batch_size: int = 5) -> int:
        """
        Layer 3: Cross-reference new GmbH registrations with LinkedIn.

        Searches LinkedIn for officers of recently registered companies to discover
        founders who never used "stealth" keywords but just registered a company.

        Args:
            batch_size: Number of companies to process per call

        Returns:
            Number of new founders found
        """
        from sources.google_search import DdgsLibraryScraper, SerperSearchScraper

        logger.info("=== Officer cross-reference: searching for new GmbH officers on LinkedIn ===")

        cursor = self.db.conn.cursor()

        # Get recent companies (last 30 days) that haven't been officer-searched yet
        # Track which companies we've already searched in state
        searched_company_ids = set(self.state.get("officer_searched_companies", []))

        # Use first_seen_date (populated) or registration_date (often NULL)
        # to find recently discovered companies
        cursor.execute(
            """
            SELECT c.id, c.name, c.city,
                   COALESCE(c.registration_date, c.first_seen_date) as effective_date
            FROM companies c
            WHERE (c.first_seen_date >= date('now', '-30 days')
                   OR c.registration_date >= date('now', '-30 days'))
              AND c.id NOT IN ({})
            ORDER BY effective_date DESC
            LIMIT ?
        """.format(",".join("?" * len(searched_company_ids)) if searched_company_ids else "0"),
            list(searched_company_ids) + [batch_size],
        )

        companies = cursor.fetchall()
        if not companies:
            logger.info("  No new companies to cross-reference (all recent ones already processed)")
            return 0

        total_found = 0
        existing = self._get_existing_urls()

        for company_row in companies:
            company_id, company_name, city, reg_date = company_row
            logger.info(f"  Company: {company_name} ({city}, registered {reg_date})")

            # Get officers for this company
            try:
                officers = self.db.get_officers(company_id)
            except Exception:
                # Fallback: query directly
                cursor.execute(
                    """
                    SELECT name, role FROM officers WHERE company_id = ?
                """,
                    (company_id,),
                )
                officers = [{"name": row[0], "role": row[1]} for row in cursor.fetchall()]

            if not officers:
                logger.debug(f"    No officers found for company {company_id}")
                searched_company_ids.add(company_id)
                continue

            for officer in officers:
                officer_name = officer.get("name", "")
                if not officer_name or len(officer_name) < 5:
                    continue

                # Build search query for this officer
                query = f'site:linkedin.com/in "{officer_name}" founder'

                logger.info(f"    Searching: {officer_name} ({officer.get('role', 'officer')})")

                try:
                    # Use the configured search engine
                    if self.search_engine == "ddgs":
                        scraper = DdgsLibraryScraper(delay_range=(1, 3))
                        results = scraper.search_query(query, max_pages=1)
                    elif self.search_engine == "serper":
                        scraper = SerperSearchScraper(delay_range=(0.5, 1.5))
                        results = scraper.search_query(query)
                    else:
                        scraper = DdgsLibraryScraper(delay_range=(1, 3))
                        results = scraper.search_query(query, max_pages=1)

                    for r in results:
                        if r.url not in existing:
                            existing.add(r.url)
                            # Store with special detection source
                            self._store_officer_crossref_result(r, officer_name, company_name, company_id)
                            total_found += 1

                except Exception as e:
                    logger.warning(f"    Officer search failed: {e}")

                # Small delay between officer searches
                time.sleep(random.uniform(2, 5))

            # Mark company as searched
            searched_company_ids.add(company_id)

        # Save searched company IDs to state (keep bounded)
        self.state["officer_searched_companies"] = list(searched_company_ids)[-500:]
        self._save_state()

        logger.info(f"=== Officer cross-reference complete: {total_found} new founders found ===")
        return total_found

    def _store_officer_crossref_result(self, result, officer_name: str, company_name: str, company_id: int):
        """Store a founder found via Handelsregister officer cross-reference."""
        from sources.linkedin_scraper import is_dach_from_search_result

        # Officer queries like 'site:linkedin.com/in "Hans Müller" founder'
        # typically don't contain DACH terms, so the filter works well here
        search_query = f"officer:{officer_name}"

        name, headline = self._parse_search_title(result.title, result.url, result.snippet)
        confidence, signals = self._calculate_snippet_confidence(
            result.title, result.snippet, search_query=search_query, headline=headline
        )

        # Boost confidence for officer cross-ref (these are high-value leads)
        confidence = min(confidence + 0.2, 1.0)
        signals.append(f"handelsregister_officer:{officer_name}")
        signals.append(f"company:{company_name}")

        # Check if genuinely DACH (query-aware)
        is_dach, extracted_location = is_dach_from_search_result(
            title=result.title,
            snippet=result.snippet,
            url=result.url,
            search_query=search_query,
            headline=headline,
        )

        if not is_dach:
            logger.debug(f"    Skipping non-DACH: {name}")
            return

        cursor = self.db.conn.cursor()
        now = datetime.now().isoformat()

        cursor.execute(
            """
            INSERT OR IGNORE INTO stealth_founders (
                linkedin_url, name, headline, location, summary,
                detection_source, search_query, stealth_signals,
                confidence_score, first_seen_at, last_checked_at, created_at,
                company_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                result.url,
                name,
                headline,
                extracted_location,  # Store extracted DACH location
                result.snippet,
                "handelsregister_crossref",
                search_query,
                json.dumps(signals) if signals else None,
                confidence,
                now,
                now,
                now,
                company_id,
            ),
        )

        if cursor.rowcount > 0:
            self.db.conn.commit()
            self.state["total_founders_found"] = self.state.get("total_founders_found", 0) + 1
            logger.info(f"    Stored (crossref): {name} ← officer of {company_name} (conf={confidence:.2f})")

    def _do_search(self, max_pages: int = 2) -> List[str]:
        """
        Perform one search query and return new URLs found.

        Args:
            max_pages: Number of result pages to fetch (default 2 = ~60 results)
        """
        from sources.google_search import (
            BraveSearchScraper,
            CurlCffiSearchScraper,
            DdgsLibraryScraper,
            DuckDuckGoSearchScraper,
            MultiSearchScraper,
            SerperSearchScraper,
        )

        # Get current base query
        base_query = STEALTH_QUERIES[self.state["query_index"]]

        # Enhance with exclusions to surface new results on repeat runs
        query = self._build_exclusion_query(base_query, max_exclusions=5)

        engine_name = self.search_engine.upper()
        logger.info(f"[{engine_name}] Searching: {base_query}")

        try:
            # Build time filter kwargs for fresh mode
            ddgs_time_kwargs = {}
            serper_time_kwargs = {}
            if self.fresh_mode:
                ddgs_time_kwargs = {"timelimit": "m"}  # Past month
                serper_time_kwargs = {"tbs": "qdr:m"}  # Past month
                logger.info("  Fresh mode: filtering for past month only")

            # Select search engine based on preference
            if self.search_engine == "ddgs":
                scraper = DdgsLibraryScraper(delay_range=(1, 3))
                results = scraper.search_query(query, max_pages=max_pages, **ddgs_time_kwargs)
            elif self.search_engine == "serper":
                scraper = SerperSearchScraper(delay_range=(0.5, 1.5))
                results = scraper.search_query(query, **serper_time_kwargs)
            elif self.search_engine == "curl":
                scraper = CurlCffiSearchScraper(delay_range=(2, 5))
                results = scraper.search_query(query, max_pages=max_pages)
            elif self.search_engine == "brave":
                scraper = BraveSearchScraper(delay_range=(2, 5), use_cloudscraper=True)
                results = scraper.search_query(query)
            elif self.search_engine == "ddg":
                scraper = DuckDuckGoSearchScraper(delay_range=(2, 5), use_cloudscraper=True)
                results = scraper.search_query(query, max_pages=max_pages)
            elif self.search_engine == "playwright_ddg":
                # Browser-driven DDG — bypasses TLS/fingerprint blocks that
                # hit html.duckduckgo.com. See sources/playwright_ddg_scraper.py.
                # Each call spins up a Chromium — tolerable at ~1 query/min
                # pace but expensive if run per-query rapidly.
                from sources.playwright_ddg_scraper import PlaywrightDdgScraper

                with PlaywrightDdgScraper(delay_range=(2, 5)) as scraper:
                    results = scraper.search_query(query, max_pages=max_pages)
            else:  # rotate
                scraper = MultiSearchScraper(delay_range=(2, 5))
                results = scraper.search_query(query, max_pages=max_pages)

            # Get existing URLs to filter
            existing = self._get_existing_urls()
            existing.update(self.state["processed_urls"])
            existing.update(self.state["pending_urls"])

            # Extract new URLs
            new_urls = []
            for r in results:
                if r.url not in existing:
                    new_urls.append(r.url)
                    logger.info(f"  Found: {r.title[:50]}")

                    # Store founder directly from search snippet (no LinkedIn scrape needed!)
                    # Use base_query (without exclusions) so exclusions accumulate correctly
                    self._store_from_search_result(r, base_query)

            # Update state
            self.state["query_index"] = (self.state["query_index"] + 1) % len(STEALTH_QUERIES)
            self.state["last_search_at"] = datetime.now().isoformat()
            self.state["total_searches"] += 1

            # Track per-query yield for validation (keyed by base query)
            if "query_stats" not in self.state:
                self.state["query_stats"] = {}
            qs = self.state["query_stats"].get(base_query, {"runs": 0, "total_results": 0, "new_results": 0})
            qs["runs"] = qs.get("runs", 0) + 1
            qs["total_results"] = qs.get("total_results", 0) + len(results)
            qs["new_results"] = qs.get("new_results", 0) + len(new_urls)
            qs["last_run"] = datetime.now().isoformat()
            self.state["query_stats"][base_query] = qs

            self._save_state()

            logger.info(
                f"  Stored {len(new_urls)} founders from search snippets (total results: {len(results)}, new: {len(new_urls)})"
            )

            return new_urls

        except Exception as e:
            logger.error(f"Search failed: {e}")
            # Move to next query anyway
            self.state["query_index"] = (self.state["query_index"] + 1) % len(STEALTH_QUERIES)
            self._save_state()
            return []

    def _do_scrape(self) -> Optional[Dict[str, Any]]:
        """Scrape one LinkedIn profile from pending queue."""
        from sources.linkedin_scraper import LinkedInProfileScraper, StealthFounderDetector

        if not self.state["pending_urls"]:
            return None

        # Get next URL
        url = self.state["pending_urls"].pop(0)

        logger.info(f"Scraping: {url}")

        scraper = LinkedInProfileScraper(delay_range=(1, 2), use_cloudscraper=True)
        # Require German location for filtering
        detector = StealthFounderDetector(min_confidence=0.15, require_german_location=True)

        try:
            profile = scraper.scrape_profile(url)

            result = {
                "url": url,
                "success": False,
                "name": None,
                "confidence": 0,
                "stored": False,
                "location": None,
                "is_german": False,
            }

            if profile and profile.name:
                result["success"] = True
                result["name"] = profile.name
                result["confidence"] = profile.confidence_score
                result["location"] = profile.location
                result["is_german"] = detector.is_german(profile)

                if detector.is_stealth_founder(profile):
                    # Get the query that found this URL (approximate)
                    query_idx = max(0, self.state["query_index"] - 1)
                    search_query = STEALTH_QUERIES[query_idx]

                    self._store_founder(profile, search_query)
                    result["stored"] = True
                    self.state["total_founders_found"] += 1

                    logger.info(
                        f"  Stored: {profile.name} (conf={profile.confidence_score:.2f}, loc={profile.location})"
                    )
                elif not result["is_german"]:
                    logger.info(f"  Not in Germany: {profile.name} (loc={profile.location})")
                    self.state["skipped_non_german"] = self.state.get("skipped_non_german", 0) + 1
                else:
                    logger.info(f"  Below threshold: {profile.name} (conf={profile.confidence_score:.2f})")
                    self.state["skipped_low_confidence"] = self.state.get("skipped_low_confidence", 0) + 1
            else:
                logger.warning("  Could not extract profile data")

            # Update state
            self.state["processed_urls"].append(url)
            # Keep processed_urls bounded
            if len(self.state["processed_urls"]) > 1000:
                self.state["processed_urls"] = self.state["processed_urls"][-500:]

            self.state["last_scrape_at"] = datetime.now().isoformat()
            self.state["total_scrapes"] += 1
            self._save_state()

            return result

        except Exception as e:
            logger.error(f"Scrape failed: {e}")
            self.state["processed_urls"].append(url)
            self._save_state()
            return {"url": url, "success": False, "error": str(e)}

    def _do_recheck(self) -> Optional[Dict[str, Any]]:
        """Re-check an existing founder for profile changes using enhanced tracking."""
        from sources.linkedin_scraper import LinkedInProfileScraper

        founders = self._get_founders_to_recheck(days_old=7, limit=1)
        if not founders:
            return None

        founder = founders[0]
        url = founder["url"]
        old_headline = founder["headline"]

        logger.info(f"Re-checking: {founder['name']} ({url})")

        scraper = LinkedInProfileScraper(delay_range=(1, 2), use_cloudscraper=True)

        try:
            profile = scraper.scrape_profile(url)

            result = {
                "url": url,
                "name": founder["name"],
                "success": False,
                "changed": False,
                "changes": [],
            }

            if profile and profile.name:
                result["success"] = True

                # Use the new update_stealth_founder method for change tracking
                changes = self.db.update_stealth_founder(
                    founder_id=founder["id"],
                    headline=profile.headline,
                    summary=profile.summary,
                    current_company=profile.current_company,
                    location=profile.location,
                    confidence_score=profile.confidence_score,
                )

                if changes:
                    result["changed"] = True
                    result["changes"] = changes
                    logger.info(f"  CHANGED: {len(changes)} field(s) updated")

                    for change in changes:
                        logger.info(f"    {change['field']}: {change['change_type']}")

                        # Check for stealth emergence
                        if change["change_type"] == "went_stealth":
                            logger.info("  WENT STEALTH! (may be starting something)")
                        elif change["change_type"] == "became_founder":
                            logger.info("  BECAME FOUNDER! Checking for company match...")
                            self._try_emergence_match(founder["id"])

            else:
                logger.warning("  Could not fetch profile")
                # Still update last_checked_at
                self._update_founder_check(founder["id"])

            return result

        except Exception as e:
            logger.error(f"Re-check failed: {e}")
            return {"url": url, "success": False, "error": str(e)}

    def _try_emergence_match(self, founder_id: int) -> Optional[Dict]:
        """Try to match a founder to a newly registered company."""
        try:
            from processing.emergence_matcher import EmergenceMatcher

            founder = self.db.get_stealth_founder(founder_id)
            if not founder or founder.get("company_id"):
                return None

            matcher = EmergenceMatcher(self.db)
            matches = matcher.find_matches_for_founder(founder, limit=3)

            if matches:
                best_match = matches[0]
                if best_match["name_similarity"] >= 0.95:
                    # Auto-link high confidence match
                    self.db.mark_founder_emerged(founder_id, best_match["company_id"])
                    logger.info(
                        f"  AUTO-LINKED: {founder['name']} -> {best_match['company_name']} "
                        f"(similarity: {best_match['name_similarity']:.2f})"
                    )
                    return best_match
                else:
                    logger.info(
                        f"  CANDIDATE: {best_match['company_name']} "
                        f"(similarity: {best_match['name_similarity']:.2f}) - needs review"
                    )

            return None
        except Exception as e:
            logger.warning(f"Emergence matching failed: {e}")
            return None

    def run_emergence_detection(self) -> Dict[str, Any]:
        """Run full emergence detection for all unemerged founders."""
        try:
            from processing.emergence_matcher import run_emergence_detection

            return run_emergence_detection(self.db, auto_link=True)
        except Exception as e:
            logger.error(f"Emergence detection failed: {e}")
            return {"error": str(e)}

    def run_once(self, iteration: int = 0) -> Dict[str, Any]:
        """
        Run one cycle: search, scrape, or re-check.

        Alternates between:
        - Scraping pending URLs (priority)
        - Searching for new URLs
        - Re-checking existing founders (every 10 iterations)

        Returns stats about what was done.
        """
        stats = {
            "action": None,
            "success": False,
            "details": {},
        }

        # Every 10 iterations, run officer cross-reference and re-check
        if iteration > 0 and iteration % 10 == 0:
            # Layer 3: Cross-reference new GmbH officers with LinkedIn
            if self.include_officers:
                try:
                    officer_found = self._search_new_officers(batch_size=3)
                    if officer_found > 0:
                        stats["action"] = "officer_crossref"
                        stats["success"] = True
                        stats["details"] = {"new_founders": officer_found}
                        return stats
                except Exception as e:
                    logger.warning(f"Officer cross-reference failed: {e}")

            # Re-check existing founders
            result = self._do_recheck()
            if result:
                stats["action"] = "recheck"
                stats["success"] = result.get("success", False)
                stats["details"] = result
                return stats

        # Decide whether to search or scrape
        # If we have pending URLs, scrape them first
        if self.state["pending_urls"]:
            stats["action"] = "scrape"
            result = self._do_scrape()
            if result:
                stats["success"] = result.get("success", False)
                stats["details"] = result
        else:
            stats["action"] = "search"
            try:
                new_urls = self._do_search()
                stats["success"] = True  # Search worked, even if 0 new URLs
                stats["details"] = {"new_urls": len(new_urls)}
            except Exception as e:
                stats["success"] = False
                stats["details"] = {"error": str(e)}
                logger.warning(f"  Search failed: {e}")

        return stats

    def run_continuous(self, max_iterations: int = None):
        """
        Run continuously with proper delays and exponential backoff on failures.

        Args:
            max_iterations: Maximum iterations (None = run forever)
        """
        iteration = 0

        logger.info("Starting slow continuous scraper...")
        logger.info(f"  Search engine: {self.search_engine}")
        logger.info(f"  Search delay: {self.search_delay}s (±{self.jitter * 100:.0f}%)")
        logger.info(f"  Scrape delay: {self.scrape_delay}s (±{self.jitter * 100:.0f}%)")
        logger.info(f"  Fresh mode: {'ON (past month only)' if self.fresh_mode else 'OFF (all time)'}")
        logger.info(f"  Officer crossref: {'ON' if self.include_officers else 'OFF'}")
        logger.info(f"  State file: {self.state_file}")
        logger.info(f"  Pending URLs: {len(self.state['pending_urls'])}")

        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1

                # Run one cycle (pass iteration for periodic re-checks)
                stats = self.run_once(iteration=iteration)

                logger.info(f"[{iteration}] {stats['action']}: {stats['details']}")

                # Track consecutive failures for backoff
                if stats["success"]:
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                    if self.consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
                        backoff = self._calculate_backoff()
                        logger.warning(
                            f"  {self.consecutive_failures} consecutive failures - backing off for {backoff / 60:.0f} minutes"
                        )
                        time.sleep(backoff)
                        # Reset counter after long backoff so we don't spiral
                        self.consecutive_failures = 0
                        continue

                # Choose delay based on action
                if stats["action"] == "search":
                    delay = self._get_delay(self.search_delay)
                else:
                    delay = self._get_delay(self.scrape_delay)

                # On rate limit failure: wait 5 minutes flat instead of compounding
                if not stats["success"] and self.consecutive_failures > 0:
                    delay = max(delay, 300)  # At least 5 minutes after a rate limit
                    logger.info(f"  Rate limited — waiting {delay:.0f}s before next query")

                logger.info(f"  Sleeping {delay:.0f}s...")
                time.sleep(delay)

        except KeyboardInterrupt:
            logger.info("Stopped by user")
            self._save_state()

    def get_stats(self) -> Dict[str, Any]:
        """Get current scraper statistics."""
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM stealth_founders")
        total_in_db = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM stealth_founders WHERE confidence_score >= 0.5")
        high_confidence = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM stealth_founders WHERE emerged_at IS NOT NULL")
        emerged_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM stealth_founders WHERE profile_changed = 1")
        changed_count = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(*) FROM stealth_founders
            WHERE last_checked_at IS NULL
               OR datetime(last_checked_at) < datetime('now', '-7 days')
        """)
        needs_recheck = cursor.fetchone()[0]

        return {
            "total_in_db": total_in_db,
            "high_confidence": high_confidence,
            "emerged": emerged_count,
            "profile_changed": changed_count,
            "needs_recheck": needs_recheck,
            "pending_urls": len(self.state["pending_urls"]),
            "total_searches": self.state["total_searches"],
            "total_scrapes": self.state["total_scrapes"],
            "total_founders_found": self.state["total_founders_found"],
            "skipped_non_german": self.state.get("skipped_non_german", 0),
            "skipped_low_confidence": self.state.get("skipped_low_confidence", 0),
            "current_query_index": self.state["query_index"],
            "current_query": STEALTH_QUERIES[self.state["query_index"]],
            "last_search_at": self.state["last_search_at"],
            "last_scrape_at": self.state["last_scrape_at"],
        }

    def print_query_report(self):
        """Print a report showing which queries produce results and which don't."""
        qs = self.state.get("query_stats", {})
        if not qs:
            print("No query stats yet. Run the scraper first to collect data.")
            return

        # Sort by new_results descending
        sorted_queries = sorted(qs.items(), key=lambda x: x[1].get("new_results", 0), reverse=True)

        print(f"\n{'=' * 90}")
        print(f"QUERY YIELD REPORT — {len(sorted_queries)} queries tracked")
        print(f"{'=' * 90}")

        # Summary
        total_runs = sum(v.get("runs", 0) for v in qs.values())
        total_results = sum(v.get("total_results", 0) for v in qs.values())
        total_new = sum(v.get("new_results", 0) for v in qs.values())
        zero_yield = sum(1 for v in qs.values() if v.get("new_results", 0) == 0)

        print(f"\n  Total runs: {total_runs} | Total results: {total_results} | New founders: {total_new}")
        print(
            f"  Zero-yield queries: {zero_yield}/{len(sorted_queries)} ({zero_yield * 100 // max(len(sorted_queries), 1)}%)"
        )
        print(f"  Avg new per query: {total_new / max(len(sorted_queries), 1):.1f}")

        # Top producers
        print(f"\n{'─' * 90}")
        print("  TOP PRODUCERS (queries that find the most new founders)")
        print(f"{'─' * 90}")
        for query, stats in sorted_queries[:15]:
            runs = stats.get("runs", 0)
            total = stats.get("total_results", 0)
            new = stats.get("new_results", 0)
            yield_pct = (new * 100 // max(total, 1)) if total > 0 else 0
            bar = "#" * min(new, 30)
            print(f"  {new:>4} new ({total:>3} total, {runs}x run) {bar}")
            print(f"       {query[:80]}")

        # Zero-yield queries
        zero_queries = [(q, s) for q, s in sorted_queries if s.get("new_results", 0) == 0 and s.get("runs", 0) > 0]
        if zero_queries:
            print(f"\n{'─' * 90}")
            print(f"  ZERO-YIELD QUERIES ({len(zero_queries)} queries returned 0 new founders)")
            print(f"{'─' * 90}")
            for query, stats in zero_queries:
                runs = stats.get("runs", 0)
                total = stats.get("total_results", 0)
                print(f"  {total:>3} total results, {runs}x run — {query[:70]}")

        # Not yet run
        not_run = [q for q in STEALTH_QUERIES if q not in qs]
        if not_run:
            print(f"\n{'─' * 90}")
            print(f"  NOT YET RUN ({len(not_run)} queries)")
            print(f"{'─' * 90}")
            for q in not_run[:10]:
                print(f"    {q[:80]}")
            if len(not_run) > 10:
                print(f"    ... and {len(not_run) - 10} more")

        print(f"\n{'=' * 90}\n")


def run_slow_scraper(
    db_path: str = "handelsregister.db",
    search_delay: int = 60,
    scrape_delay: int = 90,
    max_iterations: int = None,
):
    """
    Convenience function to run the slow scraper.

    Args:
        db_path: Path to database
        search_delay: Seconds between search requests (default 60)
        scrape_delay: Seconds between LinkedIn scrapes (default 90)
        max_iterations: Max iterations (None = run forever)
    """
    from persistence.database import Database

    db = Database(db_path)
    try:
        scraper = SlowStealthScraper(
            db=db,
            search_delay=search_delay,
            scrape_delay=scrape_delay,
        )
        scraper.run_continuous(max_iterations=max_iterations)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("SLOW STEALTH FOUNDER SCRAPER")
    print("=" * 60)
    print()
    print("This scraper runs at ~1 request per minute to avoid blocks.")
    print("It will gradually build up a database of stealth founders.")
    print()
    print("Press Ctrl+C to stop (state is saved automatically)")
    print()

    # Run with 10 iterations for testing, or None for continuous
    run_slow_scraper(
        search_delay=60,  # 1 minute between searches
        scrape_delay=90,  # 1.5 minutes between LinkedIn scrapes
        max_iterations=10,  # Remove this for continuous running
    )
