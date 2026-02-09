#!/bin/bash
# Startup script for Railway deployment
# Runs both web UI and scheduler in parallel

# Don't exit on error - we want web UI to run even if scheduler fails
set +e

echo "=== Handelsregister Scraper - Railway Deployment ==="
echo "Database: ${DATABASE_PATH:-/data/handelsregister.db}"
echo "Port: ${PORT:-8000}"
echo "Working directory: $(pwd)"

# Export database path for web app
export DB_PATH="${DATABASE_PATH:-/data/handelsregister.db}"

# Ensure data directory exists
mkdir -p /data

# Start scheduler in background (non-LinkedIn jobs only)
echo "Starting scheduler (background)..."
python3 -m scheduler.main \
    --db "$DB_PATH" \
    --verbose \
    2>&1 | sed 's/^/[scheduler] /' &
SCHEDULER_PID=$!
echo "Scheduler started with PID: $SCHEDULER_PID"

# Give scheduler time to initialize
sleep 2

# Check if scheduler is still running
if kill -0 $SCHEDULER_PID 2>/dev/null; then
    echo "Scheduler is running"
else
    echo "WARNING: Scheduler may have failed to start, continuing with web UI only"
fi

# Start web UI in foreground (this is the main process)
echo "Starting web UI on port ${PORT:-8000}..."
exec uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --log-level info
