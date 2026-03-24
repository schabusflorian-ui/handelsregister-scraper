"""
Tests for officer LinkedIn search and matching module.

Tests query builders, name/headline extraction, company extraction,
location extraction, confidence scoring, and result parsing.
"""

import pytest

from processing.officer_linkedin_search import (
    OfficerLinkedInMatch,
    RateLimitedError,
    _calculate_match_confidence,
    _clean_company_name,
    _extract_companies_from_text,
    _extract_location,
    _extract_name_and_headline,
    build_fallback_query,
    build_search_query,
    parse_search_result,
)

# ============================================================================
# Query Builder Tests
# ============================================================================


class TestBuildSearchQuery:
    """Test search query construction."""

    def test_basic_query(self):
        """Standard name + company produces correct query."""
        query = build_search_query("Max Mustermann", "TechStartup Berlin")
        assert "linkedin.com/in" in query
        assert '"Max Mustermann"' in query
        assert '"TechStartup Berlin"' in query

    def test_strips_gmbh(self):
        """GmbH suffix is stripped from company name."""
        query = build_search_query("Anna Schmidt", "RoboTech GmbH")
        assert '"RoboTech"' in query
        assert "GmbH" not in query

    def test_strips_gmbh_co_kg(self):
        """GmbH & Co. KG suffix is stripped."""
        query = build_search_query("Jan Müller", "AutoPilot GmbH & Co. KG")
        assert '"AutoPilot"' in query
        assert "GmbH" not in query
        assert "KG" not in query

    def test_strips_ug(self):
        """UG (haftungsbeschränkt) suffix is stripped."""
        query = build_search_query("Lisa Weber", "DroneAI UG (haftungsbeschränkt)")
        assert '"DroneAI"' in query
        assert "UG" not in query

    def test_strips_ag(self):
        """AG suffix is stripped."""
        query = build_search_query("Hans Fischer", "DataVision AG")
        assert '"DataVision"' in query
        assert " AG" not in query


class TestBuildFallbackQuery:
    """Test fallback query construction."""

    def test_with_city(self):
        """Fallback query includes city."""
        query = build_fallback_query("Max Mustermann", "Berlin")
        assert "linkedin.com/in" in query
        assert '"Max Mustermann"' in query
        assert '"Berlin"' in query

    def test_without_city(self):
        """Fallback query without city omits city."""
        query = build_fallback_query("Max Mustermann")
        assert "linkedin.com/in" in query
        assert '"Max Mustermann"' in query
        # Should not contain empty quotes or None
        assert '""' not in query
        assert "None" not in query


class TestCleanCompanyName:
    """Test legal form stripping."""

    def test_gmbh(self):
        assert _clean_company_name("TechBot GmbH") == "TechBot"

    def test_ug(self):
        assert _clean_company_name("AI Labs UG") == "AI Labs"

    def test_ag(self):
        assert _clean_company_name("RoboVision AG") == "RoboVision"

    def test_ggmbh(self):
        assert _clean_company_name("SocialTech gGmbH") == "SocialTech"

    def test_no_legal_form(self):
        assert _clean_company_name("Pure Company Name") == "Pure Company Name"

    def test_gmbh_co_kg(self):
        assert _clean_company_name("Factory GmbH & Co. KG") == "Factory"


# ============================================================================
# Extraction Tests
# ============================================================================


class TestExtractNameAndHeadline:
    """Test LinkedIn title parsing."""

    def test_standard_pipe_linkedin(self):
        """Standard LinkedIn title: 'Name - Headline | LinkedIn'."""
        name, headline = _extract_name_and_headline("Max Müller - CEO at StartupX | LinkedIn")
        assert name == "Max Müller"
        assert headline == "CEO at StartupX"

    def test_no_headline(self):
        """Name only: 'Max Müller | LinkedIn'."""
        name, headline = _extract_name_and_headline("Max Müller | LinkedIn")
        assert name == "Max Müller"
        assert headline is None

    def test_dash_linkedin(self):
        """Alternative format: 'Name - Headline - LinkedIn'."""
        name, headline = _extract_name_and_headline("Max Müller - CEO at StartupX - LinkedIn")
        assert name == "Max Müller"
        # The headline includes everything after first ' - '
        assert "CEO at StartupX" in headline

    def test_empty_title(self):
        """Empty string returns empty name, no headline."""
        name, headline = _extract_name_and_headline("")
        assert name == ""
        assert headline is None


