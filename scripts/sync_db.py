#!/usr/bin/env python3
"""
Bidirectional database sync between local and Railway.

Pulls: companies + officers from Railway → local (for officer cross-reference)
Pushes: stealth_founders from local → Railway (for web UI visibility)

Usage:
    python scripts/sync_db.py                  # Full bidirectional sync
    python scripts/sync_db.py --pull-only      # Only pull companies/officers
    python scripts/sync_db.py --push-only      # Only push stealth founders
    python scripts/sync_db.py --stats          # Show both DBs side-by-side

Requires: railway CLI installed and linked to project
"""

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

RAILWAY_DB = "/data/handelsregister.db"
LOCAL_DB_DEFAULT = str(Path(__file__).parent.parent / "handelsregister.db")


def railway_ssh_script(script: str, timeout: int = 120) -> str:
    """
    Execute a Python script inside the Railway container.

    Writes script to a temp file, base64-encodes it, and decodes+executes
    inside the container. This avoids all shell quoting issues.
    """
    import base64

    encoded = base64.b64encode(script.encode()).decode()
    # Single command: decode base64 and pipe to python3
    cmd = f"echo {encoded} | base64 -d | python3"
    result = subprocess.run(
        ["railway", "ssh", cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        # railway ssh often puts Python errors in stdout, not stderr
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"railway ssh failed (rc={result.returncode}): {error_msg[:500]}")
    # Check for Python tracebacks in stdout (railway ssh may still return rc=0)
    if result.stdout.strip().startswith("Traceback"):
        raise RuntimeError(f"Python error in container: {result.stdout.strip()[:500]}")
    return result.stdout.strip()


def pull_companies_and_officers(local_db: str, days: int = 30) -> dict:
    """
    Pull recent companies and their officers from Railway to local DB.

    Uses railway ssh to export JSON from the container, then imports locally.
    """
    print(f"\n{'=' * 60}")
    print("PULL: Railway → Local (companies + officers)")
    print(f"{'=' * 60}")

    # Export from Railway as JSON via SSH
    # Use first_seen_date since registration_date is mostly NULL
    # Note: Railway uses 'registry_court' not 'court'
    export_script = f"""
import sqlite3, json, sys
conn = sqlite3.connect("{RAILWAY_DB}")
conn.row_factory = sqlite3.Row
c = conn.cursor()

c.execute('''
    SELECT id, company_number, native_company_number, name, legal_form,
           current_status, street, postal_code, city, state,
           capital_amount, capital_currency, registration_date, registry_court,
           ai_robotics_score, matched_keywords, tech_categories,
           startup_score, startup_classification, source,
           first_seen_date, last_updated
    FROM companies
    WHERE first_seen_date >= date('now', '-{days} days')
''')
companies = [dict(row) for row in c.fetchall()]
company_ids = [co['id'] for co in companies]

officers = []
if company_ids:
    placeholders = ','.join('?' * len(company_ids))
    c.execute(f'SELECT id, company_id, name, role, start_date, end_date, is_current FROM officers WHERE company_id IN ({{placeholders}})', company_ids)
    officers = [dict(row) for row in c.fetchall()]

conn.close()
out = json.dumps({{'companies': companies, 'officers': officers}})
sys.stdout.write(out)
sys.stdout.flush()
"""

    print(f"  Exporting from Railway (last {days} days)...")

    try:
        raw = railway_ssh_script(export_script, timeout=180)
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"companies": 0, "officers": 0, "error": str(e)}

    # Parse the JSON output (last line should be JSON)
    lines = raw.strip().split("\n")
    json_line = lines[-1] if lines else "{}"
    try:
        data = json.loads(json_line)
    except json.JSONDecodeError as e:
        print(f"  ERROR parsing response: {e}")
        print(f"  Raw output (last 500 chars): {raw[-500:]}")
        return {"companies": 0, "officers": 0, "error": str(e)}

    companies = data.get("companies", [])
    officers = data.get("officers", [])
    print(f"  Received {len(companies)} companies, {len(officers)} officers")

    if not companies:
        print("  Nothing to import")
        return {"companies": 0, "officers": 0}

    # Import into local DB
    conn = sqlite3.connect(local_db)
    cursor = conn.cursor()

    # Ensure tables exist (they should from the main app)
    imported_companies = 0
    skipped_companies = 0

    for co in companies:
        try:
            # Check if company already exists locally (by company_number)
            cursor.execute("SELECT id FROM companies WHERE company_number = ?", (co["company_number"],))
            existing = cursor.fetchone()

            if existing:
                # Update with Railway data (Railway has richer data)
                cursor.execute(
                    """
                    UPDATE companies SET
                        name = COALESCE(?, name),
                        city = COALESCE(?, city),
                        ai_robotics_score = COALESCE(?, ai_robotics_score),
                        matched_keywords = COALESCE(?, matched_keywords),
                        startup_score = COALESCE(?, startup_score),
                        first_seen_date = COALESCE(?, first_seen_date),
                        last_updated = ?
                    WHERE company_number = ?
                """,
                    (
                        co.get("name"),
                        co.get("city"),
                        co.get("ai_robotics_score"),
                        co.get("matched_keywords"),
                        co.get("startup_score"),
                        co.get("first_seen_date"),
                        datetime.now().isoformat(),
                        co["company_number"],
                    ),
                )
                skipped_companies += 1
            else:
                cursor.execute(
                    """
                    INSERT INTO companies (
                        company_number, native_company_number, name, legal_form,
                        current_status, street, postal_code, city, state,
                        capital_amount, capital_currency, registration_date, registry_court,
                        ai_robotics_score, matched_keywords, tech_categories,
                        startup_score, startup_classification, source,
                        first_seen_date, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        co.get("company_number"),
                        co.get("native_company_number"),
                        co.get("name"),
                        co.get("legal_form"),
                        co.get("current_status"),
                        co.get("street"),
                        co.get("postal_code"),
                        co.get("city"),
                        co.get("state"),
                        co.get("capital_amount"),
                        co.get("capital_currency"),
                        co.get("registration_date"),
                        co.get("registry_court"),
                        co.get("ai_robotics_score"),
                        co.get("matched_keywords"),
                        co.get("tech_categories"),
                        co.get("startup_score"),
                        co.get("startup_classification"),
                        co.get("source"),
                        co.get("first_seen_date"),
                        co.get("last_updated"),
                    ),
                )
                imported_companies += 1
        except Exception as e:
            print(f"  Error importing company {co.get('name')}: {e}")

    # Build a mapping from Railway company IDs to local company IDs
    railway_to_local = {}
    for co in companies:
        cursor.execute("SELECT id FROM companies WHERE company_number = ?", (co["company_number"],))
        row = cursor.fetchone()
        if row:
            railway_to_local[co["id"]] = row[0]

    # Import officers
    imported_officers = 0
    skipped_officers = 0

    for off in officers:
        railway_company_id = off.get("company_id")
        local_company_id = railway_to_local.get(railway_company_id)

        if not local_company_id:
            continue

        try:
            # Check if officer already exists (by company_id + name + role)
            cursor.execute(
                """
                SELECT id FROM officers
                WHERE company_id = ? AND name = ? AND role = ?
            """,
                (local_company_id, off.get("name"), off.get("role")),
            )

            if cursor.fetchone():
                skipped_officers += 1
                continue

            cursor.execute(
                """
                INSERT INTO officers (company_id, name, role, start_date, end_date, is_current)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    local_company_id,
                    off.get("name"),
                    off.get("role"),
                    off.get("start_date"),
                    off.get("end_date"),
                    off.get("is_current", 1),
                ),
            )
            imported_officers += 1
        except Exception as e:
            print(f"  Error importing officer {off.get('name')}: {e}")

    conn.commit()
    conn.close()

    print(f"  Companies: {imported_companies} new, {skipped_companies} updated")
    print(f"  Officers:  {imported_officers} new, {skipped_officers} existing")

    return {
        "companies": imported_companies,
        "companies_updated": skipped_companies,
        "officers": imported_officers,
    }


