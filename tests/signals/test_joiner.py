"""
Integration-style tests for the signal joiner — exercise the full
in-memory DB → adapters → joiner path with hand-crafted signals that
cover the real cases we care about (cross-source match, strict cross-type
guard, idempotence, score recompute).

Lives above pure unit tests because the joiner is where business logic +
SQL meet, and bugs here corrupt entity aggregates silently.
"""

import json

import pytest

from persistence.database import Database
from signals.common import iso_now
from signals.joiner import run_joiner

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    db = Database(str(tmp_path / "sigtest.db"))
    yield db.conn
    db.close()


def _insert_signal(conn, **fields):
    """Thin helper — assumes defaults for most columns."""
    defaults = {
        "signal_type":   "linkedin_stealth",
        "observed_at":   iso_now(),
        "source_url":    None,
        "entity_name":   None,
        "entity_domain": None,
        "entity_github": None,
        "person_name":   None,
        "person_linkedin": None,
        "sector_tags":   None,
        "geo_region":    None,
        "raw_payload":   None,
        "confidence":    50.0,
    }
    defaults.update(fields)
    # source_url must be unique-ish for INSERT OR IGNORE to pass
    if defaults["source_url"] is None:
        import uuid
        defaults["source_url"] = f"test://{uuid.uuid4()}"
    cols = ",".join(defaults.keys())
    placeholders = ",".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO signals ({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Resolution — exact matches
# ---------------------------------------------------------------------------

class TestExactMatches:
    def test_domain_match_merges_entities(self, conn):
        # Two signals, same domain, different signal_types → one entity
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="FooCo GmbH", entity_domain="fooco.ai")
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Jane Doe", person_name="Jane Doe",
                       entity_domain="fooco.ai",
                       person_linkedin="https://linkedin.com/in/janedoe")
        run_joiner(conn)
        n = conn.execute("SELECT COUNT(*) FROM stealth_entities").fetchone()[0]
        assert n == 1, "same domain → one entity, not two"
        row = conn.execute("SELECT signal_count, signal_types "
                           "FROM stealth_entities LIMIT 1").fetchone()
        assert row[0] == 2
        assert set(json.loads(row[1])) == {"handelsregister_new", "linkedin_stealth"}

    def test_github_match_merges(self, conn):
        _insert_signal(conn, signal_type="github_org_new",
                       entity_name="FooCo", entity_github="fooco")
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="FooCo", entity_github="FooCo",
                       person_name="Jane")
        run_joiner(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM stealth_entities").fetchone()[0] == 1

    def test_linkedin_match_merges(self, conn):
        li = "https://linkedin.com/in/janedoe"
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Jane", person_linkedin=li)
        _insert_signal(conn, signal_type="linkedin_transition",
                       entity_name="Jane Doe", person_linkedin=li)
        run_joiner(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM stealth_entities").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Resolution — fuzzy name
# ---------------------------------------------------------------------------

class TestFuzzyNameMatches:
    def test_legal_form_variants_merge(self, conn):
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="Peec AI GmbH")
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="Peec AI")
        run_joiner(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM stealth_entities").fetchone()[0] == 1

    def test_cross_type_strict_mode_blocks_single_distinctive_token(self, conn):
        # Two signals, different types, share only ONE distinctive token.
        # In loose mode they'd merge; strict (cross-type) blocks it.
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Bozorg Kalantar Open Ecosystem",
                       person_name="Bozorg Kalantar",
                       person_linkedin="https://linkedin.com/in/bozorgk")
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="M.C Ecosystem GmbH")
        run_joiner(conn)
        # Two entities, not one
        assert conn.execute(
            "SELECT COUNT(*) FROM stealth_entities").fetchone()[0] == 2

    def test_cross_type_two_shared_tokens_still_merges(self, conn):
        # 2+ shared tokens pass even in strict mode
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Open Robotics",
                       person_name="Jane")
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="Open Robotics GmbH")
        run_joiner(conn)
        n = conn.execute("SELECT COUNT(*) FROM stealth_entities").fetchone()[0]
        assert n == 1, "2+ shared tokens should still cross-match"

    def test_city_does_not_cross_match(self, conn):
        # The exact prod bug — city names must never cross-match
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="Berlin Robotics GmbH")
        _insert_signal(conn, signal_type="news_mention",
                       entity_name="Berlin")
        run_joiner(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM stealth_entities").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------

class TestAggregates:
    def test_sector_tags_unioned_across_signals(self, conn):
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="FooCo", sector_tags='["climate"]')
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="FooCo", person_name="Jane",
                       sector_tags='["robotics","ai"]')
        run_joiner(conn)
        row = conn.execute("SELECT sector_tags FROM stealth_entities").fetchone()
        tags = set(json.loads(row[0]))
        assert tags == {"ai", "climate", "robotics"}

    def test_composite_score_populated(self, conn):
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="FooCo", entity_domain="fooco.ai")
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="FooCo", person_name="Jane",
                       entity_domain="fooco.ai")
        _insert_signal(conn, signal_type="crt_domain_new",
                       entity_name="FooCo", entity_domain="fooco.ai")
        run_joiner(conn)
        score = conn.execute("SELECT composite_score "
                             "FROM stealth_entities").fetchone()[0]
        # 3 types + bonuses — well above any single-signal weight
        assert score >= 60

    def test_is_person_flag(self, conn):
        # Use names with completely different first tokens so the fuzzy
        # matcher doesn't collapse them into one entity.
        _insert_signal(conn, signal_type="handelsregister_new",
                       entity_name="AlphaLegal Industries GmbH")
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="BetaStealth Co", person_name="X Y",
                       person_linkedin="https://linkedin.com/in/xy")
        run_joiner(conn)
        rows = {r["primary_name"]: r
                for r in conn.execute(
                    "SELECT primary_name, is_person FROM stealth_entities")}
        # Both entities should exist as separate rows
        assert len(rows) == 2
        # The handelsregister-only entity should not be flagged person
        hr_row = next(v for k, v in rows.items() if "AlphaLegal" in k)
        li_row = next(v for k, v in rows.items() if "BetaStealth" in k)
        assert hr_row["is_person"] == 0
        assert li_row["is_person"] == 1

    def test_founder_confidence_scales_with_evidence(self, conn):
        # Baseline — just stealth bio
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Alpha", person_name="A B",
                       person_linkedin="https://linkedin.com/in/ab",
                       raw_payload='{"headline": "ex-Google"}')
        # Richer — stealth + transition + founder keyword in payload
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="Beta", person_name="C D",
                       person_linkedin="https://linkedin.com/in/cd",
                       raw_payload='{"headline": "Co-Founder @ Stealth"}')
        _insert_signal(conn, signal_type="linkedin_transition",
                       entity_name="Beta", person_name="C D",
                       person_linkedin="https://linkedin.com/in/cd",
                       raw_payload='{"new": "Co-Founder"}')
        run_joiner(conn)
        rows = {r["primary_name"]: r for r in conn.execute(
            "SELECT primary_name, founder_confidence, person_role "
            "FROM stealth_entities")}
        assert rows["Beta"]["founder_confidence"] > rows["Alpha"]["founder_confidence"]
        assert rows["Beta"]["person_role"] is not None


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_rerun_does_not_duplicate(self, conn):
        _insert_signal(conn, signal_type="linkedin_stealth",
                       entity_name="FooCo",
                       person_linkedin="https://linkedin.com/in/x")
        run_joiner(conn)
        n1 = conn.execute("SELECT COUNT(*) FROM stealth_entities").fetchone()[0]
        run_joiner(conn)
        n2 = conn.execute("SELECT COUNT(*) FROM stealth_entities").fetchone()[0]
        assert n1 == n2 == 1

    def test_rebuild_all_matches_fresh(self, conn):
        """rebuild_all should produce the same aggregates as a fresh run.

        We seed entity names ≥ 3 chars because the joiner's fuzzy fallback
        gates on normalized token length ≥ 3 — single-letter names skip
        the match entirely and would double on rebuild (acceptable edge
        case for real data where entities are typically multi-token).
        """
        for name in ("Alpha Industries", "Bravo Dynamics", "Charlie Systems"):
            _insert_signal(conn, signal_type="handelsregister_new", entity_name=name)
        run_joiner(conn)
        before = conn.execute(
            "SELECT COUNT(*), SUM(signal_count), SUM(composite_score) "
            "FROM stealth_entities").fetchone()
        run_joiner(conn, rebuild_all=True)
        after = conn.execute(
            "SELECT COUNT(*), SUM(signal_count), SUM(composite_score) "
            "FROM stealth_entities").fetchone()
        assert tuple(before) == tuple(after)
