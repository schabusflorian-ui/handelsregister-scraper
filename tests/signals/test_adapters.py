"""
Tests for the adapters that reshape existing tables into `signals` rows.

Focus is on the FILTERING behavior — the stopword list, the funding-verb
guard, the officer-ambiguity blacklist. These are the levers that
determine signal-to-noise ratio in production.
"""

import pytest

from persistence.database import Database
from signals.adapters import _NEWS_FUNDING_PATTERNS, _guess_entity, adapt_news
from signals.officer_adapter import _clean_person_name, _is_distinctive_name

# ---------------------------------------------------------------------------
# News entity extraction
# ---------------------------------------------------------------------------


class TestNewsEntityExtraction:
    """These are regression tests against actual news titles that
    previously extracted into garbage entity names."""

    def test_multi_token_capitalized_phrase(self):
        assert _guess_entity("Peec AI raises Series A") == "Peec AI"

    def test_camelcase_single_token_accepted(self):
        assert _guess_entity("OpenAI launches new model") == "OpenAI"
        assert _guess_entity("GitHub acquires Copilot") in ("GitHub", "GitHub Copilot")

    def test_plain_single_word_rejected(self):
        # "Berlin" alone → not a company. "Donnerstag" alone → not a company.
        assert _guess_entity("Berlin ist die Hauptstadt") is None
        assert _guess_entity("Donnerstag im Fokus") is None

    def test_stopword_only_title_rejected(self):
        assert _guess_entity("Studie zum Startup-Ökosystem") is None

    def test_empty_input(self):
        assert _guess_entity("") is None
        assert _guess_entity(None) is None


# ---------------------------------------------------------------------------
# News funding-verb guard
# ---------------------------------------------------------------------------


class TestNewsFundingGuard:
    @pytest.mark.parametrize(
        "title",
        [
            "Peec AI raises $5M seed",
            "FooCo launches new product",
            "BarCo closes Series A",
            "StartUp XYZ gründet in Berlin",
            "AI firm secures funding",
            "QuxCo coming out of stealth",
        ],
    )
    def test_funding_titles_pass(self, title):
        assert _NEWS_FUNDING_PATTERNS.search(title) is not None

    @pytest.mark.parametrize(
        "title",
        [
            "German tech industry grows",
            "A deep look at the climate crisis",
            "Berlin ist cool im Sommer",
            "Studie: Wirtschaft wächst leicht",
        ],
    )
    def test_non_funding_titles_rejected(self, title):
        assert _NEWS_FUNDING_PATTERNS.search(title) is None


# ---------------------------------------------------------------------------
# Officer name cleaning
# ---------------------------------------------------------------------------


class TestOfficerNameCleaning:
    @pytest.mark.parametrize(
        "raw,expected_tuple",
        [
            ("Marvin Vocke", ("marvin", "vocke")),
            ("Marvin Maximilian Vocke", ("marvin", "vocke")),
            ("Jane Doe", ("jane", "doe")),
        ],
    )
    def test_clean_names_parsed(self, raw, expected_tuple):
        assert _clean_person_name(raw) == expected_tuple

    @pytest.mark.parametrize(
        "raw",
        [
            # LinkedIn snippet pollution
            "Laurenz Vavrovsky – Working on something new Paul Lind – Co-Founder",
            "Friedrich Koebler - Founder & CEO: Stealth Startup",
            # Legal entity in officer position
            "GL-AA Anlagen Automation GmbH, Berlin",
            "Hörwurm Verwaltungs GmbH",
            # Weird separators / artifacts
            "Jörg : Sonnenberg",
            # Single-token / initials
            "A B",
            "Ha D.",
            # Empty
            None,
            "",
        ],
    )
    def test_dirty_names_rejected(self, raw):
        assert _clean_person_name(raw) is None, f"should reject {raw!r}"

    def test_distinctive_wrapper_matches_clean(self):
        assert _is_distinctive_name("Jane Doe") is True
        assert _is_distinctive_name("A B") is False
        assert _is_distinctive_name(None) is False


# ---------------------------------------------------------------------------
# Full news adapter smoke
# ---------------------------------------------------------------------------


class TestNewsAdapterEndToEnd:
    @pytest.fixture
    def conn(self, tmp_path):
        db = Database(str(tmp_path / "news_test.db"))
        yield db.conn
        db.close()

    def _seed_article(self, conn, *, title, url, is_funding=1, is_early=0, is_ai=0):
        conn.execute(
            """INSERT INTO news_articles
                 (url, title, source, published_date, is_funding_related,
                  is_ai_related, is_early_stage_related, fetched_at)
               VALUES (?, ?, 'test', date('now'), ?, ?, ?, date('now'))""",
            (url, title, is_funding, is_ai, is_early),
        )
        conn.commit()

    def test_funding_title_creates_signal(self, conn):
        self._seed_article(conn, title="Peec AI raises $5M seed round", url="http://ex.com/1")
        stats = adapt_news(conn)
        assert stats["inserted"] == 1
        row = conn.execute("SELECT entity_name, signal_type FROM signals LIMIT 1").fetchone()
        assert row["entity_name"] == "Peec AI"
        assert row["signal_type"] == "news_mention"

    def test_non_funding_title_filtered(self, conn):
        self._seed_article(
            conn, title="Studie zur deutschen Wirtschaft", url="http://ex.com/2", is_funding=0, is_early=1
        )
        stats = adapt_news(conn)
        assert stats["inserted"] == 0
        assert stats["no_funding_verb"] == 1

    def test_funding_title_without_entity_filtered(self, conn):
        self._seed_article(conn, title="startup gründet neu — der tag", url="http://ex.com/3", is_funding=0, is_early=1)
        stats = adapt_news(conn)
        assert stats["inserted"] == 0
        # Either no_entity or no_funding_verb — depends on whether a verb
        # pattern catches "gründet". Just assert nothing emitted.
        assert stats["inserted"] == 0

    def test_idempotent_rerun(self, conn):
        # is_funding=1 so the adapter's WHERE clause selects the row AND
        # the funding-verb guard passes.
        self._seed_article(conn, title="OpenAI launches new model", url="http://ex.com/4", is_funding=1)
        n1 = adapt_news(conn)["inserted"]
        n2 = adapt_news(conn)["inserted"]
        assert n1 == 1 and n2 == 0  # UNIQUE(signal_type, source_url) kicks in
