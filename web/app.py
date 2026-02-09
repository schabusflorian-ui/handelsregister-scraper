"""
Handelsregister Scraper - Web UI

A simple FastAPI-based web interface for browsing and managing
the Handelsregister startup discovery database.

Run with: python -m web.app
Or: uvicorn web.app:app --reload
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from persistence.database import Database

# Configuration
DB_PATH = os.environ.get('DB_PATH', 'handelsregister.db')
WEB_DIR = Path(__file__).parent

app = FastAPI(
    title="Handelsregister Scraper",
    description="AI/Robotics Startup Discovery Platform"
)

# Templates
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# Static files (if any local static files exist)
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def get_db() -> Database:
    """Get database connection."""
    return Database(DB_PATH)


def format_currency(amount: Optional[float], currency: str = "EUR") -> str:
    """Format currency amount."""
    if amount is None:
        return "-"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M {currency}"
    elif amount >= 1_000:
        return f"{amount / 1_000:.0f}K {currency}"
    return f"{amount:,.0f} {currency}"


def format_date(date_str: Optional[str]) -> str:
    """Format date string."""
    if not date_str:
        return "-"
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime("%Y-%m-%d")
    except:
        return date_str[:10] if len(date_str) >= 10 else date_str


# Add template filters
templates.env.filters["currency"] = format_currency
templates.env.filters["date"] = format_date
templates.env.filters["split"] = lambda s, sep=",": s.split(sep) if s else []


@app.get("/health")
async def health_check():
    """Simple health check endpoint for Railway."""
    try:
        db = get_db()
        # Quick DB check
        db.conn.execute("SELECT 1").fetchone()
        db.close()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


@app.get("/", response_class=HTMLResponse)
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

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "stats": stats,
            "recent_companies": recent,
            "capital_events": capital_events,
            "job_runs": job_runs,
        })
    finally:
        db.close()


@app.get("/companies", response_class=HTMLResponse)
async def companies_list(
    request: Request,
    q: Optional[str] = None,
    city: Optional[str] = None,
    min_score: Optional[int] = None,
    classification: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Company search and listing page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Build query
        conditions = []
        params = []

        if q:
            conditions.append("(name LIKE ? OR purpose LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if city:
            conditions.append("city = ?")
            params.append(city)
        if min_score is not None:
            conditions.append("ai_robotics_score >= ?")
            params.append(min_score)
        if classification:
            conditions.append("startup_classification = ?")
            params.append(classification)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Get total count
        count_query = f"SELECT COUNT(*) FROM companies WHERE {where_clause}"
        total = db.conn.execute(count_query, params).fetchone()[0]

        # Get companies
        query = f"""
            SELECT * FROM companies
            WHERE {where_clause}
            ORDER BY ai_robotics_score DESC, startup_score DESC, name
            LIMIT ? OFFSET ?
        """
        companies = db.conn.execute(query, params + [per_page, offset]).fetchall()
        companies = [dict(row) for row in companies]

        # Get cities for filter
        cities = db.conn.execute("""
            SELECT city, COUNT(*) as count
            FROM companies
            WHERE city IS NOT NULL AND city != ''
            GROUP BY city
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()

        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse("companies.html", {
            "request": request,
            "companies": companies,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
            "q": q or "",
            "city": city or "",
            "min_score": min_score,
            "classification": classification or "",
            "cities": cities,
        })
    finally:
        db.close()


