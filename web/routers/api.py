from fastapi import APIRouter, HTTPException, Request

from web.dependencies import get_db

router = APIRouter()


@router.get("/api/filter-presets")
async def list_filter_presets():
    """List all saved filter presets."""
    db = get_db()
    try:
        presets = db.conn.execute("SELECT * FROM filter_presets ORDER BY name").fetchall()
        return [dict(row) for row in presets]
    finally:
        db.close()


@router.post("/api/filter-presets")
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


@router.delete("/api/filter-presets/{preset_id}")
async def delete_filter_preset(preset_id: int):
    """Delete a saved filter preset."""
    db = get_db()
    try:
        db.conn.execute("DELETE FROM filter_presets WHERE id = ?", (preset_id,))
        db.conn.commit()
        return {"success": True}
    finally:
        db.close()
