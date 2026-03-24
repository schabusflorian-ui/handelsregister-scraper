from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard with statistics."""
    db = get_db()
    try:
        stats = db.get_statistics()

        # Get recent companies
        recent = db.search_companies(limit=10)

        # Get recent capital events
        capital_events = db.get_recent_capital_events(days=30)[:5]

        # Get job runs
        try:
            job_runs = db.conn.execute("""
                SELECT * FROM job_runs
                ORDER BY id DESC LIMIT 5
            """).fetchall()
            job_runs = [dict(row) for row in job_runs]
        except:
            job_runs = []

        # Get new (unviewed) companies with high AI score
        try:
            new_companies = db.conn.execute("""
                SELECT id, name, city, ai_robotics_score, startup_classification, first_seen_date
                FROM companies
                WHERE (viewed = 0 OR viewed IS NULL)
                  AND ai_robotics_score >= 3
                ORDER BY first_seen_date DESC, ai_robotics_score DESC
                LIMIT 10
            """).fetchall()
            new_companies = [dict(row) for row in new_companies]
            new_count = db.conn.execute("""
                SELECT COUNT(*) FROM companies
                WHERE (viewed = 0 OR viewed IS NULL) AND ai_robotics_score >= 3
            """).fetchone()[0]
        except:
            new_companies = []
            new_count = 0

        # Get contacted count
        try:
            contacted_count = db.conn.execute("SELECT COUNT(*) FROM companies WHERE contacted = 1").fetchone()[0]
        except:
            contacted_count = 0

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "stats": stats,
                "recent_companies": recent,
                "capital_events": capital_events,
                "job_runs": job_runs,
                "new_companies": new_companies,
                "new_count": new_count,
                "contacted_count": contacted_count,
            },
        )
    finally:
        db.close()


@router.get("/api/stats/refresh", response_class=HTMLResponse)
async def api_stats_refresh(request: Request):
    """Refresh stats partial."""
    db = get_db()
    try:
        stats = db.get_statistics()
        return templates.TemplateResponse(
            "partials/stats_cards.html",
            {
                "request": request,
                "stats": stats,
            },
        )
    finally:
        db.close()


@router.get("/api/news-stats")
async def news_stats():
    """Quick stats on news-sourced companies and recent job runs."""
    db = get_db()
    try:
        # News-sourced companies by day (last 14 days)
        by_day = db.conn.execute("""
            SELECT date(first_seen_date) as day, COUNT(*) as cnt
            FROM companies
            WHERE source LIKE '%news%'
              AND first_seen_date >= date('now', '-14 days')
            GROUP BY day ORDER BY day DESC
        """).fetchall()

        # All sources in last 7 days
        by_source = db.conn.execute("""
            SELECT source, COUNT(*) as cnt
            FROM companies
            WHERE first_seen_date >= date('now', '-7 days')
            GROUP BY source ORDER BY cnt DESC
        """).fetchall()

        # Recent news_monitoring job runs
        job_runs = db.conn.execute("""
            SELECT job_type, completed_at, companies_found, companies_new, requests_used
            FROM job_runs
            WHERE job_type = 'news_monitoring'
            ORDER BY id DESC LIMIT 14
        """).fetchall()

        # Also all job runs from last 7 days
        all_runs = db.conn.execute("""
            SELECT job_type, completed_at, companies_found, companies_new, requests_used
            FROM job_runs
            WHERE completed_at >= date('now', '-7 days')
            ORDER BY completed_at DESC
        """).fetchall()

        return {
            "news_companies_by_day": [dict(r) for r in by_day],
            "all_sources_last_7d": [dict(r) for r in by_source],
            "news_job_runs": [dict(r) for r in job_runs],
            "all_job_runs_last_7d": [dict(r) for r in all_runs],
        }
    finally:
        db.close()
