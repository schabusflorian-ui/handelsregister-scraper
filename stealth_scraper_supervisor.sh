#!/bin/bash
#
# Supervisor script for stealth founder scraper
# Automatically restarts the scraper if it crashes
#
# Usage:
#   ./stealth_scraper_supervisor.sh [--slow|--fast]
#
# To run in background:
#   nohup ./stealth_scraper_supervisor.sh --slow > data/supervisor.log 2>&1 &
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration
PID_FILE="data/stealth_scraper.pid"
SUPERVISOR_PID_FILE="data/supervisor.pid"
LOG_FILE="data/supervisor.log"
RESTART_DELAY=60  # Wait 60 seconds before restart after crash
MAX_RESTARTS_PER_HOUR=5
SCRAPER_ARGS="${@:---slow}"  # Default to --slow mode

# Ensure data directory exists
mkdir -p data

# Save supervisor PID
echo $$ > "$SUPERVISOR_PID_FILE"

# Track restarts
restart_count=0
last_restart_hour=$(date +%H)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

cleanup() {
    log "Supervisor shutting down..."
    if [ -f "$PID_FILE" ]; then
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log "Stopping scraper (PID: $pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 2
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi
    rm -f "$SUPERVISOR_PID_FILE"
    exit 0
}

trap cleanup SIGINT SIGTERM

log "=========================================="
log "STEALTH SCRAPER SUPERVISOR STARTED"
log "=========================================="
log "Scraper args: $SCRAPER_ARGS"
log "Restart delay: ${RESTART_DELAY}s"
log "Max restarts/hour: $MAX_RESTARTS_PER_HOUR"
log "=========================================="

while true; do
    # Check restart limit
    current_hour=$(date +%H)
    if [ "$current_hour" != "$last_restart_hour" ]; then
        restart_count=0
        last_restart_hour=$current_hour
    fi

    if [ $restart_count -ge $MAX_RESTARTS_PER_HOUR ]; then
        log "WARNING: Too many restarts ($restart_count) in the last hour. Waiting 1 hour..."
        sleep 3600
        restart_count=0
        continue
    fi

    # Start the scraper
    log "Starting stealth scraper..."
    python run_stealth_scraper.py $SCRAPER_ARGS &
    scraper_pid=$!
    echo "$scraper_pid" > "$PID_FILE"
    log "Scraper started with PID: $scraper_pid"

    # Wait for scraper to finish (crash or stop)
    wait $scraper_pid
    exit_code=$?

    log "Scraper exited with code: $exit_code"

    # Check if it was a clean shutdown (exit code 0 or interrupted)
    if [ $exit_code -eq 0 ]; then
        log "Scraper stopped cleanly. Not restarting."
        break
    fi

    # Increment restart counter
    restart_count=$((restart_count + 1))
    log "Restart count this hour: $restart_count"

    # Wait before restarting
    log "Waiting ${RESTART_DELAY}s before restart..."
    sleep $RESTART_DELAY
done

cleanup
