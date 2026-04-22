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

import logging
import signal
import sys
from datetime import datetime
from typing import Any, Dict

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from persistence.database import Database
from scheduler.jobs.announcement_job import AnnouncementMonitoringJob
from scheduler.jobs.backfill_job import BackfillJob
from scheduler.jobs.csv_export_job import CSVExportJob
from scheduler.jobs.discovery_job import DiscoveryJob
from scheduler.jobs.enrichment_job import EnrichmentJob
from scheduler.jobs.investor_detection_job import InvestorDetectionJob
from scheduler.jobs.news_job import NewsMonitoringJob
from scheduler.jobs.officer_linkedin_job import OfficerLinkedInEnrichmentJob
from scheduler.jobs.registration_scan_job import RegistrationScanJob
from scheduler.jobs.website_job import WebsiteFinderJob
from scheduler.jobs.website_scrape_job import WebsiteScrapeJob
from scheduler.jobs.founder_matcher import find_emerged_founders, link_founder_to_company
from scheduler.rate_limiter import PersistentRateLimiter

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
        registration_scan_max_requests: int = 40,
    ):
        """
        Initialize scheduler.

        Args:
            db_path: Path to SQLite database
            discovery_interval_hours: Hours between discovery job runs
            discovery_max_requests: Max requests per discovery run
            backfill_max_requests: Max requests per backfill run
            registration_scan_max_requests: Max requests per registration scan run
        """
        self.db_path = db_path
        self.discovery_interval_hours = discovery_interval_hours
        self.discovery_max_requests = discovery_max_requests
        self.backfill_max_requests = backfill_max_requests
        self.registration_scan_max_requests = registration_scan_max_requests

        # Don't create DB connection here - create fresh connections in each job
        # to avoid SQLite thread safety issues with APScheduler's ThreadPoolExecutor
        self.rate_limiter = PersistentRateLimiter(db_path)

        # Configure APScheduler
        # Use MemoryJobStore to avoid pickle issues with sqlite3.Connection
        # Jobs will be re-registered on restart, which is fine for our use case
        jobstores = {"default": MemoryJobStore()}
        executors = {
            "default": ThreadPoolExecutor(max_workers=1)  # One job at a time
        }
        job_defaults = {
            "coalesce": True,  # Combine missed runs
            "max_instances": 1,  # Only one instance at a time
            "misfire_grace_time": 3600,  # 1 hour grace period
        }

        self.scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone="UTC",
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
                stats["companies_new"],
                stats["companies_found"],
            )

            # Log to job history
            self._log_job_completion("discovery", stats, db)

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
                stats["companies_new"],
                stats["progress_percent"],
            )

            # Log to job history
            self._log_job_completion("backfill", stats, db)

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
                stats["companies_processed"],
                stats["events_detected"],
            )

            # Log to job history
            self._log_job_completion("enrichment", stats, db)

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
                # Fast domain-guess lookup for high-signal new companies so the
                # UI shows a site within minutes; full scrape stays on the
                # daily WebsiteScrapeJob.
                inline_website_lookup=True,
                inline_website_min_score=3,
            )
            stats = job.run()

            logger.info(
                "Announcement job completed: %d fetched, %d new companies, %d capital events",
                stats["announcements_fetched"],
                stats["new_companies"],
                stats["capital_events"],
            )

            # Log to job history
            self._log_job_completion("announcement", stats, db)

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

            if stats.get("status") == "success":
                logger.info(
                    "CSV export completed: %d companies exported to %s",
                    stats.get("total_exported", 0),
                    stats.get("export_dir", export_dir),
                )
            else:
                logger.warning("CSV export failed: %s", stats.get("error", "unknown"))

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
                stats["investments_found"],
                stats["investments_new"],
            )

            # Log to job history
            self._log_job_completion("investor_detection", stats, db)

        except Exception as e:
            logger.exception("Investor detection job failed: %s", e)
        finally:
            db.close()

    def _run_news_monitoring_job(self):
        """Execute news monitoring job wrapper."""
        logger.info("Starting news monitoring job")

        db = Database(self.db_path)
        try:
            job = NewsMonitoringJob(
                db=db,
                rate_limiter=self.rate_limiter,
                max_hr_lookups=5,
            )
            stats = job.run()

            logger.info(
                "News monitoring completed: %d articles, %d funding, %d AI, %d early-stage, "
                "%d companies created, %d HR-enriched",
                stats["articles_fetched"],
                stats["funding_mentions"],
                stats["ai_articles"],
                stats.get("early_stage_articles", 0),
                stats.get("companies_created", 0),
                stats.get("companies_enriched_hr", 0),
            )

            self._log_job_completion("news_monitoring", stats, db)

        except Exception as e:
            logger.exception("News monitoring job failed: %s", e)
        finally:
            db.close()

    def _run_founder_emergence_job(self):
        """Check if any stealth founders have emerged (registered a company)."""
        logger.info("Starting founder emergence detection job")

        db = Database(self.db_path)
        try:
            emerged = find_emerged_founders(db, min_similarity=0.85)

            auto_linked = 0
            for match in emerged:
                # Auto-link high-confidence matches (exact or near-exact name match)
                best_officer = match["officer_matches"][0] if match["officer_matches"] else None
                if best_officer and best_officer["similarity"] >= 0.95:
                    link_founder_to_company(db, match["founder_id"], best_officer["company_id"])
                    auto_linked += 1
                    logger.info(
                        "Founder emerged: %s -> %s (similarity=%.0f%%)",
                        match["founder_name"],
                        best_officer["company_name"],
                        best_officer["similarity"] * 100,
                    )

            stats = {
                "founders_checked": len(emerged),
                "auto_linked": auto_linked,
                "pending_review": len(emerged) - auto_linked,
            }

            logger.info(
                "Founder emergence: %d potential matches, %d auto-linked, %d for review",
                len(emerged), auto_linked, len(emerged) - auto_linked,
            )

            self._log_job_completion("founder_emergence", stats, db)

        except Exception as e:
            logger.exception("Founder emergence job failed: %s", e)
        finally:
            db.close()

    def _run_website_finder_job(self):
        """Execute website finder job wrapper."""
        logger.info("Starting website finder job")

        db = Database(self.db_path)
        try:
            job = WebsiteFinderJob(db=db, batch_size=50)
            stats = job.run()

            logger.info(
                "Website finder completed: %d checked, %d found (%d guess, %d search)",
                stats["companies_checked"],
                stats["websites_found"],
                stats.get("websites_by_guess", 0),
                stats.get("websites_by_search", 0),
            )

            self._log_job_completion("website_finder", stats, db)

        except Exception as e:
            logger.exception("Website finder job failed: %s", e)
        finally:
            db.close()

    def _run_website_scrape_job(self):
        """Execute website scrape job wrapper."""
        logger.info("Starting website scrape job")

        db = Database(self.db_path)
        try:
            job = WebsiteScrapeJob(db=db, batch_size=30)
            stats = job.run()

            logger.info(
                "Website scrape completed: %d checked, %d enriched, %d descriptions, %d investors",
                stats["companies_checked"],
                stats["companies_enriched"],
                stats["descriptions_added"],
                stats["investors_detected"],
            )

            self._log_job_completion("website_scrape", stats, db)

        except Exception as e:
            logger.exception("Website scrape job failed: %s", e)
        finally:
            db.close()

    def _run_officer_linkedin_job(self):
        """Execute officer LinkedIn enrichment job wrapper."""
        logger.info("Starting officer LinkedIn enrichment job")

        db = Database(self.db_path)
        try:
            job = OfficerLinkedInEnrichmentJob(
                db=db,
                search_delay=180,  # 3 min between searches
                min_confidence=0.40,
            )
            stats = job.run_batch(batch_size=5)

            logger.info(
                "Officer LinkedIn enrichment completed: %d processed, %d enriched, %d no match",
                stats["officers_processed"],
                stats["officers_enriched"],
                stats["officers_no_match"],
            )

            self._log_job_completion("officer_linkedin", stats, db)

        except Exception as e:
            logger.exception("Officer LinkedIn enrichment job failed: %s", e)
        finally:
            db.close()

    def _run_registration_scan_job(self):
        """Execute registration scan job wrapper."""
        logger.info("Starting registration scan job")

        db = Database(self.db_path)
        try:
            job = RegistrationScanJob(
                db=db,
                rate_limiter=self.rate_limiter,
                max_requests=self.registration_scan_max_requests,
            )
            stats = job.run()

            logger.info(
                "Registration scan completed: %d found, %d new, %d requests",
                stats["companies_found"],
                stats["companies_new"],
                stats["requests_used"],
            )

            self._log_job_completion("registration_scan", stats, db)

        except Exception as e:
            logger.exception("Registration scan job failed: %s", e)
        finally:
            db.close()

    # Map each job type's stat keys → generic logging columns.
    # Each tuple: (key for companies_found, key for companies_new, key for requests_used)
    _STAT_KEY_MAP: Dict[str, tuple] = {
        "discovery": ("companies_found", "companies_new", "requests_used"),
        "backfill": ("companies_found", "companies_new", "requests_used"),
        "enrichment": ("companies_processed", "events_detected", "requests_used"),
        "announcement": ("announcements_fetched", "new_companies", "requests_used"),
        "investor_detection": ("investments_found", "investments_new", "requests_used"),
        "news_monitoring": ("articles_fetched", "companies_created", "requests_used"),
        "website_finder": ("companies_checked", "websites_found", "requests_used"),
        "website_scrape": ("companies_checked", "companies_enriched", "requests_used"),
        "officer_linkedin": ("officers_processed", "officers_enriched", "requests_used"),
        "registration_scan": ("companies_found", "companies_new", "requests_used"),
    }

    def _log_job_completion(self, job_type: str, stats: Dict[str, Any], db: Database):
        """Log job completion to database.

        Uses per-job-type key mapping so each job's specific stat names
        are correctly stored into the generic companies_found / companies_new /
        requests_used columns.
        """
        try:
            found_key, new_key, req_key = self._STAT_KEY_MAP.get(
                job_type, ("companies_found", "companies_new", "requests_used")
            )
            db.conn.execute(
                """
                INSERT INTO job_runs (job_type, started_at, completed_at, status,
                                     companies_found, companies_new, requests_used)
                VALUES (?, ?, ?, 'completed', ?, ?, ?)
            """,
                (
                    job_type,
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                    stats.get(found_key, 0),
                    stats.get(new_key, 0),
                    stats.get(req_key, 0),
                ),
            )
            db.conn.commit()
        except Exception as e:
            logger.error("Failed to log job completion: %s", e)

    def setup_jobs(self):
        """Configure scheduled jobs."""
        # Discovery job: every N hours
        self.scheduler.add_job(
            self._run_discovery_job,
            trigger=IntervalTrigger(hours=self.discovery_interval_hours),
            id="discovery_job",
            name="Discovery Job",
            replace_existing=True,
        )

        # Backfill job: twice daily at 3 AM and 3 PM UTC (use more of the budget)
        self.scheduler.add_job(
            self._run_backfill_job,
            trigger=CronTrigger(hour="3,15", minute=0),
            id="backfill_job",
            name="Backfill Job",
            replace_existing=True,
        )

        # Enrichment job: daily at 4 AM UTC (after backfill)
        self.scheduler.add_job(
            self._run_enrichment_job,
            trigger=CronTrigger(hour=4, minute=0),
            id="enrichment_job",
            name="Enrichment Job",
            replace_existing=True,
        )

        # Announcement monitoring job: daily at 5 AM UTC (after enrichment)
        self.scheduler.add_job(
            self._run_announcement_job,
            trigger=CronTrigger(hour=5, minute=0),
            id="announcement_job",
            name="Announcement Monitoring Job",
            replace_existing=True,
        )

        # CSV export job: daily at 6 AM UTC (after all data jobs)
        self.scheduler.add_job(
            self._run_csv_export_job,
            trigger=CronTrigger(hour=6, minute=0),
            id="csv_export_job",
            name="CSV Export Job",
            replace_existing=True,
        )

        # Investor detection job: daily at 7 AM UTC (after CSV export)
        # Scans capital events, officers, and announcements for VC involvement
        self.scheduler.add_job(
            self._run_investor_detection_job,
            trigger=CronTrigger(hour=7, minute=0),
            id="investor_detection_job",
            name="Investor Detection Job",
            replace_existing=True,
        )

        # News monitoring job: daily at 8 AM UTC (no API calls, just RSS)
        self.scheduler.add_job(
            self._run_news_monitoring_job,
            trigger=CronTrigger(hour=8, minute=0),
            id="news_monitoring_job",
            name="News Monitoring Job",
            replace_existing=True,
        )

        # Website finder job: every 3 hours. High-signal new companies get a
        # best-effort inline lookup in announcement_job; this run covers the
        # long tail and re-checks companies where the earlier guess failed.
        self.scheduler.add_job(
            self._run_website_finder_job,
            trigger=CronTrigger(hour="*/3", minute=0),
            id="website_finder_job",
            name="Website Finder Job",
            replace_existing=True,
        )

        # Website scrape job: daily at 10 AM UTC (after website finder)
        # Scrapes found websites for company descriptions, tech keywords, etc.
        self.scheduler.add_job(
            self._run_website_scrape_job,
            trigger=CronTrigger(hour=10, minute=0),
            id="website_scrape_job",
            name="Website Scrape Job",
            replace_existing=True,
        )

        # Officer LinkedIn enrichment job: daily at 11 AM UTC (after website scrape)
        # Searches for officer LinkedIn profiles via DDG, extracts career data from snippets
        self.scheduler.add_job(
            self._run_officer_linkedin_job,
            trigger=CronTrigger(hour=11, minute=0),
            id="officer_linkedin_job",
            name="Officer LinkedIn Enrichment Job",
            replace_existing=True,
        )

        # Founder emergence detection: daily at 12 PM UTC (after officer LinkedIn)
        # Matches stealth founders against newly registered company officers
        self.scheduler.add_job(
            self._run_founder_emergence_job,
            trigger=CronTrigger(hour=12, minute=0),
            id="founder_emergence_job",
            name="Founder Emergence Detection Job",
            replace_existing=True,
        )

        # Registration scan job: every 4 hours (offset from discovery's even hours)
        # Scans sequential HRB numbers to find newest company registrations
        self.scheduler.add_job(
            self._run_registration_scan_job,
            trigger=CronTrigger(hour="1,5,9,13,17,21", minute=30),
            id="registration_scan_job",
            name="Registration Scan Job",
            replace_existing=True,
        )

        logger.info(
            "Jobs configured: discovery every %d hours, backfill 3AM+3PM, enrichment 4AM, "
            "announcements 5AM, CSV export 6AM, investor detection 7AM, news monitoring 8AM, "
            "website finder 9AM, website scrape 10AM, officer LinkedIn 11AM, "
            "founder emergence 12PM, registration scan every 4h",
            self.discovery_interval_hours,
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
                id="discovery_immediate",
                replace_existing=True,
            )

        if run_backfill_now:
            self.scheduler.add_job(
                self._run_backfill_job,
                id="backfill_immediate",
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
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                }
            )

        rate_state = self.rate_limiter.get_state()

        return {
            "running": self._running,
            "jobs": jobs,
            "rate_limit": {
                "tokens_available": rate_state.tokens_available,
                "requests_this_hour": rate_state.requests_this_hour,
                "can_request": rate_state.can_request,
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
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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
            if status["rate_limit"]["requests_this_hour"] > 0:
                logger.info(
                    "Status: %d requests this hour, %.1f tokens available",
                    status["rate_limit"]["requests_this_hour"],
                    status["rate_limit"]["tokens_available"],
                )

    except KeyboardInterrupt:
        scheduler.stop()