class TestExtractCompaniesFromText:
    """Test high-value company extraction."""

    def test_finds_faang(self):
        """Finds FAANG companies in text."""
        companies = _extract_companies_from_text("Previously worked at Google and then joined Meta")
        assert "Google" in companies
        assert "Meta" in companies

    def test_european_tech(self):
        """Finds European tech companies."""
        companies = _extract_companies_from_text("Experience at N26, now leading engineering at Celonis")
        assert "N26" in companies
        assert "Celonis" in companies

    def test_short_name_word_boundary(self):
        """Short names (≤4 chars) use word boundary matching."""
        # "SAP" should match when standalone
        companies = _extract_companies_from_text("Worked at SAP for 5 years")
        assert "Sap" in companies  # title-cased

        # "SAP" should NOT match inside another word
        companies = _extract_companies_from_text("He sapped the energy from the room")
        assert len(companies) == 0

    def test_deduplicates(self):
        """Same company mentioned twice → one result."""
        companies = _extract_companies_from_text("Google engineer, then Google manager")
        assert companies.count("Google") == 1

    def test_no_false_positive_ey(self):
        """'previously' does NOT match 'ernst & young' (ey was removed)."""
        companies = _extract_companies_from_text("He previously worked at a bank")
        # Should not find Ernst & Young in this text
        assert "Ernst & Young" not in companies
        assert len(companies) == 0

    def test_ernst_young_full_name(self):
        """'ernst & young' as full name is found."""
        companies = _extract_companies_from_text("Consulting at Ernst & Young for 3 years")
        assert "Ernst & Young" in companies

    def test_consulting_firms(self):
        """Finds consulting firms."""
        companies = _extract_companies_from_text("McKinsey consultant, then BCG partner")
        assert "Mckinsey" in companies
        assert "Bcg" in companies

    def test_empty_text(self):
        """Empty text returns empty list."""
        assert _extract_companies_from_text("") == []

    def test_no_companies(self):
        """Text with no high-value companies returns empty list."""
        companies = _extract_companies_from_text("He works at a small local bakery in Munich")
        assert len(companies) == 0


class TestExtractLocation:
    """Test location extraction from snippets."""

    def test_based_in(self):
        """Extracts 'based in City, Country'."""
        location = _extract_location("Software engineer based in Berlin, Germany")
        assert location is not None
        assert "Berlin" in location

    def test_located_in(self):
        """Extracts 'located in City'."""
        location = _extract_location("Currently located in Munich")
        assert location is not None
        assert "Munich" in location

    def test_known_city(self):
        """Finds known DACH cities."""
        location = _extract_location("CTO with 10 years in Hamburg tech scene")
        assert location is not None
        assert location == "Hamburg"

    def test_no_location(self):
        """Returns None when no location found."""
        location = _extract_location("Experienced software developer and team lead")
        assert location is None

    def test_vienna(self):
        """Finds Vienna (Wien)."""
        location = _extract_location("Working in Wien at a fintech startup")
        assert location is not None
        assert location == "Wien"


# ============================================================================
# Confidence Scoring Tests
# ============================================================================


