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

# Start scheduler in background (Handelsregister scraping)
echo "Starting scheduler (background)..."
python3 -m scheduler.main \
    --db "$DB_PATH" \
    --run-now \
    --verbose &
SCHEDULER_PID=$!
echo "Scheduler started with PID: $SCHEDULER_PID"

# Stealth scraper: search engines block cloud IPs, run locally instead:
#   caffeinate -i python3 run_stealth.py --engine curl --delay 90

# Start web UI in foreground (this is the main process)
echo "Starting web UI on port ${PORT:-8000}..."
exec python3 -m uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --log-level info