@app.get("/companies/{company_id}", response_class=HTMLResponse)
async def company_detail(request: Request, company_id: int):
    """Company detail page."""
    db = get_db()
    try:
        company = db.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")

        # Get officers
        officers = db.get_officers(company_id)

        # Get capital events
        capital_events = db.get_capital_events(company_id)

        # Get investments
        try:
            investments = db.conn.execute("""
                SELECT inv.*, i.canonical_name as investor_name, i.type as investor_type
                FROM investments inv
                JOIN investors i ON inv.investor_id = i.id
                WHERE inv.company_id = ?
                ORDER BY inv.confidence DESC
            """, (company_id,)).fetchall()
            investments = [dict(row) for row in investments]
        except:
            investments = []

        # Get announcements
        try:
            announcements = db.conn.execute("""
                SELECT * FROM announcements
                WHERE company_id = ?
                ORDER BY announcement_date DESC
                LIMIT 10
            """, (company_id,)).fetchall()
            announcements = [dict(row) for row in announcements]
        except:
            announcements = []

        return templates.TemplateResponse("company_detail.html", {
            "request": request,
            "company": company,
            "officers": officers,
            "capital_events": capital_events,
            "investments": investments,
            "announcements": announcements,
        })
    finally:
        db.close()


@app.get("/investors", response_class=HTMLResponse)
async def investors_list(request: Request, page: int = 1, per_page: int = 25):
    """Investor listing page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Get investors with investment counts
        investors = db.conn.execute("""
            SELECT i.*, COUNT(inv.id) as investment_count
            FROM investors i
            LEFT JOIN investments inv ON i.id = inv.investor_id
            GROUP BY i.id
            ORDER BY investment_count DESC, i.canonical_name
            LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()
        investors = [dict(row) for row in investors]

        # Get total
        total = db.conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse("investors.html", {
            "request": request,
            "investors": investors,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        })
    finally:
        db.close()


@app.get("/investors/{investor_id}", response_class=HTMLResponse)
async def investor_detail(request: Request, investor_id: int):
    """Investor detail page."""
    db = get_db()
    try:
        investor = db.conn.execute(
            "SELECT * FROM investors WHERE id = ?", (investor_id,)
        ).fetchone()

        if not investor:
            raise HTTPException(status_code=404, detail="Investor not found")

        investor = dict(investor)

        # Get portfolio companies
        portfolio = db.conn.execute("""
            SELECT c.*, inv.confidence, inv.round_type, inv.detection_source
            FROM investments inv
            JOIN companies c ON inv.company_id = c.id
            WHERE inv.investor_id = ?
            ORDER BY inv.confidence DESC, c.ai_robotics_score DESC
        """, (investor_id,)).fetchall()
        portfolio = [dict(row) for row in portfolio]

        # Get aliases
        aliases = db.conn.execute(
            "SELECT * FROM investor_aliases WHERE investor_id = ?", (investor_id,)
        ).fetchall()
        aliases = [dict(row) for row in aliases]

        # Get legal entities
        entities = db.conn.execute(
            "SELECT * FROM investor_legal_entities WHERE investor_id = ?", (investor_id,)
        ).fetchall()
        entities = [dict(row) for row in entities]

        return templates.TemplateResponse("investor_detail.html", {
            "request": request,
            "investor": investor,
            "portfolio": portfolio,
            "aliases": aliases,
            "entities": entities,
        })
    finally:
        db.close()


