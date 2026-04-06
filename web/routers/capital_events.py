from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/capital-events", response_class=HTMLResponse)
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
            name="capital_events.html",
            request=request,
            context={
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
