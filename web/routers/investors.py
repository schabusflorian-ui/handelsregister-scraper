from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/investors", response_class=HTMLResponse)
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
            name="investors.html",
            request=request,
            context={
                "investors": investors,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
            },
        )
    finally:
        db.close()


@router.get("/investors/{investor_id}", response_class=HTMLResponse)
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
            name="investor_detail.html",
            request=request,
            context={
                "investor": investor,
                "portfolio": portfolio,
                "aliases": aliases,
                "entities": entities,
            },
        )
    finally:
        db.close()
