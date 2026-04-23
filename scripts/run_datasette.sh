#!/usr/bin/env bash
# Launch a read-only Datasette instance for the idea-discovery DB.
# Opens a web UI at http://localhost:8001 + a JSON API on the same URLs
# with `.json` appended (e.g. /handelsregister/v_launch_candidates_loose.json).
#
# Re-run scheduler.jobs.idea_gap_queries --setup-views first if you've
# recently changed the view definitions; otherwise Datasette sees the old ones.

set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${DATASETTE_PORT:-8001}"

# Refresh the SQL views (cheap, idempotent) so Datasette sees the latest.
python3 -m scheduler.jobs.idea_gap_queries --setup-views >/dev/null

# Rebuild FTS tables if triggers missing OR data count mismatches — safe to
# run on every launch; ~1 second total.
python3 -m scheduler.jobs.idea_fts_setup_job 2>&1 | grep -E "INFO|ERROR" | sed 's/^/  /'

echo "Datasette starting on http://localhost:${PORT}"
echo "  - browseable:  http://localhost:${PORT}/handelsregister"
echo "  - JSON API:    append .json to any URL (e.g. .../v_launch_candidates_loose.json)"
echo "  - stats:       http://localhost:${PORT}/handelsregister/stats"
echo "  - launch list: http://localhost:${PORT}/handelsregister/launch_candidates"
echo "  - gap finder:  http://localhost:${PORT}/handelsregister/empty_cells"
echo
exec python3 -m datasette handelsregister.db \
    --host 127.0.0.1 \
    --port "$PORT" \
    --metadata datasette/metadata.json \
    --setting sql_time_limit_ms 10000 \
    --setting default_page_size 50 \
    --setting max_returned_rows 2000 \
    --setting allow_download off
