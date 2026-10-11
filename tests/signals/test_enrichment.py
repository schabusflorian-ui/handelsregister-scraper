"""
Regression tests for the enrichment pass — academic detection, website
join, and the new filter paths in the joiner's _compute_founder_confidence.
"""

import pytest

from persistence.database import Database
from signals.common import iso_now
from signals.joiner import _ACADEMIC_RX, _website_lookup, run_joiner

# ---------------------------------------------------------------------------
# Academic affiliation detection
# ---------------------------------------------------------------------------


class TestAcademicDetection:
    @pytest.mark.parametrize(
        "bio,expected_match_contains",
        [
            ("PhD at TUM, now building a stealth AI startup", "tum"),
            ("Researcher at Fraunhofer-Institut für Produktionstechnik", "fraunhofer"),
            ("ETH Zurich alumnus, Co-Founder @ Hiverge", "eth"),
            ("Ex-Max Planck Institute for Intelligent Systems", "max planck"),
            ("EPFL spin-off founder", "epfl"),
            ("Co-Founder — ex-RWTH Aachen deeptech lab", "rwth"),
            ("UnternehmerTUM resident founder", "unternehmertum"),
            ("Humboldt University, now stealth", "humboldt"),
        ],
    )
    def test_catches_dach_academic(self, bio, expected_match_contains):
        m = _ACADEMIC_RX.search(bio)
        assert m, f"should match: {bio!r}"
        assert expected_match_contains in m.group(0).lower()

    @pytest.mark.parametrize(
        "bio",
        [
            "Software engineer at BigCo",
            "Marketing lead at a startup",
            "Freelance designer",
        ],
    )
    def test_does_not_false_positive(self, bio):
        assert _ACADEMIC_RX.search(bio) is None

    def test_lookup_integrates_with_joiner(self, tmp_path):
        db = Database(str(tmp_path / "acad_test.db"))
        try:
            cur = db.conn.cursor()
            cur.execute(
                """INSERT INTO signals (signal_type, observed_at, source_url,
                                         entity_name, person_name, person_linkedin,
                                         raw_payload)
                   VALUES ('linkedin_stealth', ?, 'test://1', 'Academic One', 'Jane A',
                           'https://linkedin.com/in/jane-a',
                           ?)""",
                (iso_now(), '{"headline": "PhD @ ETH Zurich, Co-Founder @ Stealth"}'),
            )
            db.conn.commit()
            run_joiner(db.conn)
            row = cur.execute(
                "SELECT academic_founder, academic_institution, founder_confidence FROM stealth_entities LIMIT 1"
            ).fetchone()
            assert row["academic_founder"] == 1
            assert "eth" in row["academic_institution"].lower()
            # Confidence boosted (+10 for academic)
            assert row["founder_confidence"] >= 55
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Website enrichment join (graceful when table missing)
# ---------------------------------------------------------------------------


class TestWebsiteLookup:
    def test_degrades_gracefully_without_table(self, tmp_path):
        """A fresh Database doesn't create website_enrichment (that's
        website_enrichment_job's job). The joiner must not crash."""
        db = Database(str(tmp_path / "no_web.db"))
        try:
            cur = db.conn.cursor()
            # Table absent — _website_lookup returns (None, None), not crash
            title, desc = _website_lookup(cur, "example.ai")
            assert title is None and desc is None
        finally:
            db.close()

    def test_returns_data_when_table_exists_and_matches(self, tmp_path):
        db = Database(str(tmp_path / "web.db"))
        try:
            cur = db.conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS website_enrichment (
                       normalized_website TEXT PRIMARY KEY,
                       title TEXT, meta_description TEXT, error TEXT,
                       fetched_at TEXT)"""
            )
            cur.execute(
                "INSERT INTO website_enrichment "
                "(normalized_website, title, meta_description, fetched_at) "
                "VALUES ('test.ai', 'TestCo — Stealth AI', 'We build cool things', '2026-10-10')"
            )
            db.conn.commit()
            title, desc = _website_lookup(cur, "test.ai")
            assert title == "TestCo — Stealth AI"
            assert "cool" in desc
            # Non-match returns Nones
            assert _website_lookup(cur, "unknown.com") == (None, None)
            # Error rows are excluded
            cur.execute(
                "INSERT INTO website_enrichment "
                "(normalized_website, title, error, fetched_at) "
                "VALUES ('bad.ai', 'junk', 'timeout', '2026-10-10')"
            )
            db.conn.commit()
            assert _website_lookup(cur, "bad.ai") == (None, None)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Filter correctness in /signals/people SQL
# ---------------------------------------------------------------------------


class TestFilterSQL:
    """Smoke-test the filter clauses by building synthetic entities and
    running the exact SQL predicates the router uses."""

    @pytest.fixture
    def seeded(self, tmp_path):
        db = Database(str(tmp_path / "filter.db"))
        cur = db.conn.cursor()
        # Three persons with different attributes
        for i, (name, geo, fc, acad, tier, domain, days_ago) in enumerate(
            [
                ("DACH Academic Fresh", "DE", 70, 1, 1, "fresh.ai", 5),
                ("DACH Non-Academic Old", "DE", 70, 0, None, None, 200),
                ("US Academic Fresh", "US", 70, 1, 1, "us.ai", 5),
            ],
            start=1,
        ):
            cur.execute(
                """INSERT INTO stealth_entities (
                     primary_name, geo_region, founder_confidence, academic_founder,
                     watchlist_tier, primary_domain, is_person, signal_count,
                     last_signal_at, signal_types, composite_score, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, date('now', ?), '[\"linkedin_stealth\"]', 70, '2026-01-01', '2026-01-01')""",
                (name, geo, fc, acad, tier, domain, f"-{days_ago} days"),
            )
        db.conn.commit()
        yield db
        db.close()

    def test_dach_only_filter(self, seeded):
        n = seeded.conn.execute(
            "SELECT COUNT(*) FROM stealth_entities WHERE is_person=1 "
            "AND geo_region IN ('DE','AT','CH','DACH','dach','germany','austria','switzerland')"
        ).fetchone()[0]
        assert n == 2  # the two DACH rows

    def test_academic_only_filter(self, seeded):
        n = seeded.conn.execute(
            "SELECT COUNT(*) FROM stealth_entities WHERE is_person=1 AND academic_founder=1"
        ).fetchone()[0]
        assert n == 2

    def test_has_product_filter(self, seeded):
        n = seeded.conn.execute(
            "SELECT COUNT(*) FROM stealth_entities WHERE is_person=1 "
            "AND (primary_domain IS NOT NULL OR primary_github IS NOT NULL)"
        ).fetchone()[0]
        assert n == 2  # DACH Academic Fresh + US Academic Fresh

    def test_max_age_days_filter(self, seeded):
        n = seeded.conn.execute(
            "SELECT COUNT(*) FROM stealth_entities WHERE is_person=1 AND last_signal_at >= date('now', '-30 days')"
        ).fetchone()[0]
        assert n == 2  # the two "fresh" rows

    def test_combined_dach_fresh_academic(self, seeded):
        n = seeded.conn.execute(
            """SELECT COUNT(*) FROM stealth_entities
                WHERE is_person=1
                  AND geo_region IN ('DE','AT','CH')
                  AND last_signal_at >= date('now','-30 days')
                  AND academic_founder=1"""
        ).fetchone()[0]
        assert n == 1  # just the DACH Academic Fresh row
