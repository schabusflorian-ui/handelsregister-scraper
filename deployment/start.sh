#!/bin/bash
# Startup script for Railway deployment
# Runs both web UI and scheduler in parallel

# Don't exit on error - we want web UI to run even if scheduler fails
set +e

echo "=== Handelsregister Scraper - Railway Deployment ==="
echo "Database: ${DATABASE_PATH:-/data/handelsregister.db}"
echo "Port: ${PORT:-8000}"
echo "Working directory: $(pwd)"
echo "Python version: $(python3 --version)"

# Change to app directory (in case we're not there)
cd /app

# Export database path for web app
export DB_PATH="${DATABASE_PATH:-/data/handelsregister.db}"

# Ensure data directory exists
mkdir -p /data

# Test imports before starting
echo "Testing imports..."
python3 -c "from web.app import app; print('Web app import OK')" || echo "Web app import FAILED"

# Start scheduler in background (non-LinkedIn jobs only)
echo "Starting scheduler (background)..."
python3 -m scheduler.main \
    --db "$DB_PATH" \
    --verbose \
    2>&1 | sed 's/^/[scheduler] /' &
SCHEDULER_PID=$!
echo "Scheduler started with PID: $SCHEDULER_PID"

# Don't wait - start web UI immediately
echo "Starting web UI on port ${PORT:-8000}..."
exec python3 -m uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --log-level info
