"""
Unit tests for signals/common.py — the normalization + scoring helpers
that every adapter and the joiner depend on. If these drift, the whole
pipeline drifts, so pin the edge cases that previously caused real-data
false matches.
"""

from datetime import datetime, timezone

import pytest

from signals.common import (
    SIGNAL_TYPES,
    SIGNAL_WEIGHTS,
    composite_score,
    ensure_json,
    iso_now,
    name_similarity,
    normalize_domain,
    normalize_github,
    normalize_name,
    recency_multiplier,
)

# ---------------------------------------------------------------------------
# normalize_domain
# ---------------------------------------------------------------------------


class TestNormalizeDomain:
    def test_strips_scheme_and_www(self):
        assert normalize_domain("https://www.Example.COM/path?q=1") == "example.com"

    def test_bare_host(self):
        assert normalize_domain("acme.de") == "acme.de"

    def test_rejects_generic_hosts(self):
        # LinkedIn/GitHub/etc. are too generic to be stealth-entity join keys
        for u in ("linkedin.com/in/foo", "github.com/bar", "https://news.ycombinator.com/x", "reddit.com/r/x"):
            assert normalize_domain(u) is None, f"should reject {u}"

    def test_rejects_non_domain_input(self):
        for u in ("not a url", "", None, "tel:+123", "mailto:x@y.com"):
            assert normalize_domain(u) is None, f"should reject {u!r}"

    def test_rejects_single_label(self):
        # Must contain a dot
        assert normalize_domain("localhost") is None


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


class TestNormalizeName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Acme GmbH", "acme"),
            ("ACME Robotics GmbH i.G.", "acme robotics i g"),  # "i g" survives — expected
            ("Mu-Zero.AI Technologies, Inc.", "mu zero ai"),
            ("Fraunhofer-Institut", "fraunhofer institut"),
        ],
    )
    def test_strips_legal_forms_and_punct(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_handles_accents(self):
        assert normalize_name("Müller GmbH") == "muller"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_input(self, raw):
        assert normalize_name(raw) is None


# ---------------------------------------------------------------------------
# normalize_github
# ---------------------------------------------------------------------------


class TestNormalizeGithub:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("https://github.com/acme", "acme"),
            ("https://www.github.com/ACME", "acme"),
            ("@acme", "acme"),
            ("acme/repo", "acme"),
            ("ACME", "acme"),
        ],
    )
    def test_normalizes_handles(self, raw, expected):
        assert normalize_github(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "acme org"])
    def test_rejects_junk(self, raw):
        assert normalize_github(raw) is None


# ---------------------------------------------------------------------------
# name_similarity — THE critical function for cross-source matching
# ---------------------------------------------------------------------------


class TestNameSimilarity:
    """These are regression tests pinned against real-data false matches
    we saw during the signals pipeline rollout. If any of these change
    behavior, double-check it's actually an improvement."""

    def test_exact_match(self):
        assert name_similarity("Peec AI", "Peec AI") == 1.0

    def test_legal_form_tolerance(self):
        # "Peec AI" vs "Peec AI GmbH" — legal form strip + Jaccard
        assert name_similarity("Peec AI", "Peec AI GmbH") >= 0.9

    def test_distinctive_single_token(self):
        # "Fraunhofer" is 10 chars — distinctive single-token match allowed
        assert name_similarity("Fraunhofer", "Fraunhofer Institute") == 1.0
        assert name_similarity("Mistral", "Mistral AI") == 1.0
        assert name_similarity("OpenAI", "OpenAI GmbH") == 1.0

    def test_city_name_does_not_match(self):
        # The actual production bug this guards: "Berlin" false-matching
        # dozens of "Berlin Robotics GmbH"-style legal names.
        assert name_similarity("Berlin", "Berlin Robotics GmbH") == 0.0

    def test_generic_business_word_does_not_match(self):
        assert name_similarity("Startup", "Startup Robotics GmbH") == 0.0
        assert name_similarity("Studio", "Studio Alpha Beta") == 0.0

    def test_short_single_token_requires_6_chars_for_overlap(self):
        # "acme" (4 chars) is BELOW the 6-char distinctive-token threshold,
        # so even in loose mode it does not trigger the overlap-coefficient
        # bonus — scores drop to jaccard (1/2 = 0.5) in both modes.
        assert name_similarity("acme", "acme robotics", strict=False) == 0.5
        assert name_similarity("acme", "acme robotics", strict=True) == 0.5

    def test_6char_distinctive_token_bonus_in_loose(self):
        # "fraunhofer" is 10 chars — above the 6-char distinctive threshold,
        # overlap-coefficient bonus kicks in in loose mode. Use "institut"
        # and "foundation" because "labs" is a legal-form variant that
        # gets stripped before matching.
        assert name_similarity("fraunhofer", "fraunhofer institut foundation", strict=False) == 1.0
        assert name_similarity("fraunhofer", "fraunhofer institut foundation", strict=True) < 0.75

    def test_cross_type_false_match_guard(self):
        # The production bug: "M.C - Ecosystem GmbH" bundling with
        # "Bozorg Kalantar – Open Ecosystem". Shared single distinctive
        # token "ecosystem" — allowed in loose mode (overlap=1.0), BLOCKED
        # in strict mode because we require 2+ shared tokens.
        a, b = "M.C Ecosystem", "Bozorg Kalantar Open Ecosystem"
        # Loose: distinctive single token "ecosystem" (9 chars) triggers overlap
        assert name_similarity(a, b, strict=False) >= 0.75
        # Strict: single-token overlap path disabled → below threshold
        assert name_similarity(a, b, strict=True) < 0.75

    def test_no_shared_meaningful_tokens(self):
        assert name_similarity("Foo Labs", "Bar Corp") == 0.0

    def test_handles_missing_input(self):
        assert name_similarity(None, "foo") == 0.0
        assert name_similarity("foo", "") == 0.0


