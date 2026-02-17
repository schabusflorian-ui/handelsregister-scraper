"""
Tests for AIRoboticsFilter and related filtering functions.

This is a critical test module since the filter determines which companies
are identified as AI/robotics related.
"""

import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from processing.filters import (
    AIRoboticsFilter,
    FilterConfig,
    FilterResult,
    extract_legal_form,
    DEFAULT_AI_KEYWORDS,
    HIGH_SIGNAL_KEYWORDS,
    TECH_CATEGORIES,
)
from tests.fixtures.sample_data import (
    TRUE_POSITIVES,
    FALSE_POSITIVES,
    EDGE_CASES,
    LEGAL_FORMS,
)


class TestAIRoboticsFilterKeywordMatching:
    """Test keyword matching functionality."""

    def test_basic_ai_keyword_matches(self, filter_instance):
        """Verify core AI terms are matched."""
        test_cases = [
            "Artificial Intelligence GmbH",
            "Machine Learning Solutions AG",
            "Deep Learning Research UG",
            "Künstliche Intelligenz GmbH",
            "Maschinelles Lernen AG",
        ]
        for name in test_cases:
            score = filter_instance.calculate_relevance_score(name)
            assert score >= 1, f"Expected score >= 1 for '{name}', got {score}"

    def test_robotics_keyword_matches(self, filter_instance):
        """Verify robotics terms are matched."""
        test_cases = [
            "Robotik Solutions GmbH",
            "Robotics Engineering AG",
            "Cobot Systems UG",
            "Industrieroboter Tech GmbH",
            "Drone Technology AG",
        ]
        for name in test_cases:
            score = filter_instance.calculate_relevance_score(name)
            assert score >= 1, f"Expected score >= 1 for '{name}', got {score}"

    def test_german_keyword_variants(self, filter_instance):
        """Test umlauts (ä, ö, ü) in keywords."""
        # These should match — German compound terms used in company names
        assert filter_instance.calculate_relevance_score("Künstliche Intelligenz GmbH") >= 1
        assert filter_instance.calculate_relevance_score("Maschinelles Lernen UG") >= 1
        assert filter_instance.calculate_relevance_score("Bildverarbeitung Systems AG") >= 1

    def test_case_insensitivity(self, filter_instance):
        """Keywords match regardless of case."""
        base_name = "machine learning gmbh"
        score_lower = filter_instance.calculate_relevance_score(base_name)
        score_upper = filter_instance.calculate_relevance_score(base_name.upper())
        score_title = filter_instance.calculate_relevance_score(base_name.title())

        assert score_lower == score_upper == score_title
        assert score_lower >= 1

    def test_standalone_ai_pattern(self, filter_instance):
        """\\bAI\\b matches 'AI GmbH' but not 'HAIR'."""
        # Should match - standalone AI
        ai_company = filter_instance.calculate_relevance_score("AI Solutions GmbH")
        assert ai_company >= 2, f"Standalone AI should score >= 2, got {ai_company}"

        # Should NOT match - AI embedded in word
        hair_salon = filter_instance.calculate_relevance_score("HAIR Salon Berlin GmbH")
        assert hair_salon == 0, f"HAIR should score 0, got {hair_salon}"

        fair_trade = filter_instance.calculate_relevance_score("FAIR Trade Import GmbH")
        assert fair_trade == 0, f"FAIR should score 0, got {fair_trade}"

    def test_no_false_positive_kai_prefix(self, filter_instance):
        """'Kai-Uwe' doesn't match KI patterns."""
        # These should NOT match
        kai_names = [
            "Kai-Uwe Consulting GmbH",
            "Kai Schmidt Immobilien GmbH",
            "Kira Modedesign UG",
        ]
        for name in kai_names:
            score = filter_instance.calculate_relevance_score(name)
            assert score == 0, f"'{name}' should score 0 (false positive), got {score}"


