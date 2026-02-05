"""
News Monitoring Job - Scan RSS feeds for startup funding news.

Runs periodically to:
1. Fetch articles from German startup media
2. Extract funding announcements
3. Match companies to our database
4. Create alerts for relevant news
"""

import logging
from datetime import datetime
from typing import Dict, Any

from sources.news_monitor import NewsMonitor
from processing.investor_matcher import InvestorMatcher
from processing.filters import AIRoboticsFilter

logger = logging.getLogger(__name__)


class NewsMonitoringJob:
    """
    Monitor RSS feeds for startup news.

    Detects funding announcements and new AI/robotics startups.
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
                if self.monitor.is_funding_related(article):
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
                            )
                            stats['new_alerts'] += 1

                        # Detect investors mentioned
                        for inv_name in mention.investors:
                            inv_matches = self.matcher.match(inv_name)
                            if inv_matches:
                                stats['investors_detected'] += 1

                # Track AI/robotics articles
                if self.monitor.is_ai_robotics_related(article):
                    stats['ai_articles'] += 1

                    # Store article for later reference
                    self._store_article(article)

                # Track early-stage signals (grants, spinoffs, accelerators)
                if self.monitor.is_early_stage_signal(article):
                    stats['early_stage_articles'] += 1
                    self._store_article(article)

        except Exception as e:
            logger.exception("News monitoring failed: %s", e)
            stats['errors'] += 1

        stats['duration_seconds'] = (datetime.utcnow() - started_at).total_seconds()

        logger.info(
            "News monitoring complete: %d articles, %d funding mentions, %d AI articles",
            stats['articles_fetched'],
            stats['funding_mentions'],
            stats['ai_articles'],
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

    def _record_news_alert(self, company_id: int, article, mention):
        """Record a news alert for a company."""
        conn = self.db.conn

        # Ensure alerts table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_alerts (
                id INTEGER PRIMARY KEY,
                company_id INTEGER,
                article_url TEXT,
                article_title TEXT,
                source TEXT,
                alert_type TEXT,
                amount REAL,
                currency TEXT,
                round_type TEXT,
                investors TEXT,
                created_at TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            )
        """)

        try:
            conn.execute("""
                INSERT INTO news_alerts
                (company_id, article_url, article_title, source, alert_type,
                 amount, currency, round_type, investors, created_at)
                VALUES (?, ?, ?, ?, 'funding', ?, ?, ?, ?, ?)
            """, (
                company_id,
                article.url,
                article.title,
                article.source,
                mention.amount,
                mention.currency,
                mention.round_type,
                ','.join(mention.investors),
                datetime.utcnow().isoformat(),
            ))
            conn.commit()
        except Exception as e:
            logger.error("Failed to record news alert: %s", e)

    def _store_article(self, article):
        """Store article for reference."""
        conn = self.db.conn

        # Ensure articles table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                id INTEGER PRIMARY KEY,
                url TEXT UNIQUE,
                title TEXT,
                source TEXT,
                published_date TEXT,
                content_hash TEXT,
                is_funding_related INTEGER,
                is_ai_related INTEGER,
                fetched_at TEXT
            )
        """)

        try:
            conn.execute("""
                INSERT OR IGNORE INTO news_articles
                (url, title, source, published_date, content_hash,
                 is_funding_related, is_ai_related, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                article.url,
                article.title,
                article.source,
                article.published_date,
                article.content_hash,
                1 if self.monitor.is_funding_related(article) else 0,
                1 if self.monitor.is_ai_robotics_related(article) else 0,
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
