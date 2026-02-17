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

# Start stealth scraper in background (founder discovery)
echo "Starting stealth scraper (background)..."
python3 -u run_stealth.py \
    --engine "${STEALTH_ENGINE:-brave}" \
    --delay "${STEALTH_DELAY:-90}" \
    2>&1 &
STEALTH_PID=$!
echo "Stealth scraper started with PID: $STEALTH_PID"

# Start web UI in foreground (this is the main process)
echo "Starting web UI on port ${PORT:-8000}..."
exec python3 -m uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --log-level info