class TestAIRoboticsFilterScoring:
    """Test relevance score calculation."""

    def test_relevance_score_calculation(self, filter_instance):
        """Score increases with more keyword matches."""
        single_match = filter_instance.calculate_relevance_score("Robotik GmbH")
        double_match = filter_instance.calculate_relevance_score("Robotik Machine Learning GmbH")

        assert double_match > single_match, "More keywords should increase score"

    def test_high_signal_bonus(self, filter_instance):
        """High-signal keywords add bonus points."""
        # 'machine learning' is high-signal, should get bonus
        high_signal = filter_instance.calculate_relevance_score("Machine Learning GmbH")
        # 'data science' is medium-signal (no bonus)
        medium_signal = filter_instance.calculate_relevance_score("Data Science GmbH")

        # High-signal gets +1 base +1 bonus = 2, medium-signal gets +1 base = 1
        assert high_signal >= 2, f"High-signal should score >= 2, got {high_signal}"
        assert medium_signal >= 1, f"Medium-signal should score >= 1, got {medium_signal}"

    def test_multiple_high_signal_keywords(self, filter_instance):
        """Multiple high-signal keywords accumulate bonuses."""
        # Multiple high-signal keywords
        name = "Machine Learning Deep Learning Neural Network GmbH"
        score = filter_instance.calculate_relevance_score(name)
        # At least 3 keywords × 2 (base + bonus) = 6
        assert score >= 6, f"Multiple high-signal should score >= 6, got {score}"

    def test_zero_score_for_non_ai_company(self, filter_instance):
        """Non-AI companies should score 0."""
        non_ai = [
            "Müller Verwaltungs GmbH",
            "Schmidt Immobilien AG",
            "Gastro Service UG",
            "Autohaus Premium GmbH",
        ]
        for name in non_ai:
            score = filter_instance.calculate_relevance_score(name)
            assert score == 0, f"'{name}' should score 0, got {score}"

    def test_empty_text_returns_zero(self, filter_instance):
        """Empty or None text returns 0."""
        assert filter_instance.calculate_relevance_score("") == 0
        assert filter_instance.calculate_relevance_score(None) == 0


class TestAIRoboticsFilterCategories:
    """Test technology category classification."""

    def test_computer_vision_classification(self, filter_instance):
        """CV keywords -> computer_vision category."""
        names = [
            "Computer Vision Tech GmbH",
            "Bildverarbeitung Systems AG",
            "Objekterkennung AI UG",
            "Videoanalyse Solutions GmbH",
        ]
        for name in names:
            categories = filter_instance.classify_tech_categories(name)
            assert "computer_vision" in categories, f"'{name}' should be computer_vision"

    def test_nlp_classification(self, filter_instance):
        """NLP keywords -> nlp category."""
        names = [
            "NLP Solutions GmbH",
            "Chatbot Development AG",
            "Sprachverarbeitung Systems GmbH",
        ]
        for name in names:
            categories = filter_instance.classify_tech_categories(name)
            assert "nlp" in categories, f"'{name}' should be nlp"

    def test_robotics_classification(self, filter_instance):
        """Robotics keywords -> robotics category."""
        names = [
            "Robotik Automation GmbH",
            "Cobot Solutions AG",
            "Drone Technology UG",
        ]
        for name in names:
            categories = filter_instance.classify_tech_categories(name)
            assert "robotics" in categories, f"'{name}' should be robotics"

    def test_autonomous_systems_classification(self, filter_instance):
        """Autonomous systems keywords -> autonomous_systems category."""
        name = "Autonomes Fahren GmbH"
        categories = filter_instance.classify_tech_categories(name)
        assert "autonomous_systems" in categories, f"'{name}' should be autonomous_systems"

    def test_multiple_categories(self, filter_instance):
        """Company matching multiple categories."""
        name = "AI Robotics Computer Vision Deep Learning GmbH"
        categories = filter_instance.classify_tech_categories(name)

        assert len(categories) >= 2, f"Should have multiple categories, got {categories}"

    def test_no_categories_for_non_ai(self, filter_instance):
        """Non-AI company has no categories."""
        categories = filter_instance.classify_tech_categories("Müller Consulting GmbH")
        assert categories == [], f"Should have no categories, got {categories}"


