"""
Handelsregister Scraper - Web UI

A simple FastAPI-based web interface for browsing and managing
the Handelsregister startup discovery database.

Run with: python -m web.app
Or: uvicorn web.app:app --reload
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from persistence.database import Database

# Configuration
DB_PATH = os.environ.get("DB_PATH", "handelsregister.db")
WEB_DIR = Path(__file__).parent

app = FastAPI(title="Handelsregister Scraper", description="AI/Robotics Startup Discovery Platform")

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
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except:
        return date_str[:10] if len(date_str) >= 10 else date_str


# Add template filters
templates.env.filters["currency"] = format_currency
templates.env.filters["date"] = format_date
templates.env.filters["split"] = lambda s, sep=",": s.split(sep) if s else []

import json as _json

templates.env.filters["from_json"] = lambda s: _json.loads(s) if s else []


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


@app.get("/companies", response_class=HTMLResponse)
async def companies_list(
    request: Request,
    q: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    legal_form: Optional[str] = None,
    year: Optional[str] = None,  # Accept string, parse manually
    min_score: Optional[str] = None,  # Accept string, parse manually
    classification: Optional[str] = None,
    has_website: Optional[str] = None,  # Accept string, parse manually
    min_climate: Optional[str] = None,  # Accept string, parse manually
    contacted: Optional[str] = None,  # 'yes', 'no', or None for all
    viewed: Optional[str] = None,  # 'yes', 'no', or None for all
    relevance: Optional[str] = None,  # 'relevant', 'irrelevant', 'unscreened', or None
    sort: Optional[str] = None,  # Column to sort by
    sort_dir: Optional[str] = None,  # 'asc' or 'desc'
    page: int = 1,
    per_page: int = 25,
):
    """Company search and listing page."""
    # Parse string params that may be empty
    year_int = int(year) if year and year.isdigit() else None
    min_score_int = int(min_score) if min_score and min_score.lstrip("-").isdigit() else None
    min_climate_int = int(min_climate) if min_climate and min_climate.lstrip("-").isdigit() else None
    has_website_bool = has_website == "true" if has_website else None

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
        if state:
            conditions.append("state = ?")
            params.append(state)
        if legal_form:
            conditions.append("legal_form = ?")
            params.append(legal_form)
        if year_int:
            conditions.append("substr(first_seen_date, 1, 4) = ?")
            params.append(str(year_int))
        if min_score_int is not None:
            conditions.append("ai_robotics_score >= ?")
            params.append(min_score_int)
        if min_climate_int is not None:
            conditions.append("climate_score >= ?")
            params.append(min_climate_int)
        if classification:
            conditions.append("startup_classification = ?")
            params.append(classification)
        if has_website_bool:
            conditions.append("website IS NOT NULL")
        if contacted == "yes":
            conditions.append("contacted = 1")
        elif contacted == "no":
            conditions.append("(contacted = 0 OR contacted IS NULL)")
        if viewed == "yes":
            conditions.append("viewed = 1")
        elif viewed == "no":
            conditions.append("(viewed = 0 OR viewed IS NULL)")
        if relevance == "relevant":
            conditions.append("relevance = 'relevant'")
        elif relevance == "irrelevant":
            conditions.append("relevance = 'irrelevant'")
        elif relevance == "unscreened":
            conditions.append("(relevance IS NULL)")

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Sorting
        allowed_sort_cols = {
            "name": "name",
            "legal_form": "legal_form",
            "city": "city",
            "state": "state",
            "year": "first_seen_date",
            "ai_score": "ai_robotics_score",
            "climate_score": "climate_score",
            "classification": "startup_classification",
            "startup_score": "startup_score",
            "capital": "capital_amount",
            "registry": "registry_court",
            "reg_date": "registration_date",
            "source": "source",
            "relevance": "relevance",
        }
        sort_column = allowed_sort_cols.get(sort)
        sort_direction = "ASC" if sort_dir == "asc" else "DESC"
        if sort_column:
            order_clause = f"{sort_column} {sort_direction} NULLS LAST, name"
        else:
            order_clause = "first_seen_date DESC, ai_robotics_score DESC, name"

        # Get total count
        count_query = f"SELECT COUNT(*) FROM companies WHERE {where_clause}"
        total = db.conn.execute(count_query, params).fetchone()[0]

        # Get companies
        query = f"""
            SELECT * FROM companies
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """
        companies = db.conn.execute(query, params + [per_page, offset]).fetchall()
        companies = [dict(row) for row in companies]

        # Get filter options
        cities = db.conn.execute("""
            SELECT city, COUNT(*) as count
            FROM companies
            WHERE city IS NOT NULL AND city != ''
            GROUP BY city
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()

        states = db.conn.execute("""
            SELECT state, COUNT(*) as count
            FROM companies
            WHERE state IS NOT NULL AND state != ''
            GROUP BY state
            ORDER BY count DESC
        """).fetchall()

        legal_forms = db.conn.execute("""
            SELECT legal_form, COUNT(*) as count
            FROM companies
            WHERE legal_form IS NOT NULL AND legal_form != ''
            GROUP BY legal_form
            ORDER BY count DESC
            LIMIT 15
        """).fetchall()

        years = db.conn.execute("""
            SELECT substr(first_seen_date, 1, 4) as year, COUNT(*) as count
            FROM companies
            WHERE first_seen_date IS NOT NULL
            GROUP BY year
            ORDER BY year DESC
        """).fetchall()

        total_pages = (total + per_page - 1) // per_page

        # Load saved filter presets
        try:
            filter_presets = db.conn.execute("SELECT * FROM filter_presets ORDER BY name").fetchall()
            filter_presets = [dict(row) for row in filter_presets]
        except:
            filter_presets = []

        # Build filter query string for pagination links (exclude empty values)
        filter_params = {}
        if q:
            filter_params["q"] = q
        if year:
            filter_params["year"] = year
        if city:
            filter_params["city"] = city
        if state:
            filter_params["state"] = state
        if legal_form:
            filter_params["legal_form"] = legal_form
        if min_score:
            filter_params["min_score"] = min_score
        if min_climate:
            filter_params["min_climate"] = min_climate
        if classification:
            filter_params["classification"] = classification
        if has_website:
            filter_params["has_website"] = "true"
        if contacted:
            filter_params["contacted"] = contacted
        if viewed:
            filter_params["viewed"] = viewed
        if relevance:
            filter_params["relevance"] = relevance
        if per_page != 25:
            filter_params["per_page"] = per_page
        from urllib.parse import urlencode

        # filter_qs excludes sort/page so sort links and pagination can set them
        filter_qs = urlencode(filter_params)
        if sort:
            filter_params["sort"] = sort
        if sort_dir:
            filter_params["sort_dir"] = sort_dir
        # filter_qs_with_sort includes sort for pagination links
        filter_qs_with_sort = urlencode(filter_params)

        return templates.TemplateResponse(
            "companies.html",
            {
                "request": request,
                "companies": companies,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "q": q or "",
                "city": city or "",
                "state": state or "",
                "legal_form": legal_form or "",
                "year": year or "",
                "min_score": min_score or "",
                "min_climate": min_climate or "",
                "classification": classification or "",
                "has_website": has_website or "",
                "contacted": contacted or "",
                "viewed": viewed or "",
                "relevance": relevance or "",
                "sort": sort or "",
                "sort_dir": sort_dir or "",
                "filter_qs": filter_qs,
                "filter_qs_with_sort": filter_qs_with_sort,
                "filter_presets": filter_presets,
                "cities": cities,
                "states": states,
                "legal_forms": legal_forms,
                "years": years,
            },
        )
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
            investments = db.conn.execute(
                """
                SELECT inv.*, i.canonical_name as investor_name, i.type as investor_type
                FROM investments inv
                JOIN investors i ON inv.investor_id = i.id
                WHERE inv.company_id = ?
                ORDER BY inv.confidence DESC
            """,
                (company_id,),
            ).fetchall()
            investments = [dict(row) for row in investments]
        except:
            investments = []

        # Get announcements
        try:
            announcements = db.conn.execute(
                """
                SELECT * FROM announcements
                WHERE company_id = ?
                ORDER BY announcement_date DESC
                LIMIT 10
            """,
                (company_id,),
            ).fetchall()
            announcements = [dict(row) for row in announcements]
        except:
            announcements = []

        # Mark as viewed
        try:
            db.conn.execute(
                """
                UPDATE companies SET viewed = 1, viewed_at = COALESCE(viewed_at, datetime('now'))
                WHERE id = ?
            """,
                (company_id,),
            )
            db.conn.commit()
            company["viewed"] = 1
        except:
            pass

        return templates.TemplateResponse(
            "company_detail.html",
            {
                "request": request,
                "company": company,
                "officers": officers,
                "capital_events": capital_events,
                "investments": investments,
                "announcements": announcements,
            },
        )
    finally:
        db.close()


