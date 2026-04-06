from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/stealth-founders")
async def stealth_founders_redirect():
    """Redirect to founders page."""
    return RedirectResponse(url="/founders", status_code=302)


@router.get("/founders", response_class=HTMLResponse)
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
            name="founders.html",
            request=request,
            context={
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


@router.post("/founders/{founder_id}/toggle-contacted")
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


@router.post("/founders/{founder_id}/relevance")
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


@router.post("/founders/bulk-relevance")
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


@router.post("/founders/{founder_id}/notes")
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