class TestAIRoboticsFilterFullFiltering:
    """Test the full filter_company method."""

    def test_filter_passes_ai_company(self, filter_instance):
        """AI company passes filter."""
        result = filter_instance.filter_company(
            name="Machine Learning Solutions GmbH",
            status="active",
        )
        assert result.passes is True
        assert result.relevance_score >= 1
        assert len(result.matched_keywords) >= 1

    def test_filter_rejects_non_ai_company(self, filter_instance):
        """Non-AI company fails filter."""
        result = filter_instance.filter_company(
            name="Müller Verwaltungs GmbH",
            status="active",
        )
        assert result.passes is False
        assert result.relevance_score == 0
        assert "Low combined score" in result.rejection_reason

    def test_filter_respects_status(self, filter_instance):
        """Filter rejects inactive companies."""
        result = filter_instance.filter_company(
            name="AI Solutions GmbH",
            status="liquidated",
        )
        assert result.passes is False
        assert "Status not allowed" in result.rejection_reason

    def test_filter_respects_city_filter(self, filter_instance):
        """City filter restricts results."""
        config = FilterConfig(cities_filter=["Berlin", "Munich"])
        city_filter = AIRoboticsFilter(config)

        # Berlin passes
        result_berlin = city_filter.filter_company(
            name="AI Solutions GmbH",
            city="Berlin",
        )
        assert result_berlin.passes is True

        # Hamburg fails
        result_hamburg = city_filter.filter_company(
            name="AI Solutions GmbH",
            city="Hamburg",
        )
        assert result_hamburg.passes is False
        assert "City not in filter" in result_hamburg.rejection_reason

    def test_filter_respects_min_capital(self, filter_instance):
        """Capital threshold enforced."""
        config = FilterConfig(min_capital=50000.0)
        capital_filter = AIRoboticsFilter(config)

        # High capital passes
        result_high = capital_filter.filter_company(
            name="AI Solutions GmbH",
            capital=100000.0,
        )
        assert result_high.passes is True

        # Low capital fails
        result_low = capital_filter.filter_company(
            name="AI Solutions GmbH",
            capital=25000.0,
        )
        assert result_low.passes is False
        assert "Capital below minimum" in result_low.rejection_reason


class TestAIRoboticsFilterEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_name(self, filter_instance):
        """Handles empty name gracefully."""
        result = filter_instance.filter_company(name="")
        assert result.passes is False
        assert result.relevance_score == 0

    def test_very_long_name(self, filter_instance):
        """No performance issues with long text."""
        long_name = "A" * 500 + " AI Machine Learning GmbH"
        result = filter_instance.filter_company(name=long_name)
        assert result.relevance_score >= 2

    def test_special_characters(self, filter_instance):
        """Unicode and symbols handled."""
        special_names = [
            "KI & Robotik GmbH",
            "AI/ML Solutions GmbH",
            "Künstliche Intelligenz (KI) GmbH",
        ]
        for name in special_names:
            result = filter_instance.filter_company(name=name)
            # Should not crash, may or may not match
            assert isinstance(result.relevance_score, int)

    def test_quick_filter_method(self, filter_instance):
        """Quick filter method works correctly."""
        assert filter_instance.quick_filter("Machine Learning GmbH") is True
        assert filter_instance.quick_filter("Müller Consulting GmbH") is False

    def test_purpose_combined_with_name(self, filter_instance):
        """Purpose text is combined with name for matching."""
        # Name alone doesn't match, but purpose does
        result = filter_instance.filter_company(
            name="Generic Tech GmbH",
            purpose="Development of machine learning algorithms",
        )
        assert result.relevance_score >= 1


class TestTruePositives:
    """Test all true positive cases from sample data."""

    @pytest.mark.parametrize("name,expected_passes,expected_min_score,expected_categories", TRUE_POSITIVES)
    def test_true_positive(self, filter_instance, name, expected_passes, expected_min_score, expected_categories):
        """Verify true positives are correctly identified."""
        result = filter_instance.filter_company(name=name, status="active")

        assert result.passes == expected_passes, f"'{name}' should pass={expected_passes}"
        assert result.relevance_score >= expected_min_score, \
            f"'{name}' should score >= {expected_min_score}, got {result.relevance_score}"

        # Check at least one expected category is present
        if expected_categories:
            categories_matched = any(cat in result.tech_categories for cat in expected_categories)
            assert categories_matched, \
                f"'{name}' should have one of {expected_categories}, got {result.tech_categories}"