class TestCalculateMatchConfidence:
    """Test 3-factor confidence scoring."""

    def test_perfect_match(self):
        """Exact name + company + city → high confidence."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Max Mustermann",
            company_name="TechBot GmbH",
            snippet="CEO at TechBot, based in Berlin",
            title="Max Mustermann - CEO at TechBot | LinkedIn",
            company_city="Berlin",
            location="Berlin",
        )
        assert score >= 0.95  # 0.50 (name) + 0.30 (company) + 0.20 (city) = 1.00

    def test_name_only(self):
        """Exact name match only → ~0.50."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Max Mustermann",
            company_name="UnrelatedCompany GmbH",
            snippet="Something about another company",
            title="Max Mustermann | LinkedIn",
            company_city=None,
            location=None,
        )
        assert 0.45 <= score <= 0.55

    def test_partial_name_match(self):
        """Fuzzy name match → lower score."""
        score = _calculate_match_confidence(
            officer_name="Maximilian Mustermann",
            name_from_search="Max Mustermann",
            company_name="TechBot GmbH",
            snippet="Something",
            title="Max Mustermann | LinkedIn",
            company_city=None,
            location=None,
        )
        # Name similarity ~0.80 → 0.25 points (0.70-0.85 bracket)
        assert 0.20 <= score <= 0.40

    def test_wrong_name_below_threshold(self):
        """Totally different name → low confidence."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Anna Schmidt",
            company_name="SomeCompany GmbH",
            snippet="Something completely different",
            title="Anna Schmidt | LinkedIn",
            company_city=None,
            location=None,
        )
        assert score < 0.40

    def test_company_partial_word_match(self):
        """First significant word of multi-word company matches → partial score."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Max Mustermann",
            company_name="Advanced Robotics Solutions GmbH",
            snippet="Working on advanced technology",
            title="Max Mustermann - Engineer | LinkedIn",
            company_city=None,
            location=None,
        )
        # Name match (0.50) + partial company match (0.15)
        assert 0.60 <= score <= 0.70

    def test_country_match_gives_partial_location_score(self):
        """Country-level match gives partial location score."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Max Mustermann",
            company_name="SomeCompany GmbH",
            snippet="Engineer in Germany",
            title="Max Mustermann | LinkedIn",
            company_city="Munich",
            location="Germany",
        )
        # Name (0.50) + country match (0.10)
        assert 0.55 <= score <= 0.65

    def test_score_capped_at_one(self):
        """Score never exceeds 1.0."""
        score = _calculate_match_confidence(
            officer_name="Max Mustermann",
            name_from_search="Max Mustermann",
            company_name="TechBot GmbH",
            snippet="CEO at TechBot in Berlin, TechBot headquarters",
            title="Max Mustermann - CEO at TechBot | LinkedIn",
            company_city="Berlin",
            location="Berlin, Germany",
        )
        assert score <= 1.0


# ============================================================================
# Parse Search Result Tests
# ============================================================================


class TestParseSearchResult:
    """Test full result parsing pipeline."""

    def test_complete_result(self):
        """Realistic LinkedIn result → correct OfficerLinkedInMatch."""
        match = parse_search_result(
            title="Max Mustermann - Co-Founder & CEO at TechBot | LinkedIn",
            snippet="Max Mustermann is Co-Founder & CEO at TechBot. Previously at Google and McKinsey. Based in Berlin, Germany.",
            url="https://www.linkedin.com/in/max-mustermann",
            officer_name="Max Mustermann",
            company_name="TechBot GmbH",
            company_city="Berlin",
        )

        assert isinstance(match, OfficerLinkedInMatch)
        assert match.linkedin_url == "https://www.linkedin.com/in/max-mustermann"
        assert match.name_from_search == "Max Mustermann"
        assert match.headline == "Co-Founder & CEO at TechBot"
        assert "Google" in match.previous_companies
        assert "Mckinsey" in match.previous_companies
        assert match.location is not None
        assert "Berlin" in match.location
        assert match.match_confidence >= 0.90
        assert match.source == "search_snippet"

    def test_minimal_result(self):
        """Minimal info → returns match with lower confidence."""
        match = parse_search_result(
            title="Some Person | LinkedIn",
            snippet="Profile information",
            url="https://linkedin.com/in/some-person",
            officer_name="Different Name",
            company_name="Unknown Company GmbH",
        )

        assert isinstance(match, OfficerLinkedInMatch)
        assert match.linkedin_url == "https://linkedin.com/in/some-person"
        assert match.match_confidence < 0.40  # Below threshold

    def test_snippet_truncated(self):
        """Long snippets are truncated to 500 chars."""
        long_snippet = "A" * 1000
        match = parse_search_result(
            title="Max Mustermann | LinkedIn",
            snippet=long_snippet,
            url="https://linkedin.com/in/max",
            officer_name="Max Mustermann",
            company_name="Company GmbH",
        )
        assert len(match.snippet) <= 500


# ============================================================================
# Error Classes
# ============================================================================


class TestRateLimitedError:
    """Test RateLimitedError exception."""

    def test_is_exception(self):
        """RateLimitedError can be raised and caught."""
        with pytest.raises(RateLimitedError):
            raise RateLimitedError("DuckDuckGo rate limited")

    def test_is_subclass_of_exception(self):
        """RateLimitedError is a proper Exception subclass."""
        assert issubclass(RateLimitedError, Exception)

    def test_message_preserved(self):
        """Error message is preserved."""
        try:
            raise RateLimitedError("test message")
        except RateLimitedError as e:
            assert "test message" in str(e)
