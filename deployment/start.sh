#!/bin/bash
# Startup script for Railway deployment
# Runs both web UI and scheduler in parallel

set -e

echo "=== Handelsregister Scraper - Railway Deployment ==="
echo "Database: ${DATABASE_PATH:-/data/handelsregister.db}"
echo "Port: ${PORT:-8000}"

# Export database path for web app
export DB_PATH="${DATABASE_PATH:-/data/handelsregister.db}"

# Start scheduler in background (non-LinkedIn jobs only)
echo "Starting scheduler (background)..."
python3 -m scheduler.main \
    --db "$DB_PATH" \
    --verbose \
    2>&1 | sed 's/^/[scheduler] /' &
SCHEDULER_PID=$!

# Give scheduler time to initialize
sleep 2

# Start web UI in foreground
echo "Starting web UI on port ${PORT:-8000}..."
exec uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --log-level info
