"""
News Monitoring Job - Scan RSS feeds for startup funding news.

Runs periodically to:
1. Fetch articles from German startup media
2. Extract funding announcements
3. Match companies to our database
4. Create alerts for relevant news (funding + early-stage signals)
"""

import re
import logging
from datetime import datetime
from typing import Dict, Any, List

from sources.news_monitor import NewsMonitor, EARLY_STAGE_PATTERNS
from processing.investor_matcher import InvestorMatcher
from processing.filters import AIRoboticsFilter

logger = logging.getLogger(__name__)


class NewsMonitoringJob:
    """
    Monitor RSS feeds for startup news.

    Detects funding announcements, AI/robotics startups, and early-stage signals
    (grants, university spinoffs, accelerator entries).
    """

    def __init__(self, db, matcher: InvestorMatcher = None):
        """
        Initialize job.

        Args:
            db: Database instance
            matcher: Investor matcher for detecting investors in news
        """
        self.db = db
        self.matcher = matcher or InvestorMatcher()
        self.filter = AIRoboticsFilter()
        self.monitor = NewsMonitor()

    def run(self) -> Dict[str, Any]:
        """
        Run news monitoring job.

        Returns:
            Statistics about the monitoring run
        """
        started_at = datetime.utcnow()

        stats = {
            'articles_fetched': 0,
            'funding_mentions': 0,
            'ai_articles': 0,
            'early_stage_articles': 0,
            'companies_matched': 0,
            'investors_detected': 0,
            'new_alerts': 0,
            'errors': 0,
        }

        try:
            # Fetch all articles
            articles = self.monitor.fetch_all_articles()
            stats['articles_fetched'] = len(articles)

            # Process funding-related articles
            for article in articles:
                is_funding = self.monitor.is_funding_related(article)
                is_ai = self.monitor.is_ai_robotics_related(article)
                is_early_stage = self.monitor.is_early_stage_signal(article)

                if is_funding:
                    mention = self.monitor.extract_funding_info(article)
                    if mention:
                        stats['funding_mentions'] += 1

                        # Try to match company to database
                        matched = self._match_company(mention.company_name)
                        if matched:
                            stats['companies_matched'] += 1
                            self._record_news_alert(
                                company_id=matched,
                                article=article,
                                mention=mention,
                                alert_type='funding',
                            )
                            stats['new_alerts'] += 1

                        # Detect investors mentioned
                        for inv_name in mention.investors:
                            inv_matches = self.matcher.match(inv_name)
                            if inv_matches:
                                stats['investors_detected'] += 1

                # Track AI/robotics/climate articles
                if is_ai:
                    stats['ai_articles'] += 1

                # Track early-stage signals (grants, spinoffs, accelerators)
                if is_early_stage:
                    stats['early_stage_articles'] += 1

                    # Create alert for early-stage signal if company can be matched
                    signals = self._extract_early_stage_signals(article)
                    company_name = self._extract_company_from_early_stage(article)
                    matched = self._match_company(company_name) if company_name else None
                    if matched:
                        self._record_early_stage_alert(
                            company_id=matched,
                            article=article,
                            signals=signals,
                        )
                        stats['new_alerts'] += 1

                # Store article if it's relevant (any of the three categories)
                if is_ai or is_early_stage or is_funding:
                    self._store_article(
                        article,
                        is_funding=is_funding,
                        is_ai=is_ai,
                        is_early_stage=is_early_stage,
                    )

        except Exception as e:
            logger.exception("News monitoring failed: %s", e)
            stats['errors'] += 1

        stats['duration_seconds'] = (datetime.utcnow() - started_at).total_seconds()

        logger.info(
            "News monitoring complete: %d articles, %d funding mentions, %d AI articles, %d early-stage",
            stats['articles_fetched'],
            stats['funding_mentions'],
            stats['ai_articles'],
            stats['early_stage_articles'],
        )

        return stats

    def _match_company(self, company_name: str) -> int:
        """Try to match company name to database."""
        if not company_name:
            return None

        conn = self.db.conn

        # Try exact match first
        row = conn.execute(
            "SELECT id FROM companies WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
            (f"%{company_name}%",)
        ).fetchone()

        return row['id'] if row else None

    def _extract_early_stage_signals(self, article) -> List[str]:
        """Extract which early-stage patterns matched in the article."""
        text = f"{article.title} {article.description or ''}"
        matched = []
        for pattern in EARLY_STAGE_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                matched.append(m.group(0))
        return matched

    def _extract_company_from_early_stage(self, article) -> str:
        """Try to extract a company name from early-stage article title."""
        title = article.title or ''
        # Common patterns: "CompanyName erhält EXIST Gründerstipendium"
        # "CompanyName gewinnt Gründerpreis"
        # "CompanyName: Ausgründung von TU München"
        patterns = [
            r'^([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)\s+(?:erhält|bekommt|gewinnt|sichert)',
            r'^([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)\s*[:\-–]',
            r'(?:Startup|Start-up|Ausgründung)\s+([A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+(?:\s+[A-Z][A-Za-zÄÖÜäöüß0-9\.\-]+)?)',
        ]
        for p in patterns:
            m = re.search(p, title)
            if m:
                name = m.group(1).strip()
                # Filter out common false positives
                if name.lower() not in ('das', 'die', 'der', 'ein', 'neue', 'deutsche', 'berliner'):
                    return name
        return None

    def _record_news_alert(self, company_id: int, article, mention, alert_type: str = 'funding'):
        """Record a news alert for a company."""
        conn = self.db.conn

        try:
            conn.execute("""
                INSERT INTO news_alerts
                (company_id, article_url, article_title, source, alert_type,
                 amount, currency, round_type, investors, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                company_id,
                article.url,
                article.title,
                article.source,
                alert_type,
                mention.amount,
                mention.currency,
                mention.round_type,
                ','.join(mention.investors),
                datetime.utcnow().isoformat(),
            ))
            conn.commit()
        except Exception as e:
            logger.error("Failed to record news alert: %s", e)

    def _record_early_stage_alert(self, company_id: int, article, signals: List[str]):
        """Record an early-stage signal alert for a company."""
        conn = self.db.conn

        try:
            conn.execute("""
                INSERT INTO news_alerts
                (company_id, article_url, article_title, source, alert_type,
                 early_stage_signals, created_at)
                VALUES (?, ?, ?, ?, 'early_stage', ?, ?)
            """, (
                company_id,
                article.url,
                article.title,
                article.source,
                ','.join(signals),
                datetime.utcnow().isoformat(),
            ))
            conn.commit()
        except Exception as e:
            logger.error("Failed to record early-stage alert: %s", e)

    def _store_article(self, article, is_funding: bool = False, is_ai: bool = False, is_early_stage: bool = False):
        """Store article for reference with classification flags."""
        conn = self.db.conn

        try:
            conn.execute("""
                INSERT OR IGNORE INTO news_articles
                (url, title, source, published_date, content_hash,
                 is_funding_related, is_ai_related, is_early_stage_related, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article.url,
                article.title,
                article.source,
                article.published_date,
                article.content_hash,
                1 if is_funding else 0,
                1 if is_ai else 0,
                1 if is_early_stage else 0,
                datetime.utcnow().isoformat(),
            ))
            conn.commit()
        except Exception as e:
            logger.debug("Article already stored or error: %s", e)


def run_news_monitoring(db_path: str) -> Dict[str, Any]:
    """Convenience function to run news monitoring."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = NewsMonitoringJob(db=db)
        return job.run()
    finally:
        db.close()