def railway_ssh_write_file(data: str, remote_path: str, timeout: int = 60) -> None:
    """
    Write data to a file on the Railway container using multiple SSH calls.

    Splits data into base64 chunks small enough for the command line limit,
    and appends them to the remote file across multiple SSH calls.
    """
    import base64

    encoded = base64.b64encode(data.encode()).decode()
    chunk_size = 60000  # ~60KB per chunk, well under 128KB arg limit

    for i in range(0, len(encoded), chunk_size):
        chunk = encoded[i : i + chunk_size]
        op = ">" if i == 0 else ">>"
        cmd = f"echo -n '{chunk}' {op} /tmp/_sync_data.b64"
        result = subprocess.run(
            ["railway", "ssh", cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Failed to write chunk: {error_msg[:300]}")

    # Decode the base64 file
    result = subprocess.run(
        ["railway", "ssh", f"base64 -d /tmp/_sync_data.b64 > {remote_path} && rm /tmp/_sync_data.b64"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        error_msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Failed to decode data: {error_msg[:300]}")


def push_stealth_founders(local_db: str) -> dict:
    """
    Push stealth founders from local DB to Railway.

    Exports local stealth_founders as JSON, writes to container via temp file,
    then imports via a Python script.
    """
    print(f"\n{'=' * 60}")
    print("PUSH: Local → Railway (stealth_founders)")
    print(f"{'=' * 60}")

    # Export local stealth founders
    conn = sqlite3.connect(local_db)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM stealth_founders")
    founders = [dict(row) for row in cursor.fetchall()]
    conn.close()

    if not founders:
        print("  No stealth founders to push")
        return {"pushed": 0, "skipped": 0}

    print(f"  Exporting {len(founders)} founders from local DB...")

    founders_json = json.dumps(founders)

    # Step 1: Write founders JSON to container as temp file (chunked to avoid arg limit)
    print(f"  Uploading {len(founders_json)} bytes to Railway container...")
    try:
        railway_ssh_write_file(founders_json, "/tmp/_sync_founders.json", timeout=60)
    except Exception as e:
        print(f"  ERROR uploading data: {e}")
        return {"pushed": 0, "skipped": 0, "error": str(e)}

    # Step 2: Run import script that reads from the temp file
    import_script = f"""
import sqlite3, json

with open('/tmp/_sync_founders.json', 'r') as f:
    founders = json.load(f)

conn = sqlite3.connect("{RAILWAY_DB}")
c = conn.cursor()

c.execute('''
    CREATE TABLE IF NOT EXISTS stealth_founders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        linkedin_url TEXT UNIQUE,
        name TEXT, headline TEXT, location TEXT, summary TEXT,
        current_company TEXT, previous_companies TEXT,
        detection_source TEXT, search_query TEXT, stealth_signals TEXT,
        confidence_score REAL DEFAULT 0.0,
        first_seen_at TEXT, last_checked_at TEXT,
        profile_changed INTEGER DEFAULT 0,
        company_id INTEGER REFERENCES companies(id),
        emerged_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
''')

imported = 0
skipped = 0

tag_cols = ['founder_role', 'ex_company_tier', 'ex_companies', 'sector_tags',
            'stealth_strength', 'data_quality', 'geo_region',
            'contacted', 'contacted_at', 'viewed', 'viewed_at', 'notes', 'relevance']

# Add tag columns if missing
for _col in tag_cols:
    try:
        _col_type = 'TEXT'
        if _col in ('contacted', 'viewed'):
            _col_type = 'INTEGER DEFAULT 0'
        c.execute('ALTER TABLE stealth_founders ADD COLUMN ' + _col + ' ' + _col_type)
    except:
        pass

updated = 0

for f in founders:
    url = f.get('linkedin_url')
    if not url:
        continue
    c.execute('SELECT id FROM stealth_founders WHERE linkedin_url = ?', (url,))
    existing = c.fetchone()
    if existing:
        # Update tag columns for existing founders
        tag_values = [f.get(_c) for _c in tag_cols]
        has_tags = any(v is not None for v in tag_values)
        if has_tags:
            set_clause = ', '.join(_c + ' = ?' for _c in tag_cols)
            c.execute('UPDATE stealth_founders SET ' + set_clause + ' WHERE linkedin_url = ?',
                      tag_values + [url])
            updated += 1
        skipped += 1
        continue
    c.execute('''
        INSERT INTO stealth_founders (
            linkedin_url, name, headline, location, summary,
            current_company, previous_companies, detection_source,
            search_query, stealth_signals, confidence_score,
            first_seen_at, last_checked_at, profile_changed,
            company_id, emerged_at, created_at,
            founder_role, ex_company_tier, ex_companies, sector_tags,
            stealth_strength, data_quality, geo_region,
            contacted, contacted_at, viewed, viewed_at, notes, relevance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        f.get('linkedin_url'), f.get('name'), f.get('headline'),
        f.get('location'), f.get('summary'), f.get('current_company'),
        f.get('previous_companies'), f.get('detection_source'),
        f.get('search_query'), f.get('stealth_signals'),
        f.get('confidence_score'), f.get('first_seen_at'),
        f.get('last_checked_at'), f.get('profile_changed', 0),
        f.get('company_id'), f.get('emerged_at'), f.get('created_at'),
        f.get('founder_role'), f.get('ex_company_tier'), f.get('ex_companies'),
        f.get('sector_tags'), f.get('stealth_strength'), f.get('data_quality'),
        f.get('geo_region'), f.get('contacted', 0), f.get('contacted_at'),
        f.get('viewed', 0), f.get('viewed_at'), f.get('notes'), f.get('relevance'),
    ))
    imported += 1

conn.commit()
conn.close()
import os
os.remove('/tmp/_sync_founders.json')
print(imported, skipped, updated)
"""

    try:
        raw = railway_ssh_script(import_script, timeout=180)
        parts = raw.strip().split("\n")[-1].split()
        imported = int(parts[0]) if len(parts) >= 1 else 0
        skipped = int(parts[1]) if len(parts) >= 2 else 0
        updated = int(parts[2]) if len(parts) >= 3 else 0
        print(f"  Pushed {imported} new founders, {updated} updated, {skipped} already existed")
        return {"pushed": imported, "skipped": skipped}
    except Exception as e:
        print(f"  ERROR: {e}")
        return {"pushed": 0, "skipped": 0, "error": str(e)}


def show_stats(local_db: str):
    """Show side-by-side stats for local and Railway DBs."""
    print(f"\n{'=' * 60}")
    print("DATABASE COMPARISON")
    print(f"{'=' * 60}")

    # Local stats
    conn = sqlite3.connect(local_db)
    cursor = conn.cursor()

    local_stats = {}
    for table in ["companies", "officers", "stealth_founders", "announcements"]:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            local_stats[table] = cursor.fetchone()[0]
        except:
            local_stats[table] = "N/A"
    conn.close()

    # Railway stats
    try:
        stats_script = f"""
import sqlite3
conn = sqlite3.connect("{RAILWAY_DB}")
c = conn.cursor()
for t in ['companies', 'officers', 'stealth_founders', 'announcements']:
    try:
        c.execute(f'SELECT COUNT(*) FROM {{t}}')
        print(f'{{t}} {{c.fetchone()[0]}}')
    except:
        print(f'{{t}} 0')
conn.close()
"""
        raw = railway_ssh_script(stats_script, timeout=60)
        railway_stats = {}
        for line in raw.strip().split("\n"):
            parts = line.split()
            if len(parts) == 2:
                railway_stats[parts[0]] = int(parts[1])
    except Exception as e:
        railway_stats = {"error": str(e)}
        print(f"  Could not reach Railway: {e}")

    print(f"\n  {'Table':<25} {'Local':>10} {'Railway':>10} {'Delta':>10}")
    print(f"  {'-' * 55}")
    for table in ["companies", "officers", "stealth_founders", "announcements"]:
        local = local_stats.get(table, 0)
        remote = railway_stats.get(table, "?")
        if isinstance(local, int) and isinstance(remote, int):
            delta = local - remote
            delta_str = f"+{delta}" if delta > 0 else str(delta)
        else:
            delta_str = "-"
        print(f"  {table:<25} {str(local):>10} {str(remote):>10} {delta_str:>10}")

    print()


def sync(local_db: str, pull: bool = True, push: bool = True, days: int = 30):
    """Run bidirectional sync."""
    print(f"\nSync started at {datetime.now().strftime('%H:%M:%S')}")
    print(f"Local DB: {local_db}")

    results = {}

    if pull:
        results["pull"] = pull_companies_and_officers(local_db, days=days)

    if push:
        results["push"] = push_stealth_founders(local_db)

    print(f"\n{'=' * 60}")
    print("SYNC COMPLETE")
    print(f"{'=' * 60}")

    if "pull" in results:
        r = results["pull"]
        print(f"  Pulled: {r.get('companies', 0)} companies, {r.get('officers', 0)} officers")

    if "push" in results:
        r = results["push"]
        print(f"  Pushed: {r.get('pushed', 0)} stealth founders")

    print()
    return results


def main():
    parser = argparse.ArgumentParser(description="Bidirectional DB sync: Railway ↔ Local")
    parser.add_argument("--pull-only", action="store_true", help="Only pull companies/officers from Railway")
    parser.add_argument("--push-only", action="store_true", help="Only push stealth founders to Railway")
    parser.add_argument("--stats", action="store_true", help="Show comparison stats only")
    parser.add_argument("--days", type=int, default=30, help="Pull companies from the last N days (default: 30)")
    parser.add_argument("--db", default=LOCAL_DB_DEFAULT, help="Local database path")

    args = parser.parse_args()

    if args.stats:
        show_stats(args.db)
        return

    pull = not args.push_only
    push = not args.pull_only

    sync(args.db, pull=pull, push=push, days=args.days)


if __name__ == "__main__":
    main()
