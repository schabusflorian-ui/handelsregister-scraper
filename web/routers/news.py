from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from web.dependencies import get_db, templates

router = APIRouter()


@router.get("/news", response_class=HTMLResponse)
async def news_page(request: Request, alert_type: str = "", page: int = 1, per_page: int = 25):
    """News alerts and articles page."""
    db = get_db()
    try:
        offset = (page - 1) * per_page

        # Get news alerts with company info
        if alert_type:
            alerts = db.conn.execute(
                """
                SELECT na.*, c.name as company_name, c.city, c.ai_robotics_score,
                       c.startup_classification
                FROM news_alerts na
                LEFT JOIN companies c ON na.company_id = c.id
                WHERE na.alert_type = ?
                ORDER BY na.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (alert_type, per_page, offset),
            ).fetchall()
            total = db.conn.execute(
                "SELECT COUNT(*) FROM news_alerts WHERE alert_type = ?", (alert_type,)
            ).fetchone()[0]
        else:
            alerts = db.conn.execute(
                """
                SELECT na.*, c.name as company_name, c.city, c.ai_robotics_score,
                       c.startup_classification
                FROM news_alerts na
                LEFT JOIN companies c ON na.company_id = c.id
                ORDER BY na.created_at DESC
                LIMIT ? OFFSET ?
                """,
                (per_page, offset),
            ).fetchall()
            total = db.conn.execute("SELECT COUNT(*) FROM news_alerts").fetchone()[0]

        alerts = [dict(row) for row in alerts]
        total_pages = (total + per_page - 1) // per_page

        # Get summary stats
        stats = {}
        try:
            stats["total_alerts"] = db.conn.execute("SELECT COUNT(*) FROM news_alerts").fetchone()[0]
            stats["funding_alerts"] = db.conn.execute(
                "SELECT COUNT(*) FROM news_alerts WHERE alert_type = 'funding'"
            ).fetchone()[0]
            stats["early_stage_alerts"] = db.conn.execute(
                "SELECT COUNT(*) FROM news_alerts WHERE alert_type = 'early_stage'"
            ).fetchone()[0]
            stats["total_articles"] = db.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
            stats["articles_last_7d"] = db.conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE fetched_at >= datetime('now', '-7 days')"
            ).fetchone()[0]

            # Top sources
            stats["top_sources"] = [
                dict(row)
                for row in db.conn.execute(
                    """
                    SELECT source, COUNT(*) as cnt
                    FROM news_articles
                    WHERE fetched_at >= datetime('now', '-30 days')
                    GROUP BY source ORDER BY cnt DESC LIMIT 10
                    """
                ).fetchall()
            ]

            # Recent articles (for reference)
            stats["recent_articles"] = [
                dict(row)
                for row in db.conn.execute(
                    """
                    SELECT url, title, source, published_date,
                           is_funding_related, is_ai_related, is_early_stage_related
                    FROM news_articles
                    ORDER BY fetched_at DESC LIMIT 20
                    """
                ).fetchall()
            ]
        except Exception:
            pass

        return templates.TemplateResponse(
            "news.html",
            {
                "request": request,
                "alerts": alerts,
                "stats": stats,
                "alert_type": alert_type,
                "page": page,
                "total_pages": total_pages,
                "total": total,
            },
        )
    finally:
        db.close()
