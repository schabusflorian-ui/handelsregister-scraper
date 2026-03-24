from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.dependencies import DB_PATH, get_db, templates

router = APIRouter()


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_list(request: Request):
    """Job management page."""
    db = get_db()
    try:
        # Get recent job runs
        job_runs = db.conn.execute("""
            SELECT * FROM job_runs
            ORDER BY id DESC
            LIMIT 50
        """).fetchall()
        job_runs = [dict(row) for row in job_runs]

        # Get scrape runs
        scrape_runs = db.get_recent_scrape_runs(limit=20)

        # Get rate limiter status (if available)
        try:
            from scheduler.rate_limiter import PersistentRateLimiter

            rate_limiter = PersistentRateLimiter(DB_PATH)
            rate_state = rate_limiter.get_state()
        except:
            rate_state = None

        # Get enrichment queue size
        queue_size = db.get_enrichment_queue_size()

        return templates.TemplateResponse(
            "jobs.html",
            {
                "request": request,
                "job_runs": job_runs,
                "scrape_runs": scrape_runs,
                "rate_state": rate_state,
                "queue_size": queue_size,
            },
        )
    finally:
        db.close()
