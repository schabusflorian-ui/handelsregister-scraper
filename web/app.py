"""
Handelsregister Scraper - Web UI

A simple FastAPI-based web interface for browsing and managing
the Handelsregister startup discovery database.

Run with: python -m web.app
Or: uvicorn web.app:app --reload
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from web.dependencies import WEB_DIR, get_db
from web.routers import admin, api, capital_events, companies, dashboard, export, founders, ideas, investors, jobs, news

app = FastAPI(title="Handelsregister Scraper", description="Startup Discovery Platform")

# Static files (if any local static files exist)
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Include routers
app.include_router(dashboard.router)
app.include_router(companies.router)
app.include_router(ideas.router)
app.include_router(founders.router)
app.include_router(investors.router)
app.include_router(capital_events.router)
app.include_router(news.router)
app.include_router(jobs.router)
app.include_router(admin.router)
app.include_router(api.router)
app.include_router(export.router)


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


@app.on_event("startup")
async def startup_event():
    """Start background scheduler on app startup."""
    admin.start_scheduler()


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown scheduler on app shutdown."""
    if admin.scheduler:
        admin.scheduler.shutdown(wait=False)
        admin.scheduler = None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