@app.get("/capital-events", response_class=HTMLResponse)
async def capital_events(request: Request, days: int = 30, page: int = 1, per_page: int = 25):
    """Capital events page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        events = db.conn.execute("""
            SELECT ce.*, c.name as company_name, c.city, c.ai_robotics_score
            FROM capital_events ce
            JOIN companies c ON ce.company_id = c.id
            WHERE ce.detected_at >= datetime('now', '-' || ? || ' days')
            ORDER BY ce.detected_at DESC
            LIMIT ? OFFSET ?
        """, (days, per_page, offset)).fetchall()
        events = [dict(row) for row in events]

        # Get total
        total = db.conn.execute("""
            SELECT COUNT(*) FROM capital_events
            WHERE detected_at >= datetime('now', '-' || ? || ' days')
        """, (days,)).fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse("capital_events.html", {
            "request": request,
            "events": events,
            "days": days,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        })
    finally:
        db.close()


@app.get("/jobs", response_class=HTMLResponse)
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

        return templates.TemplateResponse("jobs.html", {
            "request": request,
            "job_runs": job_runs,
            "scrape_runs": scrape_runs,
            "rate_state": rate_state,
            "queue_size": queue_size,
        })
    finally:
        db.close()


@app.get("/founders", response_class=HTMLResponse)
async def founders_list(request: Request, page: int = 1, per_page: int = 25):
    """Stealth founders page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Get founders
        founders = db.conn.execute("""
            SELECT sf.*, c.name as company_name
            FROM stealth_founders sf
            LEFT JOIN companies c ON sf.company_id = c.id
            ORDER BY sf.confidence_score DESC, sf.first_seen_at DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset)).fetchall()
        founders = [dict(row) for row in founders]

        # Get total
        total = db.conn.execute("SELECT COUNT(*) FROM stealth_founders").fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse("founders.html", {
            "request": request,
            "founders": founders,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        })
    finally:
        db.close()


@app.get("/export/csv")
async def export_csv(
    min_score: int = 1,
    classification: Optional[str] = None,
    limit: int = 10000,
):
    """Export companies to CSV."""
    from fastapi.responses import StreamingResponse
    import csv
    import io

    db = get_db()
    try:
        conditions = ["ai_robotics_score >= ?"]
        params = [min_score]

        if classification:
            conditions.append("startup_classification = ?")
            params.append(classification)

        where_clause = " AND ".join(conditions)

        companies = db.conn.execute(f"""
            SELECT * FROM companies
            WHERE {where_clause}
            ORDER BY ai_robotics_score DESC
            LIMIT ?
        """, params + [limit]).fetchall()

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        columns = [
            "id", "name", "city", "registration_date", "capital_amount",
            "ai_robotics_score", "startup_score", "startup_classification",
            "website", "purpose"
        ]
        writer.writerow(columns)

        # Data
        for company in companies:
            company = dict(company)
            writer.writerow([company.get(col) for col in columns])

        output.seek(0)

        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=companies.csv"}
        )
    finally:
        db.close()


# API endpoints for HTMX partial updates

@app.get("/api/companies/search", response_class=HTMLResponse)
async def api_companies_search(
    request: Request,
    q: str = "",
    min_score: int = 0,
    limit: int = 10,
):
    """Quick company search for autocomplete."""
    db = get_db()
    try:
        companies = db.conn.execute("""
            SELECT id, name, city, ai_robotics_score, startup_classification
            FROM companies
            WHERE name LIKE ? AND ai_robotics_score >= ?
            ORDER BY ai_robotics_score DESC
            LIMIT ?
        """, (f"%{q}%", min_score, limit)).fetchall()
        companies = [dict(row) for row in companies]

        return templates.TemplateResponse("partials/company_list.html", {
            "request": request,
            "companies": companies,
        })
    finally:
        db.close()


@app.get("/api/stats/refresh", response_class=HTMLResponse)
async def api_stats_refresh(request: Request):
    """Refresh stats partial."""
    db = get_db()
    try:
        stats = db.get_statistics()
        return templates.TemplateResponse("partials/stats_cards.html", {
            "request": request,
            "stats": stats,
        })
    finally:
        db.close()


@app.get("/admin/restore-db")
async def admin_restore_db():
    """Restore database from backup file."""
    import base64
    import os

    backup_path = "/app/data/db_backup.b64"
    db_path = os.environ.get('DATABASE_PATH', '/data/handelsregister.db')

    if not os.path.exists(backup_path):
        return {"error": "Backup file not found", "path": backup_path}

    try:
        with open(backup_path, 'r') as f:
            encoded = f.read().strip()

        data = base64.b64decode(encoded)

        # Write to database
        with open(db_path, 'wb') as f:
            f.write(data)

        return {
            "success": True,
            "bytes_restored": len(data),
            "db_path": db_path,
            "message": "Database restored! Refresh the dashboard to see changes."
        }
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
