"""
News Monitoring Job - Scan RSS feeds for startup funding news.

Runs periodically to:
1. Fetch articles from German startup media
2. Extract funding announcements
3. Match companies to our database OR create new ones
4. Create alerts for relevant news (funding + early-stage signals)
5. Optionally enrich new companies via Handelsregister lookup
"""

import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from processing.filters import AIRoboticsFilter
from processing.investor_matcher import InvestorMatcher
from processing.startup_scorer import StartupScorer
from sources.news_monitor import EARLY_STAGE_PATTERNS, NewsMonitor

logger = logging.getLogger(__name__)


class NewsMonitoringJob:
    """
    Monitor RSS feeds for startup news.

    Detects funding announcements, AI/robotics startups, and early-stage signals
    (grants, university spinoffs, accelerator entries).

    When a company is mentioned but not in the database, creates a new record
    and optionally enriches it via Handelsregister search.
    """

    def __init__(
        self,
        db,
        matcher: InvestorMatcher = None,
        rate_limiter=None,
        max_hr_lookups: int = 5,
    ):
        """
        Initialize job.

        Args:
            db: Database instance
            matcher: Investor matcher for detecting investors in news
            rate_limiter: Optional rate limiter for Handelsregister lookups
            max_hr_lookups: Max Handelsregister searches per run (0=disabled)
        """
        self.db = db
        self.matcher = matcher or InvestorMatcher()
        self.filter = AIRoboticsFilter()
        self.scorer = StartupScorer()
        self.monitor = NewsMonitor()
        self.rate_limiter = rate_limiter
        self.max_hr_lookups = max_hr_lookups
        self._hr_lookups_done = 0

    def run(self) -> Dict[str, Any]:
        """
        Run news monitoring job.

        Returns:
            Statistics about the monitoring run
        """
        started_at = datetime.utcnow()

        stats = {
            "articles_fetched": 0,
            "funding_mentions": 0,
            "ai_articles": 0,
            "early_stage_articles": 0,
            "companies_matched": 0,
            "companies_created": 0,
            "companies_enriched_hr": 0,
            "investors_detected": 0,
            "new_alerts": 0,
            "errors": 0,
        }

        try:
            # Fetch all articles
            articles = self.monitor.fetch_all_articles()
            stats["articles_fetched"] = len(articles)

            for article in articles:
                is_funding = self.monitor.is_funding_related(article)
                is_ai = self.monitor.is_ai_robotics_related(article)
                is_early_stage = self.monitor.is_early_stage_signal(article)

                # Process funding articles
                if is_funding:
                    mention = self.monitor.extract_funding_info(article)
                    if mention:
                        stats["funding_mentions"] += 1

                        # Match or create company
                        company_id, was_new = self._match_or_create_company(
                            company_name=mention.company_name,
                            source_article=article,
                            round_type=mention.round_type,
                            amount=mention.amount,
                        )
                        if company_id:
                            if was_new:
                                stats["companies_created"] += 1
                            else:
                                stats["companies_matched"] += 1

                            self._record_news_alert(
                                company_id=company_id,
                                article=article,
                                mention=mention,
                                alert_type="funding",
                            )
                            stats["new_alerts"] += 1

                        # Detect investors mentioned
                        for inv_name in mention.investors:
                            inv_matches = self.matcher.match(inv_name)
                            if inv_matches:
                                stats["investors_detected"] += 1

                # Track AI/robotics/climate articles
                if is_ai:
                    stats["ai_articles"] += 1

                # Track early-stage signals (grants, spinoffs, accelerators)
                if is_early_stage:
                    stats["early_stage_articles"] += 1

                    # Extract company and create alert
                    signals = self._extract_early_stage_signals(article)
                    company_name = self._extract_company_from_early_stage(article)

                    if company_name:
                        company_id, was_new = self._match_or_create_company(
                            company_name=company_name,
                            source_article=article,
                            round_type="grant"
                            if any("förder" in s.lower() or "stipend" in s.lower() for s in signals)
                            else "pre_seed",
                        )
                        if company_id:
                            if was_new:
                                stats["companies_created"] += 1
                            else:
                                stats["companies_matched"] += 1

                            self._record_early_stage_alert(
                                company_id=company_id,
                                article=article,
                                signals=signals,
                            )
                            stats["new_alerts"] += 1

                # Store article if relevant
                if is_ai or is_early_stage or is_funding:
                    self._store_article(
                        article,
                        is_funding=is_funding,
                        is_ai=is_ai,
                        is_early_stage=is_early_stage,
                    )

        except Exception as e:
            logger.exception("News monitoring failed: %s", e)
            stats["errors"] += 1

        stats["companies_enriched_hr"] = self._hr_lookups_done
        stats["duration_seconds"] = (datetime.utcnow() - started_at).total_seconds()

        logger.info(
            "News monitoring complete: %d articles, %d funding, %d AI, %d early-stage, "
            "%d companies created, %d HR enriched",
            stats["articles_fetched"],
            stats["funding_mentions"],
            stats["ai_articles"],
            stats["early_stage_articles"],
            stats["companies_created"],
            stats["companies_enriched_hr"],
        )

        return stats

    # =========================================================================
    # Company matching and creation
    # =========================================================================

    def _match_or_create_company(
        self,
        company_name: str,
        source_article=None,
        round_type: Optional[str] = None,
        amount: Optional[float] = None,
    ) -> Tuple[Optional[int], bool]:
        """
        Match company to database, or create a new record if not found.

        Returns:
            (company_id, was_new) - company_id is None if name is invalid
        """
        if not company_name or len(company_name) < 3:
            return None, False

        # Try to match existing
        company_id = self._match_company(company_name)
        if company_id:
            return company_id, False

        # Create new company from news
        company_id = self._create_company_from_news(
            company_name=company_name,
            source_article=source_article,
            round_type=round_type,
            amount=amount,
        )
        return company_id, (company_id is not None)

    def _match_company(self, company_name: str) -> Optional[int]:
        """Try to match company name to database."""
        if not company_name:
            return None

        conn = self.db.conn

        # Try exact match first
        row = conn.execute(
            "SELECT id FROM companies WHERE name LIKE ? COLLATE NOCASE LIMIT 1", (f"%{company_name}%",)
        ).fetchone()

        return row["id"] if row else None

    def _create_company_from_news(
        self,
        company_name: str,
        source_article=None,
        round_type: Optional[str] = None,
        amount: Optional[float] = None,
    ) -> Optional[int]:
        """
        Create a new company record from a news article mention.

        Applies AI/startup filters and scoring. Optionally enriches
        from Handelsregister if rate limiter allows.

        Returns:
            company_id or None if filtered out
        """
        # Filter: check if name passes basic quality checks
        if not self._is_valid_company_name(company_name):
            return None

        # Run through AI/robotics filter
        filter_result = self.filter.filter_company(
            name=company_name,
            status="currently registered",
        )

        # Score startup likelihood
        startup_result = self.scorer.score_company(
            name=company_name,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            tech_categories=filter_result.tech_categories,
        )
        classification = self.scorer.classify(
            startup_result,
            ai_relevance_score=filter_result.relevance_score,
            climate_score=filter_result.climate_score,
            tech_categories=filter_result.tech_categories,
        )

        # Generate a stable company_number from the name
        name_hash = hashlib.md5(company_name.lower().encode()).hexdigest()[:8]
        company_number = f"news_{name_hash}"

        # Check if we already created this company from news before
        existing = self.db.get_company_by_number(company_number)
        if existing:
            return existing["id"]

        # Extract legal form from company name
        from processing.filters import extract_legal_form

        legal_form = extract_legal_form(company_name)

        # Insert company
        try:
            company_id = self.db.insert_company(
                company_number=company_number,
                name=company_name,
                source="news",
                legal_form=legal_form,
                ai_robotics_score=filter_result.relevance_score,
                climate_score=filter_result.climate_score,
                matched_keywords=filter_result.matched_keywords if filter_result.matched_keywords else None,
                tech_categories=filter_result.tech_categories if filter_result.tech_categories else None,
                startup_score=startup_result.total_score,
                startup_classification=classification,
                capital_amount=amount,
            )
        except Exception as e:
            logger.error("Failed to create company from news '%s': %s", company_name, e)
            return None

        logger.info(
            "New company from news: %s (AI score: %d, startup: %s, class: %s)",
            company_name,
            filter_result.relevance_score,
            startup_result.total_score,
            classification,
        )

        # Queue for enrichment
        self.db.add_to_enrichment_queue(company_id, priority=2, reason="new_from_news")

        # Optionally try Handelsregister lookup
        if self._hr_lookups_done < self.max_hr_lookups:
            enriched = self._enrich_from_handelsregister(company_id, company_name)
            if enriched:
                self._hr_lookups_done += 1

        return company_id

    def _is_valid_company_name(self, name: str) -> bool:
        """Check if extracted name is a plausible company name."""
        if not name or len(name) < 3:
            return False

        # Too short single words are usually not company names
        if len(name.split()) == 1 and len(name) < 4:
            return False

        # Reject placeholder names
        if name.lower() in ("unknown", "unbekannt"):
            return False

        # Filter out common false positives from article titles
        stopwords = {
            "das",
            "die",
            "der",
            "ein",
            "eine",
            "neue",
            "neues",
            "neuer",
            "deutsche",
            "berliner",
            "münchner",
            "hamburger",
            "kölner",
            "startup",
            "start-up",
            "startups",
            "unternehmen",
            "firma",
            "millionen",
            "milliarden",
            "euro",
            "dollar",
            "usd",
            "serie",
            "series",
            "runde",
            "round",
            "funding",
            "investor",
            "investoren",
            "gründer",
            "founder",
            "warum",
            "wie",
            "was",
            "wer",
            "welche",
            "diese",
            "update",
            "news",
            "breaking",
            "exklusiv",
            "analyse",
            # Countries and regions
            "deutschland",
            "germany",
            "europa",
            "europe",
            "kroatien",
            "frankreich",
            "österreich",
            "schweiz",
            "italien",
            "spanien",
            "polen",
            "china",
            "indien",
            "bayern",
            "sachsen",
            "hessen",
            "brandenburg",
            # Generic words
            "incubation",
            "investment",
            "finanzierung",
            "förderung",
            "prozent",
            "umsatz",
        }
        if name.lower() in stopwords:
            return False

        return True

    # =========================================================================
    # Handelsregister enrichment
    # =========================================================================

    def _enrich_from_handelsregister(self, company_id: int, company_name: str) -> bool:
        """
        Try to find and enrich company from Handelsregister.

        Searches for the company name via BundesAPI and updates the record
        with registry data (address, legal form, capital, etc.).

        Returns:
            True if enrichment succeeded
        """
        # Check rate limiter (needs 2 tokens: init + search)
        if self.rate_limiter and not self.rate_limiter.acquire(count=2, block=False):
            logger.debug("Rate limit: skipping HR lookup for %s", company_name)
            return False

        try:
            from sources.bundesapi import BundesAPISource

            source = BundesAPISource()

            # Search by company name keywords
            name_parts = company_name.split()
            # Use the most distinctive parts (skip very short words)
            keywords = [w for w in name_parts if len(w) >= 3]
            if not keywords:
                keywords = name_parts[:2]

            results = list(
                source.search(
                    keywords=keywords,
                    keyword_mode="all",
                    max_results=5,
                )
            )

            if not results:
                # Try with fewer keywords if we had multiple
                if len(keywords) > 1:
                    results = list(
                        source.search(
                            keywords=keywords[:1],
                            keyword_mode="all",
                            max_results=10,
                        )
                    )

            # Find best match
            best = self._find_best_hr_match(company_name, results)
            if not best:
                logger.debug("No HR match for '%s' (%d results)", company_name, len(results))
                return False

            # Update company with registry data
            update_fields = {
                "native_company_number": best.native_company_number,
                "registry_court": best.registry_court,
                "registry_type": best.registry_type,
                "current_status": best.status,
                "state": best.state,
                "city": best.city,
            }

            # Update the company_number to reflect registry data
            if best.native_company_number:
                reg_hash = hashlib.md5(best.native_company_number.encode()).hexdigest()[:8]
                update_fields["company_number"] = f"news_hr_{reg_hash}"

            self.db.update_company(company_id, **update_fields)

            # Remove from enrichment queue since we just enriched it
            self.db.update_company(company_id, enrichment_status="enriched")
            self.db.remove_from_enrichment_queue(company_id)

            logger.info("Enriched from HR: %s -> %s (%s, %s)", company_name, best.name, best.registry_type, best.state)
            return True

        except Exception as e:
            logger.error("HR enrichment failed for '%s': %s", company_name, e)
            return False

    def _find_best_hr_match(self, target_name: str, results) -> Optional[Any]:
        """Find the best Handelsregister match for a company name."""
        if not results:
            return None

        target_lower = target_name.lower()
        target_words = set(target_lower.split())

        best_match = None
        best_score = 0

        for result in results:
            result_lower = result.name.lower()
            result_words = set(result_lower.split())

            # Skip deleted companies
            if result.status and "deleted" in result.status.lower():
                continue

            # Scoring: word overlap
            common_words = target_words & result_words
            # Ignore very common words
            common_words -= {"gmbh", "ug", "ag", "se", "kg", "ohg", "co.", "&", "und"}
            if not common_words:
                continue

            score = len(common_words) / max(len(target_words), 1)

            # Bonus for exact substring match
            if target_lower in result_lower or result_lower in target_lower:
                score += 0.5

            # Bonus for currently registered
            if result.status and "currently" in result.status.lower():
                score += 0.1

            if score > best_score:
                best_score = score
                best_match = result

        # Require minimum match quality
        if best_score < 0.4:
            return None

        return best_match

    # =========================================================================
    # Signal extraction
    # =========================================================================

    def _extract_early_stage_signals(self, article) -> List[str]:
        """Extract which early-stage patterns matched in the article."""
        text = f"{article.title} {article.description or ''}"
        matched = []
        for pattern in EARLY_STAGE_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                matched.append(m.group(0))
        return matched

    def _extract_company_from_early_stage(self, article) -> Optional[str]:
        """Try to extract a company name from early-stage article title."""
        title = article.title or ""
        # Common patterns: "CompanyName erhält EXIST Gründerstipendium"
        # "CompanyName gewinnt Gründerpreis"
        # "CompanyName: Ausgründung von TU München"
        patterns = [
            r"^([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)\s+(?:erhält|bekommt|gewinnt|sichert)",
            r"^([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)\s*[:\-–]",
            r"(?:Startup|Start-up|Ausgründung)\s+([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)",
        ]
        for p in patterns:
            m = re.search(p, title)
            if m:
                name = m.group(1).strip()
                if self._is_valid_company_name(name):
                    return name
        return None

    # =========================================================================
    # Alert and article storage
    # =========================================================================

    def _record_news_alert(self, company_id: int, article, mention, alert_type: str = "funding"):
        """Record a news alert for a company."""
        conn = self.db.conn

        try:
            conn.execute(
                """
                INSERT INTO news_alerts
                (company_id, article_url, article_title, source, alert_type,
                 amount, currency, round_type, investors, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    company_id,
                    article.url,
                    article.title,
                    article.source,
                    alert_type,
                    mention.amount,
                    mention.currency,
                    mention.round_type,
                    ",".join(mention.investors),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to record news alert: %s", e)

    def _record_early_stage_alert(self, company_id: int, article, signals: List[str]):
        """Record an early-stage signal alert for a company."""
        conn = self.db.conn

        try:
            conn.execute(
                """
                INSERT INTO news_alerts
                (company_id, article_url, article_title, source, alert_type,
                 early_stage_signals, created_at)
                VALUES (?, ?, ?, ?, 'early_stage', ?, ?)
            """,
                (
                    company_id,
                    article.url,
                    article.title,
                    article.source,
                    ",".join(signals),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to record early-stage alert: %s", e)

    def _store_article(self, article, is_funding: bool = False, is_ai: bool = False, is_early_stage: bool = False):
        """Store article for reference with classification flags."""
        conn = self.db.conn

        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO news_articles
                (url, title, source, published_date, content_hash,
                 is_funding_related, is_ai_related, is_early_stage_related, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    article.url,
                    article.title,
                    article.source,
                    article.published_date,
                    article.content_hash,
                    1 if is_funding else 0,
                    1 if is_ai else 0,
                    1 if is_early_stage else 0,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception as e:
            logger.debug("Article already stored or error: %s", e)


def run_news_monitoring(db_path: str, max_hr_lookups: int = 5) -> Dict[str, Any]:
    """Convenience function to run news monitoring."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = NewsMonitoringJob(db=db, max_hr_lookups=max_hr_lookups)
        return job.run()
    finally:
        db.close()