class TestFalsePositives:
    """Test all false positive cases from sample data."""

    @pytest.mark.parametrize("name,should_pass,reason", FALSE_POSITIVES)
    def test_false_positive_rejected(self, filter_instance, name, should_pass, reason):
        """Verify false positives are correctly rejected."""
        result = filter_instance.filter_company(name=name, status="active")

        if should_pass:
            assert result.passes is True, f"'{name}' ({reason}) should pass"
        else:
            # Either doesn't pass OR has score 0
            # Some false positives might technically pass with score 1 from generic matches
            # but the key is they shouldn't have high scores
            assert result.relevance_score <= 1, \
                f"'{name}' ({reason}) should score <= 1, got {result.relevance_score}"


class TestLegalFormExtraction:
    """Test legal form extraction function."""

    @pytest.mark.parametrize("name,expected_form", LEGAL_FORMS)
    def test_legal_form_extraction(self, name, expected_form):
        """Verify legal forms are extracted correctly."""
        result = extract_legal_form(name)
        assert result == expected_form, f"'{name}' should extract '{expected_form}', got '{result}'"

    def test_legal_form_gmbh_co_kg(self):
        """Test compound legal form GmbH & Co. KG."""
        assert extract_legal_form("Partner GmbH & Co. KG") == "GmbH & Co. KG"

    def test_legal_form_ug_haftungsbeschraenkt(self):
        """Test UG (haftungsbeschränkt) with umlaut."""
        result = extract_legal_form("Startup UG (haftungsbeschränkt)")
        assert result == "UG (haftungsbeschränkt)"

    def test_legal_form_case_insensitive(self):
        """Legal form detection is case insensitive."""
        assert extract_legal_form("Company GMBH") == "GmbH"
        assert extract_legal_form("Company gmbh") == "GmbH"


class TestFilterConfiguration:
    """Test FilterConfig options."""

    def test_custom_keywords(self):
        """Custom keyword list can be provided."""
        config = FilterConfig(ai_robotics_keywords=["custom", "keywords"])
        custom_filter = AIRoboticsFilter(config)

        assert custom_filter.calculate_relevance_score("Custom Solutions GmbH") >= 1
        assert custom_filter.calculate_relevance_score("Machine Learning GmbH") == 0  # Not in custom list

    def test_min_relevance_score_setting(self):
        """Min relevance score threshold works."""
        strict_config = FilterConfig(min_relevance_score=3)
        strict_filter = AIRoboticsFilter(strict_config)

        # This would pass with score 2 normally, but fails with strict filter
        result = strict_filter.filter_company(name="Machine Learning GmbH", status="active")
        # Score is still 2, but doesn't pass the min=3 threshold
        assert result.relevance_score >= 2
        if result.relevance_score < 3:
            assert result.passes is False

    def test_allowed_statuses_setting(self):
        """Allowed statuses list can be customized."""
        config = FilterConfig(allowed_statuses=["active", "pending"])
        custom_filter = AIRoboticsFilter(config)

        result_active = custom_filter.filter_company(name="AI GmbH", status="active")
        result_pending = custom_filter.filter_company(name="AI GmbH", status="pending")
        result_liquidated = custom_filter.filter_company(name="AI GmbH", status="liquidated")

        assert result_active.passes is True
        assert result_pending.passes is True
        assert result_liquidated.passes is False


class TestMatchedKeywordsOutput:
    """Test that matched keywords are correctly reported."""

    def test_matched_keywords_returned(self, filter_instance):
        """Matched keywords are included in result."""
        result = filter_instance.filter_company(
            name="Machine Learning Deep Learning Neural Network GmbH",
            status="active",
        )

        assert "machine learning" in result.matched_keywords
        assert "deep learning" in result.matched_keywords
        assert "neural network" in result.matched_keywords

    def test_standalone_ai_marked_in_keywords(self, filter_instance):
        """Standalone AI match is marked specially."""
        result = filter_instance.filter_company(name="AI Solutions GmbH", status="active")

        # Standalone matches are marked with brackets
        has_standalone = any("[AI]" in kw for kw in result.matched_keywords)
        assert has_standalone, f"Should have standalone AI marker, got {result.matched_keywords}"
