from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/companies", response_class=HTMLResponse)
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
            name="companies.html",
            request=request,
            context={
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


@router.get("/companies/{company_id}", response_class=HTMLResponse)
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
            name="company_detail.html",
            request=request,
            context={
                "company": company,
                "officers": officers,
                "capital_events": capital_events,
                "investments": investments,
                "announcements": announcements,
            },
        )
    finally:
        db.close()


@router.post("/companies/{company_id}/toggle-contacted")
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


@router.post("/companies/{company_id}/relevance")
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


@router.post("/companies/bulk-relevance")
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


@router.post("/companies/{company_id}/notes")
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


@router.get("/api/companies/search", response_class=HTMLResponse)
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
            name="partials/company_list.html",
            request=request,
            context={
                "companies": companies,
            },
        )
    finally:
        db.close()


@router.get("/api/companies/{company_id}/quick-view", response_class=HTMLResponse)
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
            name="partials/company_quick_view.html",
            request=request,
            context={
                "company": company,
                "officers": officers,
                "investments": investments,
            },
        )
    finally:
        db.close()
