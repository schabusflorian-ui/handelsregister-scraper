"""
Google Sheets Export Job - Daily export of discovered companies.

Exports AI/robotics companies to a Google Sheet for easy access and sharing.
Requires Google Sheets API credentials (service account JSON).

Environment variables:
- GOOGLE_SHEETS_CREDENTIALS: Base64-encoded service account JSON
- GOOGLE_SHEETS_ID: Target spreadsheet ID
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Check if google-api-python-client is available
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False
    logger.warning("Google Sheets API not available. Install with: pip install google-api-python-client google-auth")


class SheetsExportJob:
    """
    Export discovered companies to Google Sheets.

    Creates/updates a spreadsheet with:
    - Sheet 1: All AI/Robotics companies
    - Sheet 2: New companies (last 7 days)
    - Sheet 3: Statistics summary
    """

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(
        self,
        db,
        credentials_json: Optional[str] = None,
        spreadsheet_id: Optional[str] = None,
    ):
        """
        Initialize export job.

        Args:
            db: Database instance
            credentials_json: Service account JSON (or base64-encoded)
            spreadsheet_id: Target Google Sheets ID
        """
        self.db = db

        # Load credentials from param or environment
        self.credentials_json = credentials_json or os.getenv("GOOGLE_SHEETS_CREDENTIALS")
        self.spreadsheet_id = spreadsheet_id or os.getenv("GOOGLE_SHEETS_ID")

        self.service = None

    def _init_service(self):
        """Initialize Google Sheets API service."""
        if not SHEETS_AVAILABLE:
            raise RuntimeError("Google Sheets API not installed")

        if not self.credentials_json:
            raise ValueError("No Google Sheets credentials provided")

        if not self.spreadsheet_id:
            raise ValueError("No Google Sheets ID provided")

        # Decode credentials if base64-encoded
        try:
            creds_data = json.loads(base64.b64decode(self.credentials_json))
        except:
            # Try as plain JSON
            creds_data = json.loads(self.credentials_json)

        credentials = Credentials.from_service_account_info(creds_data, scopes=self.SCOPES)

        self.service = build("sheets", "v4", credentials=credentials)
        logger.info("Google Sheets API initialized")

    def _get_all_companies(self) -> List[Dict]:
        """Get all AI/robotics companies from database."""
        conn = self.db._get_connection()
        rows = conn.execute("""
            SELECT
                name, register_number, register_court, register_type,
                city, state, legal_form,
                ai_robotics_score, tech_categories,
                startup_score, startup_classification,
                capital_amount, current_status,
                registration_date, discovered_at,
                business_purpose
            FROM companies
            WHERE ai_robotics_score >= 1
            ORDER BY ai_robotics_score DESC, discovered_at DESC
        """).fetchall()

        return [dict(row) for row in rows]

    def _get_new_companies(self, days: int = 7) -> List[Dict]:
        """Get companies discovered in the last N days."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        conn = self.db._get_connection()
        rows = conn.execute(
            """
            SELECT
                name, register_number, register_court,
                city, state, legal_form,
                ai_robotics_score, tech_categories,
                startup_classification,
                capital_amount, discovered_at
            FROM companies
            WHERE ai_robotics_score >= 1
              AND discovered_at >= ?
            ORDER BY discovered_at DESC
        """,
            (cutoff,),
        ).fetchall()

        return [dict(row) for row in rows]

    def _get_statistics(self) -> Dict[str, Any]:
        """Get summary statistics."""
        stats = self.db.get_statistics()

        conn = self.db._get_connection()

        # Companies by AI score
        score_dist = conn.execute("""
            SELECT ai_robotics_score, COUNT(*) as count
            FROM companies
            WHERE ai_robotics_score >= 1
            GROUP BY ai_robotics_score
            ORDER BY ai_robotics_score DESC
        """).fetchall()

        # Companies by classification
        class_dist = conn.execute("""
            SELECT startup_classification, COUNT(*) as count
            FROM companies
            WHERE ai_robotics_score >= 1
            GROUP BY startup_classification
        """).fetchall()

        # Top cities
        top_cities = conn.execute("""
            SELECT city, COUNT(*) as count
            FROM companies
            WHERE ai_robotics_score >= 1 AND city IS NOT NULL
            GROUP BY city
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()

        return {
            "total_companies": stats.get("total_companies", 0),
            "ai_robotics_count": stats.get("ai_robotics_count", 0),
            "score_distribution": [dict(r) for r in score_dist],
            "classification_distribution": [dict(r) for r in class_dist],
            "top_cities": [dict(r) for r in top_cities],
            "last_updated": datetime.utcnow().isoformat(),
        }

    def _prepare_all_companies_sheet(self, companies: List[Dict]) -> List[List]:
        """Prepare data for all companies sheet."""
        headers = [
            "Name",
            "Register Number",
            "Court",
            "Type",
            "City",
            "State",
            "Legal Form",
            "AI Score",
            "Tech Categories",
            "Startup Score",
            "Classification",
            "Capital (€)",
            "Status",
            "Registration Date",
            "Discovered At",
            "Business Purpose",
        ]

        rows = [headers]
        for c in companies:
            rows.append(
                [
                    c.get("name", ""),
                    c.get("register_number", ""),
                    c.get("register_court", ""),
                    c.get("register_type", ""),
                    c.get("city", ""),
                    c.get("state", ""),
                    c.get("legal_form", ""),
                    c.get("ai_robotics_score", 0),
                    c.get("tech_categories", ""),
                    c.get("startup_score", 0),
                    c.get("startup_classification", ""),
                    c.get("capital_amount", ""),
                    c.get("current_status", ""),
                    c.get("registration_date", ""),
                    c.get("discovered_at", ""),
                    (c.get("business_purpose", "") or "")[:500],  # Truncate
                ]
            )

        return rows

    def _prepare_new_companies_sheet(self, companies: List[Dict]) -> List[List]:
        """Prepare data for new companies sheet."""
        headers = [
            "Name",
            "Register Number",
            "Court",
            "City",
            "State",
            "Legal Form",
            "AI Score",
            "Tech Categories",
            "Classification",
            "Capital (€)",
            "Discovered At",
        ]

        rows = [headers]
        for c in companies:
            rows.append(
                [
                    c.get("name", ""),
                    c.get("register_number", ""),
                    c.get("register_court", ""),
                    c.get("city", ""),
                    c.get("state", ""),
                    c.get("legal_form", ""),
                    c.get("ai_robotics_score", 0),
                    c.get("tech_categories", ""),
                    c.get("startup_classification", ""),
                    c.get("capital_amount", ""),
                    c.get("discovered_at", ""),
                ]
            )

        return rows

    def _prepare_stats_sheet(self, stats: Dict) -> List[List]:
        """Prepare data for statistics sheet."""
        rows = [
            ["Handelsregister AI/Robotics Companies - Statistics"],
            ["Last Updated", stats["last_updated"]],
            [],
            ["Summary"],
            ["Total Companies in Database", stats["total_companies"]],
            ["AI/Robotics Companies", stats["ai_robotics_count"]],
            [],
            ["Score Distribution"],
            ["AI Score", "Count"],
        ]

        for item in stats["score_distribution"]:
            rows.append([item["ai_robotics_score"], item["count"]])

        rows.extend(
            [
                [],
                ["Classification Distribution"],
                ["Classification", "Count"],
            ]
        )

        for item in stats["classification_distribution"]:
            rows.append([item["startup_classification"] or "Unknown", item["count"]])

        rows.extend(
            [
                [],
                ["Top Cities"],
                ["City", "Count"],
            ]
        )

        for item in stats["top_cities"]:
            rows.append([item["city"], item["count"]])

        return rows

    def _update_sheet(self, sheet_name: str, data: List[List]):
        """Update a sheet with data."""
        # Clear existing content
        self.service.spreadsheets().values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A:Z",
        ).execute()

        # Write new data
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": data},
        ).execute()

        logger.info(f"Updated sheet '{sheet_name}' with {len(data)} rows")

    def _ensure_sheets_exist(self):
        """Ensure all required sheets exist."""
        # Get existing sheets
        spreadsheet = self.service.spreadsheets().get(spreadsheetId=self.spreadsheet_id).execute()

        existing = {s["properties"]["title"] for s in spreadsheet["sheets"]}
        required = ["All Companies", "New (7 days)", "Statistics"]

        requests = []
        for sheet_name in required:
            if sheet_name not in existing:
                requests.append({"addSheet": {"properties": {"title": sheet_name}}})

        if requests:
            self.service.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
            logger.info(f"Created sheets: {[r['addSheet']['properties']['title'] for r in requests]}")

    def run(self) -> Dict[str, Any]:
        """
        Run the export job.

        Returns:
            Statistics about the export
        """
        if not SHEETS_AVAILABLE:
            logger.warning("Google Sheets export skipped - API not available")
            return {"status": "skipped", "reason": "API not available"}

        if not self.credentials_json or not self.spreadsheet_id:
            logger.warning("Google Sheets export skipped - credentials not configured")
            return {"status": "skipped", "reason": "credentials not configured"}

        try:
            self._init_service()
            self._ensure_sheets_exist()

            # Get data
            all_companies = self._get_all_companies()
            new_companies = self._get_new_companies(days=7)
            stats = self._get_statistics()

            # Update sheets
            self._update_sheet("All Companies", self._prepare_all_companies_sheet(all_companies))
            self._update_sheet("New (7 days)", self._prepare_new_companies_sheet(new_companies))
            self._update_sheet("Statistics", self._prepare_stats_sheet(stats))

            logger.info("Export complete: %d total companies, %d new", len(all_companies), len(new_companies))

            return {
                "status": "success",
                "total_exported": len(all_companies),
                "new_companies": len(new_companies),
                "spreadsheet_id": self.spreadsheet_id,
            }

        except Exception as e:
            logger.exception("Google Sheets export failed: %s", e)
            return {"status": "error", "error": str(e)}


def run_sheets_export(db_path: str) -> Dict[str, Any]:
    """Convenience function to run export."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = SheetsExportJob(db=db)
        return job.run()
    finally:
        db.close()
