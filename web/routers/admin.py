import logging
import os
import threading
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.dependencies import DB_PATH, get_db, templates

router = APIRouter()

logger = logging.getLogger(__name__)

# =============================================================================
# Background Stealth Founder Job Scheduler
# =============================================================================

# Job status tracking (in-memory, resets on restart)
stealth_job_status = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "scheduled": False,
    "interval_hours": 6,
    "error": None,
}

scheduler = None
job_lock = threading.Lock()


def run_stealth_job_sync():
    """Run the stealth founder job synchronously (for background thread)."""
    global stealth_job_status

    with job_lock:
        if stealth_job_status["running"]:
            logger.warning("Stealth job already running, skipping")
            return

        stealth_job_status["running"] = True
        stealth_job_status["error"] = None

    try:
        logger.info("Starting stealth founder discovery job...")

        from persistence.database import Database
        from scheduler.jobs.stealth_founder_job import StealthFounderJob

        db = Database(DB_PATH)
        try:
            job = StealthFounderJob(
                db=db,
                max_queries=3,  # Conservative for cloud
                max_profiles_to_scrape=10,
                min_confidence=0.3,
                google_delay=(20, 60),  # Longer delays for cloud IPs
                linkedin_delay=(10, 30),
            )
            result = job.run()

            stealth_job_status["last_result"] = result
            stealth_job_status["last_run"] = datetime.now().isoformat()
            logger.info(f"Stealth job completed: {result}")

        finally:
            db.close()

    except Exception as e:
        logger.error(f"Stealth job failed: {e}")
        stealth_job_status["error"] = str(e)
        stealth_job_status["last_run"] = datetime.now().isoformat()

    finally:
        stealth_job_status["running"] = False


def start_scheduler():
    """Start the APScheduler background scheduler."""
    global scheduler

    if scheduler is not None:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler()

        # Add stealth job on schedule (default: every 6 hours)
        if os.environ.get("ENABLE_STEALTH_SCHEDULER", "false").lower() == "true":
            interval_hours = int(os.environ.get("STEALTH_INTERVAL_HOURS", "6"))
            scheduler.add_job(
                run_stealth_job_sync,
                trigger=IntervalTrigger(hours=interval_hours),
                id="stealth_founder_job",
                name="Stealth Founder Discovery",
                replace_existing=True,
            )
            stealth_job_status["scheduled"] = True
            stealth_job_status["interval_hours"] = interval_hours
            logger.info(f"Stealth job scheduled every {interval_hours} hours")

        scheduler.start()
        logger.info("Background scheduler started")

    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")


