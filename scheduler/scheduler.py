"""
Handelsregister Scheduler - Continuous monitoring system.

Orchestrates discovery and backfill jobs using APScheduler for
reliable, persistent job scheduling.

Features:
- Configurable job schedules
- Persistent job store (SQLite)
- Rate limit aware scheduling
- Graceful shutdown handling
"""

import signal
import sys
import logging
from datetime import datetime
from typing import Optional, Dict, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from persistence.database import Database
from scheduler.rate_limiter import PersistentRateLimiter
from scheduler.jobs.discovery_job import DiscoveryJob
from scheduler.jobs.backfill_job import BackfillJob
from scheduler.jobs.enrichment_job import EnrichmentJob
from scheduler.jobs.announcement_job import AnnouncementMonitoringJob
from scheduler.jobs.csv_export_job import CSVExportJob
from scheduler.jobs.investor_detection_job import InvestorDetectionJob
from scheduler.jobs.news_job import NewsMonitoringJob

logger = logging.getLogger(__name__)


class HandelsregisterScheduler:
    """
    Scheduler for continuous Handelsregister monitoring.

    Manages job scheduling with:
    - Discovery job: Runs every 2 hours to find new companies
    - Backfill job: Runs continuously with idle budget
    - Rate limit awareness: Jobs respect 60 req/hr limit
    """

    def __init__(
        self,
        db_path: str,
        discovery_interval_hours: int = 2,
        discovery_max_requests: int = 25,
        backfill_max_requests: int = 50,
    ):
        """
        Initialize scheduler.

        Args:
            db_path: Path to SQLite database
            discovery_interval_hours: Hours between discovery job runs
            discovery_max_requests: Max requests per discovery run
            backfill_max_requests: Max requests per backfill run
        """
        self.db_path = db_path
        self.discovery_interval_hours = discovery_interval_hours
        self.discovery_max_requests = discovery_max_requests
        self.backfill_max_requests = backfill_max_requests

        # Don't create DB connection here - create fresh connections in each job
        # to avoid SQLite thread safety issues with APScheduler's ThreadPoolExecutor
        self.rate_limiter = PersistentRateLimiter(db_path)

        # Configure APScheduler
        # Use MemoryJobStore to avoid pickle issues with sqlite3.Connection
        # Jobs will be re-registered on restart, which is fine for our use case
        jobstores = {
            'default': MemoryJobStore()
        }
        executors = {
            'default': ThreadPoolExecutor(max_workers=1)  # One job at a time
        }
        job_defaults = {
            'coalesce': True,  # Combine missed runs
            'max_instances': 1,  # Only one instance at a time
            'misfire_grace_time': 3600,  # 1 hour grace period
        }

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone='UTC',
        )

        self._running = False

    def _run_discovery_job(self):
        """Execute discovery job wrapper."""
        logger.info("Starting discovery job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            job = DiscoveryJob(
                db=db,
                rate_limiter=self.rate_limiter,
                max_requests=self.discovery_max_requests,
            )
            stats = job.run(resume=True)

            logger.info(
                "Discovery job completed: %d new companies from %d found",
                stats['companies_new'], stats['companies_found']
            )

            # Log to job history
            self._log_job_completion('discovery', stats, db)

        except Exception as e:
            logger.exception("Discovery job failed: %s", e)
        finally:
            db.close()

    def _run_backfill_job(self):
        """Execute backfill job wrapper."""
        logger.info("Starting backfill job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            job = BackfillJob(
                db=db,
                rate_limiter=self.rate_limiter,
                max_requests=self.backfill_max_requests,
            )
            stats = job.run()

            logger.info(
                "Backfill job completed: %d new companies, %.1f%% progress",
                stats['companies_new'], stats['progress_percent']
            )

            # Log to job history
            self._log_job_completion('backfill', stats, db)

        except Exception as e:
            logger.exception("Backfill job failed: %s", e)
        finally:
            db.close()

    def _run_enrichment_job(self):
        """Execute enrichment job wrapper."""
        logger.info("Starting enrichment job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            job = EnrichmentJob(
                db=db,
                batch_size=50,
            )
            stats = job.run()

            logger.info(
                "Enrichment job completed: %d processed, %d events detected",
                stats['companies_processed'], stats['events_detected']
            )

            # Log to job history
            self._log_job_completion('enrichment', stats, db)

        except Exception as e:
            logger.exception("Enrichment job failed: %s", e)
        finally:
            db.close()

    def _run_announcement_job(self):
        """Execute announcement monitoring job wrapper."""
        logger.info("Starting announcement monitoring job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            job = AnnouncementMonitoringJob(
                db=db,
                rate_limiter=self.rate_limiter,
                lookback_days=7,  # Check last 7 days
            )
            stats = job.run()

            logger.info(
                "Announcement job completed: %d fetched, %d new companies, %d capital events",
                stats['announcements_fetched'],
                stats['new_companies'],
                stats['capital_events']
            )

            # Log to job history
            self._log_job_completion('announcement', stats, db)

        except Exception as e:
            logger.exception("Announcement job failed: %s", e)
        finally:
            db.close()

    def _run_csv_export_job(self):
        """Execute CSV export job wrapper."""
        logger.info("Starting CSV export job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            # Export to /data/exports (Railway volume)
            export_dir = "/data/exports"
            job = CSVExportJob(db=db, export_dir=export_dir)
            stats = job.run()

            if stats.get('status') == 'success':
                logger.info(
                    "CSV export completed: %d companies exported to %s",
                    stats.get('total_exported', 0),
                    stats.get('export_dir', export_dir)
                )
            else:
                logger.warning("CSV export failed: %s", stats.get('error', 'unknown'))

        except Exception as e:
            logger.exception("CSV export job failed: %s", e)
        finally:
            db.close()

    def _run_investor_detection_job(self):
        """Execute investor detection job wrapper."""
        logger.info("Starting investor detection job")

        # Create fresh DB connection for this thread
        db = Database(self.db_path)
        try:
            job = InvestorDetectionJob(
                db=db,
                batch_size=100,
                min_confidence=0.8,
            )
            stats = job.run()

            logger.info(
                "Investor detection completed: %d investments found, %d new",
                stats['investments_found'],
                stats['investments_new']
            )

            # Log to job history
            self._log_job_completion('investor_detection', stats, db)

        except Exception as e:
            logger.exception("Investor detection job failed: %s", e)
        finally:
            db.close()

    def _run_news_monitoring_job(self):
        """Execute news monitoring job wrapper."""
        logger.info("Starting news monitoring job")

        db = Database(self.db_path)
        try:
            job = NewsMonitoringJob(db=db)
            stats = job.run()

            logger.info(
                "News monitoring completed: %d articles, %d funding, %d AI, %d early-stage",
                stats['articles_fetched'],
                stats['funding_mentions'],
                stats['ai_articles'],
                stats.get('early_stage_articles', 0),
            )

            self._log_job_completion('news_monitoring', stats, db)

        except Exception as e:
            logger.exception("News monitoring job failed: %s", e)
        finally:
            db.close()

    def _log_job_completion(self, job_type: str, stats: Dict[str, Any], db: Database):
        """Log job completion to database."""
        try:
            db.conn.execute("""
                INSERT INTO job_runs (job_type, started_at, completed_at, status,
                                     companies_found, companies_new, requests_used)
                VALUES (?, ?, ?, 'completed', ?, ?, ?)
            """, (
                job_type,
                datetime.utcnow().isoformat(),
                datetime.utcnow().isoformat(),
                stats.get('companies_found', 0),
                stats.get('companies_new', 0),
                stats.get('requests_used', 0),
            ))
            db.conn.commit()
        except Exception as e:
            logger.error("Failed to log job completion: %s", e)

    def setup_jobs(self):
        """Configure scheduled jobs."""
        # Discovery job: every N hours
        self.scheduler.add_job(
            self._run_discovery_job,
            trigger=IntervalTrigger(hours=self.discovery_interval_hours),
            id='discovery_job',
            name='Discovery Job',
            replace_existing=True,
        )

        # Backfill job: twice daily at 3 AM and 3 PM UTC (use more of the budget)
        self.scheduler.add_job(
            self._run_backfill_job,
            trigger=CronTrigger(hour='3,15', minute=0),
            id='backfill_job',
            name='Backfill Job',
            replace_existing=True,
        )

        # Enrichment job: daily at 4 AM UTC (after backfill)
        self.scheduler.add_job(
            self._run_enrichment_job,
            trigger=CronTrigger(hour=4, minute=0),
            id='enrichment_job',
            name='Enrichment Job',
            replace_existing=True,
        )

        # Announcement monitoring job: daily at 5 AM UTC (after enrichment)
        self.scheduler.add_job(
            self._run_announcement_job,
            trigger=CronTrigger(hour=5, minute=0),
            id='announcement_job',
            name='Announcement Monitoring Job',
            replace_existing=True,
        )

        # CSV export job: daily at 6 AM UTC (after all data jobs)
        self.scheduler.add_job(
            self._run_csv_export_job,
            trigger=CronTrigger(hour=6, minute=0),
            id='csv_export_job',
            name='CSV Export Job',
            replace_existing=True,
        )

        # Investor detection job: daily at 7 AM UTC (after CSV export)
        # Scans capital events, officers, and announcements for VC involvement
        self.scheduler.add_job(
            self._run_investor_detection_job,
            trigger=CronTrigger(hour=7, minute=0),
            id='investor_detection_job',
            name='Investor Detection Job',
            replace_existing=True,
        )

        # News monitoring job: daily at 8 AM UTC (no API calls, just RSS)
        self.scheduler.add_job(
            self._run_news_monitoring_job,
            trigger=CronTrigger(hour=8, minute=0),
            id='news_monitoring_job',
            name='News Monitoring Job',
            replace_existing=True,
        )

        logger.info(
            "Jobs configured: discovery every %d hours, backfill 3AM+3PM, enrichment 4AM, "
            "announcements 5AM, CSV export 6AM, investor detection 7AM, news monitoring 8AM",
            self.discovery_interval_hours
        )

    def start(self, run_discovery_now: bool = False, run_backfill_now: bool = False):
        """
        Start the scheduler.

        Args:
            run_discovery_now: Run discovery job immediately
            run_backfill_now: Run backfill job immediately
        """
        self.setup_jobs()
        self.scheduler.start()
        self._running = True

        logger.info("Scheduler started")

        # Optionally run jobs immediately
        if run_discovery_now:
            self.scheduler.add_job(
                self._run_discovery_job,
                id='discovery_immediate',
                replace_existing=True,
            )

        if run_backfill_now:
            self.scheduler.add_job(
                self._run_backfill_job,
                id='backfill_immediate',
                replace_existing=True,
            )

    def stop(self):
        """Stop the scheduler gracefully."""
        if self._running:
            logger.info("Stopping scheduler...")
            self.scheduler.shutdown(wait=True)
            self._running = False
            logger.info("Scheduler stopped")

    def get_status(self) -> Dict[str, Any]:
        """Get current scheduler status."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run': job.next_run_time.isoformat() if job.next_run_time else None,
            })

        rate_state = self.rate_limiter.get_state()

        return {
            'running': self._running,
            'jobs': jobs,
            'rate_limit': {
                'tokens_available': rate_state.tokens_available,
                'requests_this_hour': rate_state.requests_this_hour,
                'can_request': rate_state.can_request,
            },
        }


def run_scheduler(
    db_path: str,
    discovery_interval: int = 2,
    run_discovery_now: bool = False,
    run_backfill_now: bool = False,
):
    """
    Run the scheduler as a long-running process.

    Args:
        db_path: Path to SQLite database
        discovery_interval: Hours between discovery runs
        run_discovery_now: Run discovery immediately
        run_backfill_now: Run backfill immediately
    """
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    scheduler = HandelsregisterScheduler(
        db_path=db_path,
        discovery_interval_hours=discovery_interval,
    )

    # Handle graceful shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start scheduler
    scheduler.start(
        run_discovery_now=run_discovery_now,
        run_backfill_now=run_backfill_now,
    )

    # Keep process running
    try:
        while True:
            import time
            time.sleep(60)

            # Log status every hour
            status = scheduler.get_status()
            if status['rate_limit']['requests_this_hour'] > 0:
                logger.info(
                    "Status: %d requests this hour, %.1f tokens available",
                    status['rate_limit']['requests_this_hour'],
                    status['rate_limit']['tokens_available'],
                )

    except KeyboardInterrupt:
        scheduler.stop()