@app.post("/companies/{company_id}/toggle-contacted")
async def toggle_contacted(company_id: int):
    """Toggle contacted status for a company."""
    db = get_db()
    try:
        # Get current status
        current = db.conn.execute("SELECT contacted FROM companies WHERE id = ?", (company_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Company not found")

        new_status = 0 if current[0] else 1
        contacted_at = datetime.now().isoformat() if new_status else None

        db.conn.execute(
            """
            UPDATE companies SET contacted = ?, contacted_at = ?
            WHERE id = ?
        """,
            (new_status, contacted_at, company_id),
        )
        db.conn.commit()

        return {"success": True, "contacted": new_status}
    finally:
        db.close()


@app.post("/companies/{company_id}/relevance")
async def set_relevance(company_id: int, request: Request):
    """Set relevance for a company (relevant/irrelevant/null)."""
    db = get_db()
    try:
        body = await request.json()
        relevance = body.get("relevance")  # 'relevant', 'irrelevant', or null to clear
        if relevance not in ("relevant", "irrelevant", None):
            raise HTTPException(status_code=400, detail="Invalid relevance value")

        db.conn.execute("UPDATE companies SET relevance = ? WHERE id = ?", (relevance, company_id))
        db.conn.commit()

        return {"success": True, "relevance": relevance}
    finally:
        db.close()


@app.post("/companies/bulk-relevance")
async def bulk_set_relevance(request: Request):
    """Set relevance for multiple companies at once."""
    db = get_db()
    try:
        body = await request.json()
        company_ids = body.get("company_ids", [])
        relevance = body.get("relevance")
        if relevance not in ("relevant", "irrelevant", None):
            raise HTTPException(status_code=400, detail="Invalid relevance value")
        if not company_ids or not isinstance(company_ids, list):
            raise HTTPException(status_code=400, detail="company_ids must be a non-empty list")

        placeholders = ",".join("?" for _ in company_ids)
        db.conn.execute(f"UPDATE companies SET relevance = ? WHERE id IN ({placeholders})", [relevance] + company_ids)
        db.conn.commit()

        return {"success": True, "updated": len(company_ids), "relevance": relevance}
    finally:
        db.close()


@app.post("/companies/{company_id}/notes")
async def update_notes(company_id: int, request: Request):
    """Update notes for a company."""
    db = get_db()
    try:
        body = await request.json()
        notes = body.get("notes", "")

        db.conn.execute("UPDATE companies SET notes = ? WHERE id = ?", (notes, company_id))
        db.conn.commit()

        return {"success": True}
    finally:
        db.close()


@app.get("/api/filter-presets")
async def list_filter_presets():
    """List all saved filter presets."""
    db = get_db()
    try:
        presets = db.conn.execute("SELECT * FROM filter_presets ORDER BY name").fetchall()
        return [dict(row) for row in presets]
    finally:
        db.close()


@app.post("/api/filter-presets")
async def create_filter_preset(request: Request):
    """Save current filters as a named preset."""
    db = get_db()
    try:
        body = await request.json()
        name = body.get("name", "").strip()
        params = body.get("params", "")
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")

        cursor = db.conn.execute("INSERT INTO filter_presets (name, params) VALUES (?, ?)", (name, params))
        db.conn.commit()

        return {"success": True, "id": cursor.lastrowid, "name": name}
    finally:
        db.close()


@app.delete("/api/filter-presets/{preset_id}")
async def delete_filter_preset(preset_id: int):
    """Delete a saved filter preset."""
    db = get_db()
    try:
        db.conn.execute("DELETE FROM filter_presets WHERE id = ?", (preset_id,))
        db.conn.commit()
        return {"success": True}
    finally:
        db.close()


@app.get("/investors", response_class=HTMLResponse)
async def investors_list(request: Request, page: int = 1, per_page: int = 25):
    """Investor listing page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Get investors with investment counts
        investors = db.conn.execute(
            """
            SELECT i.*, COUNT(inv.id) as investment_count
            FROM investors i
            LEFT JOIN investments inv ON i.id = inv.investor_id
            GROUP BY i.id
            ORDER BY investment_count DESC, i.canonical_name
            LIMIT ? OFFSET ?
        """,
            (per_page, offset),
        ).fetchall()
        investors = [dict(row) for row in investors]

        # Get total
        total = db.conn.execute("SELECT COUNT(*) FROM investors").fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse(
            "investors.html",
            {
                "request": request,
                "investors": investors,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            },
        )
    finally:
        db.close()


@app.get("/investors/{investor_id}", response_class=HTMLResponse)
async def investor_detail(request: Request, investor_id: int):
    """Investor detail page."""
    db = get_db()
    try:
        investor = db.conn.execute("SELECT * FROM investors WHERE id = ?", (investor_id,)).fetchone()

        if not investor:
            raise HTTPException(status_code=404, detail="Investor not found")

        investor = dict(investor)

        # Get portfolio companies
        portfolio = db.conn.execute(
            """
            SELECT c.*, inv.confidence, inv.round_type, inv.detection_source
            FROM investments inv
            JOIN companies c ON inv.company_id = c.id
            WHERE inv.investor_id = ?
            ORDER BY inv.confidence DESC, c.ai_robotics_score DESC
        """,
            (investor_id,),
        ).fetchall()
        portfolio = [dict(row) for row in portfolio]

        # Get aliases
        aliases = db.conn.execute("SELECT * FROM investor_aliases WHERE investor_id = ?", (investor_id,)).fetchall()
        aliases = [dict(row) for row in aliases]

        # Get legal entities
        entities = db.conn.execute(
            "SELECT * FROM investor_legal_entities WHERE investor_id = ?", (investor_id,)
        ).fetchall()
        entities = [dict(row) for row in entities]

        return templates.TemplateResponse(
            "investor_detail.html",
            {
                "request": request,
                "investor": investor,
                "portfolio": portfolio,
                "aliases": aliases,
                "entities": entities,
            },
        )
    finally:
        db.close()


@app.get("/capital-events", response_class=HTMLResponse)
async def capital_events(request: Request, days: int = 30, page: int = 1, per_page: int = 25):
    """Capital events page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        events = db.conn.execute(
            """
            SELECT ce.*, c.name as company_name, c.city, c.ai_robotics_score
            FROM capital_events ce
            JOIN companies c ON ce.company_id = c.id
            WHERE ce.detected_at >= datetime('now', '-' || ? || ' days')
            ORDER BY ce.detected_at DESC
            LIMIT ? OFFSET ?
        """,
            (days, per_page, offset),
        ).fetchall()
        events = [dict(row) for row in events]

        # Get total
        total = db.conn.execute(
            """
            SELECT COUNT(*) FROM capital_events
            WHERE detected_at >= datetime('now', '-' || ? || ' days')
        """,
            (days,),
        ).fetchone()[0]
        total_pages = (total + per_page - 1) // per_page

        return templates.TemplateResponse(
            "capital_events.html",
            {
                "request": request,
                "events": events,
                "days": days,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            },
        )
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


@app.get("/stealth-founders")
async def stealth_founders_redirect():
    """Redirect to founders page."""
    return RedirectResponse(url="/founders", status_code=302)


@app.get("/founders", response_class=HTMLResponse)
async def founders_list(
    request: Request,
    q: Optional[str] = None,
    location: Optional[str] = None,
    emerged: Optional[str] = None,  # 'yes', 'no', or None
    contacted: Optional[str] = None,  # 'yes', 'no', or None
    viewed: Optional[str] = None,  # 'yes', 'no', or None
    relevance: Optional[str] = None,  # 'relevant', 'irrelevant', 'unscreened', or None
    min_confidence: Optional[str] = None,
    # Tag filters
    data_quality: Optional[str] = None,
    stealth_strength: Optional[str] = None,
    founder_role: Optional[str] = None,
    ex_company_tier: Optional[str] = None,
    geo_region: Optional[str] = None,
    sector: Optional[str] = None,
    sort: Optional[str] = None,
    sort_dir: Optional[str] = None,
    page: int = 1,
    per_page: int = 25,
):
    """Stealth founders page."""
    min_confidence_int = int(min_confidence) if min_confidence and min_confidence.isdigit() else None

    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Build query
        conditions = []
        params = []

        if q:
            conditions.append("(sf.name LIKE ? OR sf.headline LIKE ? OR sf.current_company LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        if location:
            conditions.append("sf.location = ?")
            params.append(location)
        if emerged == "yes":
            conditions.append("sf.company_id IS NOT NULL")
        elif emerged == "no":
            conditions.append("sf.company_id IS NULL")
        if contacted == "yes":
            conditions.append("sf.contacted = 1")
        elif contacted == "no":
            conditions.append("(sf.contacted = 0 OR sf.contacted IS NULL)")
        if viewed == "yes":
            conditions.append("sf.viewed = 1")
        elif viewed == "no":
            conditions.append("(sf.viewed = 0 OR sf.viewed IS NULL)")
        if relevance == "relevant":
            conditions.append("sf.relevance = 'relevant'")
        elif relevance == "irrelevant":
            conditions.append("sf.relevance = 'irrelevant'")
        elif relevance == "unscreened":
            conditions.append("(sf.relevance IS NULL)")
        if min_confidence_int is not None:
            conditions.append("sf.confidence_score >= ?")
            params.append(min_confidence_int / 100.0)
        # Tag filters
        if data_quality:
            conditions.append("sf.data_quality = ?")
            params.append(data_quality)
        if stealth_strength:
            conditions.append("sf.stealth_strength = ?")
            params.append(stealth_strength)
        if founder_role:
            conditions.append("sf.founder_role = ?")
            params.append(founder_role)
        if ex_company_tier:
            if ex_company_tier == "any":
                conditions.append("sf.ex_company_tier IS NOT NULL")
            else:
                conditions.append("sf.ex_company_tier = ?")
                params.append(ex_company_tier)
        if geo_region:
            conditions.append("sf.geo_region = ?")
            params.append(geo_region)
        if sector:
            conditions.append("sf.sector_tags LIKE ?")
            params.append(f'%"{sector}"%')

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Sorting
        allowed_sort_cols = {
            "name": "sf.name",
            "headline": "sf.headline",
            "location": "sf.location",
            "confidence": "sf.confidence_score",
            "emerged": "sf.company_id",
            "first_seen": "sf.first_seen_at",
            "relevance": "sf.relevance",
            "stealth_strength": "sf.stealth_strength",
            "data_quality": "sf.data_quality",
            "ex_company_tier": "sf.ex_company_tier",
            "geo_region": "sf.geo_region",
        }
        sort_column = allowed_sort_cols.get(sort)
        sort_direction = "ASC" if sort_dir == "asc" else "DESC"
        if sort_column:
            order_clause = f"{sort_column} {sort_direction} NULLS LAST, sf.name"
        else:
            order_clause = "sf.confidence_score DESC, sf.first_seen_at DESC"

        # Get total count
        count_query = f"SELECT COUNT(*) FROM stealth_founders sf WHERE {where_clause}"
        total = db.conn.execute(count_query, params).fetchone()[0]

        # Get founders
        query = f"""
            SELECT sf.*, c.name as company_name
            FROM stealth_founders sf
            LEFT JOIN companies c ON sf.company_id = c.id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """
        founders = db.conn.execute(query, params + [per_page, offset]).fetchall()
        founders = [dict(row) for row in founders]

        total_pages = (total + per_page - 1) // per_page

        # Get filter options
        locations = db.conn.execute("""
            SELECT location, COUNT(*) as count
            FROM stealth_founders
            WHERE location IS NOT NULL AND location != ''
            GROUP BY location
            ORDER BY count DESC
            LIMIT 20
        """).fetchall()

        # Build filter query string
        from urllib.parse import urlencode

        filter_params = {}
        if q:
            filter_params["q"] = q
        if location:
            filter_params["location"] = location
        if emerged:
            filter_params["emerged"] = emerged
        if contacted:
            filter_params["contacted"] = contacted
        if viewed:
            filter_params["viewed"] = viewed
        if relevance:
            filter_params["relevance"] = relevance
        if min_confidence:
            filter_params["min_confidence"] = min_confidence
        if data_quality:
            filter_params["data_quality"] = data_quality
        if stealth_strength:
            filter_params["stealth_strength"] = stealth_strength
        if founder_role:
            filter_params["founder_role"] = founder_role
        if ex_company_tier:
            filter_params["ex_company_tier"] = ex_company_tier
        if geo_region:
            filter_params["geo_region"] = geo_region
        if sector:
            filter_params["sector"] = sector
        if per_page != 25:
            filter_params["per_page"] = per_page
        filter_qs = urlencode(filter_params)
        if sort:
            filter_params["sort"] = sort
        if sort_dir:
            filter_params["sort_dir"] = sort_dir
        filter_qs_with_sort = urlencode(filter_params)

        return templates.TemplateResponse(
            "founders.html",
            {
                "request": request,
                "founders": founders,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "q": q or "",
                "location": location or "",
                "emerged": emerged or "",
                "contacted": contacted or "",
                "viewed": viewed or "",
                "relevance": relevance or "",
                "min_confidence": min_confidence or "",
                "data_quality": data_quality or "",
                "stealth_strength": stealth_strength or "",
                "founder_role": founder_role or "",
                "ex_company_tier": ex_company_tier or "",
                "geo_region": geo_region or "",
                "sector": sector or "",
                "sort": sort or "",
                "sort_dir": sort_dir or "",
                "filter_qs": filter_qs,
                "filter_qs_with_sort": filter_qs_with_sort,
                "locations": locations,
            },
        )
    finally:
        db.close()


@app.post("/founders/{founder_id}/toggle-contacted")
async def toggle_founder_contacted(founder_id: int):
    """Toggle contacted status for a founder."""
    db = get_db()
    try:
        current = db.conn.execute("SELECT contacted FROM stealth_founders WHERE id = ?", (founder_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Founder not found")

        new_status = 0 if current[0] else 1
        contacted_at = datetime.now().isoformat() if new_status else None

        db.conn.execute(
            """
            UPDATE stealth_founders SET contacted = ?, contacted_at = ?
            WHERE id = ?
        """,
            (new_status, contacted_at, founder_id),
        )
        db.conn.commit()

        return {"success": True, "contacted": new_status}
    finally:
        db.close()


@app.post("/founders/{founder_id}/relevance")
async def set_founder_relevance(founder_id: int, request: Request):
    """Set relevance for a founder."""
    db = get_db()
    try:
        body = await request.json()
        relevance = body.get("relevance")
        if relevance not in ("relevant", "irrelevant", None):
            raise HTTPException(status_code=400, detail="Invalid relevance value")

        db.conn.execute("UPDATE stealth_founders SET relevance = ? WHERE id = ?", (relevance, founder_id))
        db.conn.commit()

        return {"success": True, "relevance": relevance}
    finally:
        db.close()


@app.post("/founders/bulk-relevance")
async def bulk_set_founder_relevance(request: Request):
    """Set relevance for multiple founders at once."""
    db = get_db()
    try:
        body = await request.json()
        founder_ids = body.get("founder_ids", [])
        relevance = body.get("relevance")
        if relevance not in ("relevant", "irrelevant", None):
            raise HTTPException(status_code=400, detail="Invalid relevance value")
        if not founder_ids or not isinstance(founder_ids, list):
            raise HTTPException(status_code=400, detail="founder_ids must be a non-empty list")

        placeholders = ",".join(["?"] * len(founder_ids))
        db.conn.execute(
            f"UPDATE stealth_founders SET relevance = ? WHERE id IN ({placeholders})", [relevance] + founder_ids
        )
        db.conn.commit()

        return {"success": True, "updated": len(founder_ids)}
    finally:
        db.close()


@app.post("/founders/{founder_id}/notes")
async def save_founder_notes(founder_id: int, request: Request):
    """Save notes for a founder."""
    db = get_db()
    try:
        body = await request.json()
        notes = body.get("notes", "")

        db.conn.execute("UPDATE stealth_founders SET notes = ? WHERE id = ?", (notes, founder_id))
        db.conn.commit()

        return {"success": True}
    finally:
        db.close()


@app.get("/export/csv")
async def export_csv(
    min_score: int = 1,
    classification: Optional[str] = None,
    limit: int = 10000,
):
    """Export companies to CSV."""
    import csv
    import io

    from fastapi.responses import StreamingResponse

    db = get_db()
    try:
        conditions = ["ai_robotics_score >= ?"]
        params = [min_score]

        if classification:
            conditions.append("startup_classification = ?")
            params.append(classification)

        where_clause = " AND ".join(conditions)

        companies = db.conn.execute(
            f"""
            SELECT * FROM companies
            WHERE {where_clause}
            ORDER BY ai_robotics_score DESC
            LIMIT ?
        """,
            params + [limit],
        ).fetchall()

        # Create CSV
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        columns = [
            "id",
            "name",
            "city",
            "registration_date",
            "capital_amount",
            "ai_robotics_score",
            "startup_score",
            "startup_classification",
            "website",
            "purpose",
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
            headers={"Content-Disposition": "attachment; filename=companies.csv"},
        )
    finally:
        db.close()


@app.get("/export/db")
async def export_db():
    """Download the raw SQLite database file."""
    from fastapi.responses import FileResponse

    db_path = os.path.abspath(DB_PATH)
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Database file not found")

    return FileResponse(
        db_path,
        media_type="application/x-sqlite3",
        headers={"Content-Disposition": "attachment; filename=handelsregister.db"},
    )


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
        companies = db.conn.execute(
            """
            SELECT id, name, city, ai_robotics_score, startup_classification
            FROM companies
            WHERE name LIKE ? AND ai_robotics_score >= ?
            ORDER BY ai_robotics_score DESC
            LIMIT ?
        """,
            (f"%{q}%", min_score, limit),
        ).fetchall()
        companies = [dict(row) for row in companies]

        return templates.TemplateResponse(
            "partials/company_list.html",
            {
                "request": request,
                "companies": companies,
            },
        )
    finally:
        db.close()


@app.get("/api/companies/{company_id}/quick-view", response_class=HTMLResponse)
async def company_quick_view(request: Request, company_id: int):
    """Return a compact company detail partial for inline expansion in the companies table."""
    db = get_db()
    try:
        company = db.get_company(company_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")

        # Get officers (limit 3)
        officers = db.get_officers(company_id)[:3] if hasattr(db, "get_officers") else []

        # Get investments
        try:
            investments = db.conn.execute(
                """
                SELECT inv.*, i.canonical_name as investor_name, i.type as investor_type
                FROM investments inv
                JOIN investors i ON inv.investor_id = i.id
                WHERE inv.company_id = ?
                ORDER BY inv.confidence DESC
                LIMIT 5
            """,
                (company_id,),
            ).fetchall()
            investments = [dict(row) for row in investments]
        except:
            investments = []

        # Mark as viewed
        try:
            db.conn.execute(
                """
                UPDATE companies SET viewed = 1, viewed_at = COALESCE(viewed_at, datetime('now'))
                WHERE id = ?
            """,
                (company_id,),
            )
            db.conn.commit()
        except:
            pass

        return templates.TemplateResponse(
            "partials/company_quick_view.html",
            {
                "request": request,
                "company": company,
                "officers": officers,
                "investments": investments,
            },
        )
    finally:
        db.close()


@app.get("/api/stats/refresh", response_class=HTMLResponse)
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


@app.get("/admin/restore-db")
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


# =============================================================================
# Background Stealth Founder Job Scheduler
# =============================================================================

import logging
import threading

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
logger = logging.getLogger(__name__)


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


@app.on_event("startup")
async def startup_event():
    """Start background scheduler on app startup."""
    start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown scheduler on app shutdown."""
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
        scheduler = None


@app.get("/admin/stealth-job", response_class=HTMLResponse)
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


@app.get("/admin/stealth-job/status")
async def admin_stealth_job_status():
    """Get stealth job status (API)."""
    return stealth_job_status


@app.post("/admin/stealth-job/run")
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


@app.post("/admin/stealth-job/schedule")
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


@app.post("/admin/stealth-job/stop")
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


@app.get("/admin/sync-founders", response_class=HTMLResponse)
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


@app.post("/admin/sync-founders")
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


@app.get("/api/news-stats")
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