@router.get("/admin/restore-db")
async def admin_restore_db():
    """Restore database from backup file."""
    import base64
    import os

    backup_path = "/app/data/db_backup.b64"
    db_path = os.environ.get("DATABASE_PATH", "/data/handelsregister.db")

    if not os.path.exists(backup_path):
        return {"error": "Backup file not found", "path": backup_path}

    try:
        with open(backup_path) as f:
            encoded = f.read().strip()

        data = base64.b64decode(encoded)

        # Write to database
        with open(db_path, "wb") as f:
            f.write(data)

        return {
            "success": True,
            "bytes_restored": len(data),
            "db_path": db_path,
            "message": "Database restored! Refresh the dashboard to see changes.",
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/admin/stealth-job", response_class=HTMLResponse)
async def admin_stealth_job_page(request: Request):
    """Stealth job management page."""
    db = get_db()
    try:
        # Get stealth founder stats
        try:
            founder_count = db.conn.execute("SELECT COUNT(*) FROM stealth_founders").fetchone()[0]
            high_conf_count = db.conn.execute(
                "SELECT COUNT(*) FROM stealth_founders WHERE confidence_score >= 0.6"
            ).fetchone()[0]
            recent_founders = db.conn.execute("""
                SELECT name, headline, location, confidence_score, first_seen_at
                FROM stealth_founders
                ORDER BY first_seen_at DESC
                LIMIT 5
            """).fetchall()
            recent_founders = [dict(r) for r in recent_founders]
        except:
            founder_count = 0
            high_conf_count = 0
            recent_founders = []

        return templates.TemplateResponse(
            "admin_stealth_job.html",
            {
                "request": request,
                "status": stealth_job_status,
                "founder_count": founder_count,
                "high_conf_count": high_conf_count,
                "recent_founders": recent_founders,
                "env_enabled": os.environ.get("ENABLE_STEALTH_SCHEDULER", "false"),
            },
        )
    finally:
        db.close()


@router.get("/admin/stealth-job/status")
async def admin_stealth_job_status():
    """Get stealth job status (API)."""
    return stealth_job_status


@router.post("/admin/stealth-job/run")
async def admin_stealth_job_run():
    """Trigger a manual stealth job run."""
    if stealth_job_status["running"]:
        return {"error": "Job already running", "status": stealth_job_status}

    # Run in background thread
    thread = threading.Thread(target=run_stealth_job_sync, daemon=True)
    thread.start()

    return {
        "message": "Stealth job started in background",
        "status": stealth_job_status,
    }


@router.post("/admin/stealth-job/schedule")
async def admin_stealth_job_schedule(hours: int = 6):
    """Enable/update scheduled stealth job."""
    global scheduler

    if scheduler is None:
        start_scheduler()

    try:
        from apscheduler.triggers.interval import IntervalTrigger

        # Remove existing job if any
        try:
            scheduler.remove_job("stealth_founder_job")
        except:
            pass

        # Add new scheduled job
        scheduler.add_job(
            run_stealth_job_sync,
            trigger=IntervalTrigger(hours=hours),
            id="stealth_founder_job",
            name="Stealth Founder Discovery",
            replace_existing=True,
        )

        stealth_job_status["scheduled"] = True
        stealth_job_status["interval_hours"] = hours

        return {
            "message": f"Stealth job scheduled every {hours} hours",
            "status": stealth_job_status,
        }

    except Exception as e:
        return {"error": str(e)}


@router.post("/admin/stealth-job/stop")
async def admin_stealth_job_stop():
    """Stop scheduled stealth job."""
    global scheduler

    if scheduler:
        try:
            scheduler.remove_job("stealth_founder_job")
            stealth_job_status["scheduled"] = False
            return {"message": "Scheduled job stopped", "status": stealth_job_status}
        except:
            pass

    return {"message": "No scheduled job to stop", "status": stealth_job_status}


# =============================================================================
# Sync Founders from Local
# =============================================================================


@router.get("/admin/sync-founders", response_class=HTMLResponse)
async def admin_sync_founders_page(request: Request):
    """Page to sync stealth founders from local machine."""
    db = get_db()
    try:
        try:
            founder_count = db.conn.execute("SELECT COUNT(*) FROM stealth_founders").fetchone()[0]
        except:
            founder_count = 0

        return templates.TemplateResponse(
            "admin_sync_founders.html",
            {
                "request": request,
                "founder_count": founder_count,
            },
        )
    finally:
        db.close()


@router.post("/admin/sync-founders")
async def admin_sync_founders_post(request: Request):
    """
    Receive stealth founders data from local machine.
    Accepts JSON array of founder objects or base64-encoded JSON.
    """
    import base64
    import json

    db = get_db()
    try:
        # Try to parse form data
        form = await request.form()
        data_str = form.get("data", "")

        if not data_str:
            # Try JSON body
            try:
                body = await request.json()
                founders = body if isinstance(body, list) else body.get("founders", [])
            except:
                return {"error": "No data provided"}
        else:
            # Check if base64 encoded
            try:
                if data_str.startswith("eyJ") or data_str.startswith("W3si"):
                    # Looks like base64
                    decoded = base64.b64decode(data_str).decode("utf-8")
                    founders = json.loads(decoded)
                else:
                    founders = json.loads(data_str)
            except Exception as e:
                return {"error": f"Failed to parse data: {e}"}

        if not isinstance(founders, list):
            founders = [founders]

        # Insert/update founders
        inserted = 0
        updated = 0
        errors = []

        for founder in founders:
            try:
                linkedin_url = founder.get("linkedin_url")
                if not linkedin_url:
                    errors.append("Missing linkedin_url")
                    continue

                # Check if exists
                existing = db.conn.execute(
                    "SELECT id FROM stealth_founders WHERE linkedin_url = ?", (linkedin_url,)
                ).fetchone()

                if existing:
                    # Update
                    db.conn.execute(
                        """
                        UPDATE stealth_founders SET
                            name = COALESCE(?, name),
                            headline = COALESCE(?, headline),
                            location = COALESCE(?, location),
                            summary = COALESCE(?, summary),
                            current_company = COALESCE(?, current_company),
                            previous_companies = COALESCE(?, previous_companies),
                            detection_source = COALESCE(?, detection_source),
                            search_query = COALESCE(?, search_query),
                            stealth_signals = COALESCE(?, stealth_signals),
                            confidence_score = COALESCE(?, confidence_score),
                            last_checked_at = COALESCE(?, last_checked_at)
                        WHERE linkedin_url = ?
                    """,
                        (
                            founder.get("name"),
                            founder.get("headline"),
                            founder.get("location"),
                            founder.get("summary"),
                            founder.get("current_company"),
                            founder.get("previous_companies"),
                            founder.get("detection_source"),
                            founder.get("search_query"),
                            founder.get("stealth_signals"),
                            founder.get("confidence_score"),
                            founder.get("last_checked_at"),
                            linkedin_url,
                        ),
                    )
                    updated += 1
                else:
                    # Insert
                    db.conn.execute(
                        """
                        INSERT INTO stealth_founders (
                            linkedin_url, name, headline, location, summary,
                            current_company, previous_companies, detection_source,
                            search_query, stealth_signals, confidence_score,
                            first_seen_at, last_checked_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            linkedin_url,
                            founder.get("name"),
                            founder.get("headline"),
                            founder.get("location"),
                            founder.get("summary"),
                            founder.get("current_company"),
                            founder.get("previous_companies"),
                            founder.get("detection_source", "local_sync"),
                            founder.get("search_query"),
                            founder.get("stealth_signals"),
                            founder.get("confidence_score", 0.0),
                            founder.get("first_seen_at", datetime.now().isoformat()),
                            founder.get("last_checked_at"),
                        ),
                    )
                    inserted += 1

            except Exception as e:
                errors.append(f"{founder.get('linkedin_url', 'unknown')}: {e}")

        db.conn.commit()

        return {
            "success": True,
            "inserted": inserted,
            "updated": updated,
            "total": inserted + updated,
            "errors": errors[:10] if errors else None,  # Limit error output
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()