# ---------------------------------------------------------------------------
# recency_multiplier
# ---------------------------------------------------------------------------


class TestRecencyMultiplier:
    def _mk(self, days_ago: int) -> str:
        from datetime import timedelta

        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_fresh_is_full_weight(self):
        assert recency_multiplier(self._mk(7)) == 1.0
        assert recency_multiplier(self._mk(29)) == 1.0

    def test_decay_steps(self):
        assert recency_multiplier(self._mk(60)) == 0.85
        assert recency_multiplier(self._mk(120)) == 0.65
        assert recency_multiplier(self._mk(200)) == 0.45
        assert recency_multiplier(self._mk(500)) == 0.25

    def test_missing_date_middle_bucket(self):
        assert recency_multiplier(None) == 0.5


# ---------------------------------------------------------------------------
# composite_score — this is the ranking spine
# ---------------------------------------------------------------------------


class TestCompositeScore:
    def setup_method(self):
        self.fresh = (datetime.now(timezone.utc)).isoformat()

    def test_empty_scores_zero(self):
        assert composite_score([], self.fresh) == 0.0

    def test_single_signal_weight(self):
        s = composite_score(["linkedin_stealth"], self.fresh)
        assert s == pytest.approx(SIGNAL_WEIGHTS["linkedin_stealth"])

    def test_two_signals_get_bonus(self):
        s = composite_score(["linkedin_stealth", "handelsregister_new"], self.fresh)
        base = SIGNAL_WEIGHTS["linkedin_stealth"] + SIGNAL_WEIGHTS["handelsregister_new"]
        assert s == pytest.approx(base + 15.0)  # 2+ bonus

    def test_three_signals_more_bonus(self):
        s = composite_score(
            ["linkedin_stealth", "handelsregister_new", "crt_domain_new"],
            self.fresh,
        )
        base = (
            SIGNAL_WEIGHTS["linkedin_stealth"]
            + SIGNAL_WEIGHTS["handelsregister_new"]
            + SIGNAL_WEIGHTS["crt_domain_new"]
        )
        assert s == pytest.approx(min(100.0, base + 15 + 15))

    def test_duplicate_types_dedupe(self):
        # Same type counted twice shouldn't double-score
        s1 = composite_score(["linkedin_stealth"], self.fresh)
        s2 = composite_score(["linkedin_stealth", "linkedin_stealth"], self.fresh)
        assert s1 == s2

    def test_capped_at_100(self):
        # All signal types + fresh → should cap, not overflow
        s = composite_score(list(SIGNAL_TYPES), self.fresh)
        assert s <= 100.0

    def test_decay_with_old_signal(self):
        from datetime import timedelta

        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        fresh_score = composite_score(["linkedin_stealth"], self.fresh)
        old_score = composite_score(["linkedin_stealth"], old)
        assert old_score < fresh_score
        assert old_score == pytest.approx(fresh_score * 0.25, abs=0.1)


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


class TestEnsureJson:
    def test_passthrough_string(self):
        assert ensure_json("foo") == "foo"

    def test_list_serialized(self):
        assert ensure_json(["a", "b"]) == '["a", "b"]'

    def test_none_stays_none(self):
        assert ensure_json(None) is None

    def test_dict_serialized(self):
        import json as j

        out = ensure_json({"k": 1})
        assert j.loads(out) == {"k": 1}


def test_iso_now_round_trips():
    s = iso_now()
    # ISO-parseable
    dt = datetime.fromisoformat(s)
    assert dt.tzinfo is not None
