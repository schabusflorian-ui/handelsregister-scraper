import os
from typing import Optional

from fastapi import APIRouter, HTTPException

from web.dependencies import DB_PATH, get_db

router = APIRouter()


@router.get("/export/csv")
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


@router.get("/export/db")
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
