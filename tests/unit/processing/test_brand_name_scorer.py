"""
Tests for BrandNameScorer — non-German brand name heuristic.

Tests the ability to distinguish tech startup brand names
(Allonic, constellr, Naboo) from traditional German company names
(Müller Verwaltungs GmbH, Schmidt Immobilien AG).
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from processing.brand_name_scorer import BrandNameScorer, BrandNameScore


@pytest.fixture
def scorer():
    return BrandNameScorer()


class TestLegalFormFiltering:
    """Companies with non-startup legal forms should be skipped."""

    def test_ag_skipped(self, scorer):
        result = scorer.score("Siemens AG", city="München")
        assert not result.is_likely_tech_startup
        assert result.legal_form_signal == -99

    def test_ev_skipped(self, scorer):
        result = scorer.score("Sportverein e.V.", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_gmbh_co_kg_skipped(self, scorer):
        result = scorer.score("Müller GmbH & Co. KG", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_kg_skipped(self, scorer):
        result = scorer.score("Fischer KG", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_ug_accepted(self, scorer):
        result = scorer.score("Allonic UG (haftungsbeschränkt)", city="Berlin")
        assert result.legal_form_signal == 4

    def test_gmbh_accepted(self, scorer):
        result = scorer.score("constellr GmbH", city="München")
        assert result.legal_form_signal == 0


class TestNonGermanNameDetection:
    """The core heuristic: non-German brand names."""

    def test_obvious_tech_brands(self, scorer):
        tech_brands = [
            "Allonic UG (haftungsbeschränkt)",
            "constellr GmbH",
            "Naboo UG",
            "Xentral GmbH",
            "Celonis GmbH",
            "Personio GmbH",
            "Forto GmbH",
            "Taxfix GmbH",
        ]
        for name in tech_brands:
            result = scorer.score(name, city="Berlin")
            assert result.non_german_signal > 0, \
                f"'{name}' should be detected as non-German brand"

    def test_german_traditional_names(self, scorer):
        german_names = [
            "Müller Verwaltungs GmbH",
            "Schmidt Immobilien GmbH",
            "Berliner Baugesellschaft GmbH",
            "Schneider Handelsgesellschaft GmbH",
            "Fischer Dienstleistung GmbH",
        ]
        for name in german_names:
            result = scorer.score(name, city="Berlin")
            assert result.non_german_signal == 0, \
                f"'{name}' should be detected as German"

    def test_german_surname_at_start(self, scorer):
        """Companies starting with common German surnames."""
        names = [
            "Hoffmann GmbH",
            "Weber GmbH",
            "Becker UG",
        ]
        for name in names:
            result = scorer.score(name, city="Berlin")
            assert result.non_german_signal == 0, \
                f"'{name}' should be detected as German surname"

    def test_short_brand_signal(self, scorer):
        result = scorer.score("Naboo UG", city="Berlin")
        assert result.short_brand_signal >= 2


class TestCombinedScoring:
    """Integration tests for the full scoring pipeline."""

    def test_ug_berlin_non_german_passes(self, scorer):
        """UG in Berlin with non-German name -> definitely a candidate."""
        result = scorer.score("Allonic UG (haftungsbeschränkt)", city="Berlin")
        assert result.is_likely_tech_startup
        assert result.total_score >= 7  # UG(4) + Berlin(3) + non-German(3) - minimum

    def test_gmbh_munich_non_german_passes(self, scorer):
        """GmbH in Munich with non-German name -> candidate."""
        result = scorer.score("constellr GmbH", city="München")
        assert result.is_likely_tech_startup

    def test_gmbh_passau_german_name_fails(self, scorer):
        """Traditional GmbH outside hub -> not a candidate."""
        result = scorer.score("Müller Verwaltungs GmbH", city="Passau")
        assert not result.is_likely_tech_startup

    def test_gmbh_berlin_german_name_low_score(self, scorer):
        """Traditional GmbH in Berlin -> low score despite location."""
        result = scorer.score("Müller Verwaltungs GmbH", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_startup_name_patterns_boost(self, scorer):
        """Names matching STARTUP_NAME_PATTERNS get extra signal."""
        result = scorer.score("Data Labs UG", city="Berlin")
        assert result.name_pattern_signal >= 3  # Labs (+3), Data (+1)
        assert result.is_likely_tech_startup

    def test_labs_suffix_detected(self, scorer):
        result = scorer.score("SomeBrand Labs GmbH", city="Berlin")
        assert result.name_pattern_signal >= 3

    def test_sme_patterns_penalize(self, scorer):
        """SME patterns reduce score."""
        result = scorer.score("Verwaltung Digital UG", city="Berlin")
        assert any('SME' in s for s in result.negative_signals)


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_name(self, scorer):
        result = scorer.score("", city="Berlin")
        assert not result.is_likely_tech_startup
        assert result.total_score == 0

    def test_no_city(self, scorer):
        """Works without city — just no location bonus."""
        result = scorer.score("Allonic UG (haftungsbeschränkt)")
        assert result.location_signal == 0
        # UG(4) + non-German(3) + short(2) = 9, still above threshold
        assert result.is_likely_tech_startup

    def test_unknown_legal_form(self, scorer):
        """Name without recognizable legal form."""
        result = scorer.score("SomeRandomName")
        assert result.legal_form_signal == 0

    def test_brand_part_extraction(self, scorer):
        """Legal form is correctly stripped."""
        result = scorer.score("Allonic UG (haftungsbeschränkt)", city="Berlin")
        assert result.brand_part == "Allonic"

    def test_brand_part_gmbh(self, scorer):
        result = scorer.score("constellr GmbH", city="München")
        assert result.brand_part == "constellr"
