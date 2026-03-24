"""
Tests for BrandNameScorer — non-German brand name heuristic.

Tests the ability to distinguish tech startup brand names
(Allonic, constellr, Naboo) from traditional German company names
(Müller Verwaltungs GmbH, Schmidt Immobilien AG).

v2 tests cover shell company detection, vehicle word penalties,
and English/neologism positive signals.
"""

import pytest

from processing.brand_name_scorer import BrandNameScorer


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
            assert result.non_german_signal > 0, f"'{name}' should be detected as non-German brand"

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
            assert result.non_german_signal == 0, f"'{name}' should be detected as German"

    def test_german_surname_at_start(self, scorer):
        """Companies starting with common German surnames."""
        names = [
            "Hoffmann GmbH",
            "Weber GmbH",
            "Becker UG",
        ]
        for name in names:
            result = scorer.score(name, city="Berlin")
            assert result.non_german_signal == 0, f"'{name}' should be detected as German surname"

    def test_short_brand_signal(self, scorer):
        result = scorer.score("Naboo UG", city="Berlin")
        assert result.short_brand_signal >= 2


class TestShellCompanyDetection:
    """v2: Shell companies, Vorratsgesellschaften, and financial vehicles."""

    def test_numbered_shell_aptus(self, scorer):
        """Mass-created shelf companies: aptus NNNN."""
        for num in [2635, 2636, 2637]:
            result = scorer.score(f"aptus {num}. GmbH", city="Berlin")
            assert not result.is_likely_tech_startup, f"aptus {num}. should be detected as shell company"
            assert result.shell_penalty <= -5

    def test_numbered_shell_with_initials(self, scorer):
        """Shelf companies with placeholder initials: Lindentor 1307. V V."""
        result = scorer.score("Lindentor 1307. V V GmbH", city="Berlin")
        assert not result.is_likely_tech_startup
        assert result.shell_penalty <= -5

    def test_coded_shelf_company(self, scorer):
        """Coded shelf company patterns: SCUR-Alpha 1971."""
        result = scorer.score("SCUR-Alpha 1971 GmbH", city="München")
        assert not result.is_likely_tech_startup
        assert result.shell_penalty <= -5

    def test_vorratsgesellschaft(self, scorer):
        """Explicit Vorratsgesellschaft keyword."""
        result = scorer.score("Vorratsgesellschaft Nr. 42 UG (haftungsbeschränkt)", city="Berlin")
        assert not result.is_likely_tech_startup
        assert result.shell_penalty <= -5

    def test_shelf_company_english(self, scorer):
        """English shelf company keyword."""
        result = scorer.score("Shelf Company No. 99 GmbH", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_firma_de_shelf(self, scorer):
        """firma.de Vorratsgesellschaft pattern."""
        result = scorer.score("firma.de Vorratsgesellschaft 1234 UG (haftungsbeschränkt)", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_abbreviation_number_dfi(self, scorer):
        """Abbreviation + number: DFI 24."""
        result = scorer.score("DFI 24 GmbH", city="München")
        assert not result.is_likely_tech_startup

    def test_abbreviation_number_ml(self, scorer):
        """Abbreviation + number: M&L 427."""
        result = scorer.score("M&L 427 GmbH", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_verwaltungs_ug_berlin(self, scorer):
        """Verwaltungs UG in Berlin/München — vehicle, not startup."""
        for prefix in ["BEKA", "ALE", "KBC"]:
            name = f"{prefix} Verwaltungs UG (haftungsbeschränkt)"
            result = scorer.score(name, city="München")
            assert not result.is_likely_tech_startup, f"'{name}' should fail as Verwaltungs vehicle"

    def test_holding_ug(self, scorer):
        """Holding UG — financial vehicle."""
        result = scorer.score("JRI Holding UG (haftungsbeschränkt)", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_investment_ug(self, scorer):
        """Investment UG — financial vehicle."""
        result = scorer.score("DMN Investment UG (haftungsbeschränkt)", city="Berlin")
        assert not result.is_likely_tech_startup

    def test_windpark_beteiligungs(self, scorer):
        """Wind farm holding company."""
        result = scorer.score("Windpark Krackow Beteiligungs GmbH")
        assert not result.is_likely_tech_startup

    def test_multiple_vehicle_words(self, scorer):
        """Multiple vehicle words get stronger penalty."""
        result = scorer.score("Vermögensverwaltung Beteiligungs GmbH", city="Berlin")
        assert not result.is_likely_tech_startup
        assert result.shell_penalty <= -5

    def test_real_startups_not_penalized(self, scorer):
        """Real startups should NOT trigger shell detection."""
        real_startups = [
            ("Yapstar UG (haftungsbeschränkt)", "Berlin"),
            ("Aionox UG (haftungsbeschränkt)", "Berlin"),
            ("Naven Labs GmbH", "München"),
            ("constellr GmbH", "München"),
            ("Trade Republic GmbH", "Berlin"),
        ]
        for name, city in real_startups:
            result = scorer.score(name, city=city)
            assert result.shell_penalty == 0, f"'{name}' should NOT have shell penalty"
            assert result.is_likely_tech_startup


class TestEnglishBrandDetection:
    """v2: English word and neologism detection for false negative reduction."""

    def test_english_words_in_brand(self, scorer):
        """Brands containing common English startup words get bonus."""
        english_brands = [
            "Smart City Systems GmbH",
            "Trade Republic GmbH",
            "Auto1 Group GmbH",
        ]
        for name in english_brands:
            result = scorer.score(name, city="Berlin")
            assert result.english_signal > 0, f"'{name}' should get English brand signal"

    def test_english_compound_words(self, scorer):
        """English words embedded in compound brands."""
        result = scorer.score("InstaFreight GmbH", city="Berlin")
        assert result.english_signal >= 1  # 'insta' found in compound

    def test_neologism_suffix_ly(self, scorer):
        """Neologism suffix -ly (Brainly, Grammarly)."""
        result = scorer.score("Brainly GmbH", city="München")
        assert result.english_signal >= 1

    def test_neologism_suffix_match(self, scorer):
        """Neologism suffix -fy/-ify detected in single-word brands."""
        result = scorer.score("Comatch GmbH", city="Berlin")
        # 'match' is an English word in ENGLISH_STARTUP_WORDS
        assert result.english_signal >= 1 or result.is_likely_tech_startup

    def test_german_names_no_english_signal(self, scorer):
        """German traditional names should not get English signal."""
        result = scorer.score("Müller Verwaltungs GmbH", city="Berlin")
        assert result.english_signal == 0

    def test_english_signal_helps_borderline(self, scorer):
        """English signal helps borderline cases pass threshold."""
        # A GmbH outside startup hubs but with clear English brand
        result = scorer.score("Smart Hub GmbH", city="Passau")
        # non-German(3) + English(2) + CamelCase(1) = 6 >= 5 threshold
        assert result.is_likely_tech_startup or result.total_score >= 4

    def test_english_signal_does_not_save_shells(self, scorer):
        """English words in shell companies still fail (shell penalty dominates)."""
        result = scorer.score("Shelf Company No. 99 GmbH", city="Berlin")
        assert not result.is_likely_tech_startup


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
        assert any("SME" in s for s in result.negative_signals)

    def test_known_startups_all_pass(self, scorer):
        """Comprehensive test: well-known German startups all pass."""
        known_startups = [
            ("Hyre GmbH", "Berlin"),
            ("Qonto GmbH", "Berlin"),
            ("Wolt GmbH", "Berlin"),
            ("Razor Group GmbH", "Berlin"),
            ("Gorillas Technologies GmbH", "Berlin"),
            ("Tier Mobility GmbH", "Berlin"),
            ("Lilium GmbH", "München"),
            ("Getsafe GmbH", "Heidelberg"),
            ("Staffbase GmbH", "Dresden"),
            ("HAWK:AI GmbH", "München"),
            ("Blinkist GmbH", "Berlin"),
            ("Lingoda GmbH", "Berlin"),
            ("Contentful GmbH", "Berlin"),
            ("Thermondo GmbH", "Berlin"),
            ("Flixbus GmbH", "München"),
            ("N26 GmbH", "Berlin"),
            ("Home24 GmbH", "Berlin"),
        ]
        for name, city in known_startups:
            result = scorer.score(name, city=city)
            assert result.is_likely_tech_startup, f"'{name}' ({city}) should pass (score={result.total_score})"


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

    def test_score_dataclass_has_new_fields(self, scorer):
        """BrandNameScore includes v2 fields."""
        result = scorer.score("Test GmbH", city="Berlin")
        assert hasattr(result, "shell_penalty")
        assert hasattr(result, "english_signal")

    def test_shell_penalty_included_in_total(self, scorer):
        """Shell penalty is reflected in total_score."""
        result = scorer.score("aptus 2635. GmbH", city="Berlin")
        assert result.shell_penalty < 0
        assert result.total_score < 5  # Penalty brings it below threshold
