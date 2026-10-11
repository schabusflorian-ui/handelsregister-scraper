"""
Regression tests for the hardening pass — stealth-pattern recall, negative
exclusion filters, holding-company detection, watchlist tier boost, cohort
source parsers. Each test pins a real-world case we care about so future
refactors can't silently regress.
"""

import pytest

from signals.adapters import _HOLDING_PATTERNS, EXCLUSION_PATTERNS, STEALTH_PATTERNS, _classify_shell
from signals.cohort_source import _parse_ewor, _parse_sprind
from signals.job_posting_source import _parse_comment
from signals.watchlist import tier_boost, watchlist_tier

# ---------------------------------------------------------------------------
# Stealth-pattern recall — ensures the expanded regex catches real bios
# ---------------------------------------------------------------------------


class TestStealthPatternRecall:
    @pytest.mark.parametrize(
        "bio",
        [
            # English
            "Building something new",
            "Working on something exciting",
            "Co-Founder @ Stealth",
            "Solo founder in pre-seed",
            "Indie hacker, bootstrapping an AI tool",
            "0 to 1 at a new venture",
            "On to the next adventure",
            "Pre-launch — coming soon",
            # German
            "Baue gerade an etwas Neuem",
            "Arbeite an etwas Spannendem",
            "Im Aufbau — neues SaaS",
            "Gründe gerade eine neue Firma",
            "Starte etwas Neues im AI-Bereich",
            "Nächstes Kapitel beginnt",
            # Emoji
            "🥷 Something new coming",
            "🤫 Stealth mode",
        ],
    )
    def test_catches_known_stealth_phrase(self, bio):
        assert STEALTH_PATTERNS.search(bio), f"missed stealth phrase: {bio!r}"


# ---------------------------------------------------------------------------
# Exclusion filter — these must NOT fire as stealth signals
# ---------------------------------------------------------------------------


class TestExclusionFilter:
    @pytest.mark.parametrize(
        "bio",
        [
            "Taking a sabbatical from Google",
            "On parental leave until Q2",
            "Elternzeit bis März 2027",
            "Advisor at several startups",
            "Angel investor and board member",
            "Interim CTO at ClientCo",
            "Freelance consultant",
            "PhD student at TU München",
            "Im Ruhestand",
            "Garden leave until October",
        ],
    )
    def test_exclusion_fires(self, bio):
        assert EXCLUSION_PATTERNS.search(bio), f"should have excluded: {bio!r}"

    @pytest.mark.parametrize(
        "bio",
        [
            "Founder & CEO at Stealth",
            "Building something new",
            "Co-Founder, Chief AI Officer",
        ],
    )
    def test_exclusion_does_not_false_positive(self, bio):
        assert not EXCLUSION_PATTERNS.search(bio), f"shouldn't exclude: {bio!r}"


# ---------------------------------------------------------------------------
# Holding-company (DACH pre-launch) detection
# ---------------------------------------------------------------------------


class TestHoldingDetection:
    @pytest.mark.parametrize(
        "name,shell_type",
        [
            ("Mustermann Beteiligungs GmbH", "beteiligungs"),
            ("Novello Verwaltungs UG", "verwaltungs"),
            ("Zukunft Holding GmbH", "holding"),
            ("Schmidt Vermögensverwaltung GmbH", "vermoegens"),
            ("Beispiel AI UG i.G.", "i_g"),
            ("Alpha Beteiligungsgesellschaft mbH", "beteiligungs"),
        ],
    )
    def test_detects_shell_pattern(self, name, shell_type):
        assert _HOLDING_PATTERNS.search(name), f"missed shell: {name}"
        assert _classify_shell(name) == shell_type

    @pytest.mark.parametrize(
        "name",
        [
            "Peec AI GmbH",
            "Celonis AG",
            "Deutsche Bank AG",  # Bank, not a shell
        ],
    )
    def test_regular_companies_not_shells(self, name):
        assert not _HOLDING_PATTERNS.search(name), f"wrongly flagged: {name}"


# ---------------------------------------------------------------------------
# Watchlist tiers
# ---------------------------------------------------------------------------


class TestWatchlist:
    @pytest.mark.parametrize(
        "employer,expected_tier",
        [
            ("OpenAI", 1),
            ("Anthropic", 1),
            ("Aleph Alpha", 1),
            ("DeepL", 2),
            ("N26", 2),
            ("Celonis AG", 2),
            ("Google DeepMind London", 1),  # substring
            ("SAP Signavio", 2),  # substring
            ("Max Planck Institute", 2),
            ("BMW AG", 3),
            ("Google", 3),
            ("Random Local GmbH", None),
        ],
    )
    def test_tier_lookup(self, employer, expected_tier):
        assert watchlist_tier(employer) == expected_tier

    def test_boost_scales_with_tier(self):
        assert tier_boost(1) > tier_boost(2) > tier_boost(3) > tier_boost(None)
        assert tier_boost(None) == 0.0


# ---------------------------------------------------------------------------
# Cohort parsers — regression test the slug→name derivation
# ---------------------------------------------------------------------------


class TestCohortParsers:
    def test_sprind_slug_href_parsing(self):
        html = """<html>
            <a href="/en/actions/projects/pixel-photonics">...</a>
            <a href="/en/actions/projects/polybot">...</a>
            <a href="/en/actions/projects/snipr-biome">...</a>
            <a href="/not-a-project">...</a>
        </html>"""
        entries = _parse_sprind(html, "https://www.sprind.org/en/actions/projects")
        names = [e.company_name for e in entries]
        assert "Pixel Photonics" in names
        assert "Polybot" in names
        assert "Snipr Biome" in names
        for e in entries:
            assert e.program_name == "SPRIND"
            assert e.geo_region == "DE"

    def test_ewor_slug_parsing(self):
        html = """<a href="/startups/quanted-ai">...</a>
                  <a href="/startups/supplied-technologies">...</a>"""
        entries = _parse_ewor(html, "https://www.ewor.com/our-startups")
        assert any(e.company_name == "Quanted Ai" for e in entries)
        assert any(e.company_name == "Supplied Technologies" for e in entries)


# ---------------------------------------------------------------------------
# HN job posting source
# ---------------------------------------------------------------------------


class TestJobPostingParse:
    def test_stealth_posting_detected(self):
        comment = {
            "id": 123,
            "text": "AcmeCo | Founding engineer | Berlin | Remote | ...",
            "created_at": "2026-10-01T10:00:00Z",
        }
        sig = _parse_comment(comment)
        assert sig is not None
        assert sig["signal_type"] == "job_posting_stealth"
        assert sig["entity_name"].startswith("AcmeCo")
        assert sig["geo_region"] == "DE"  # Berlin → DACH

    def test_series_c_company_excluded(self):
        comment = {
            "id": 124,
            "text": "BigCo | Founding engineer | Series D, 500 engineers | ...",
            "created_at": "2026-10-01T10:00:00Z",
        }
        assert _parse_comment(comment) is None  # excluded by series X guard

    def test_no_stealth_phrase_skipped(self):
        comment = {
            "id": 125,
            "text": "RegularCo | Senior Backend Engineer | Berlin | ...",
            "created_at": "2026-10-01T10:00:00Z",
        }
        assert _parse_comment(comment) is None
