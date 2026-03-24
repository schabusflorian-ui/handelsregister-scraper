"""
Export functionality for CSV, JSON, and reports.
"""

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class CSVExporter:
    """Export company data to CSV files."""

    DEFAULT_FIELDS = [
        "id",
        "company_number",
        "native_company_number",
        "name",
        "legal_form",
        "current_status",
        "registry_court",
        "registry_type",
        "registration_date",
        "city",
        "state",
        "capital_amount",
        "capital_currency",
        "ai_robotics_score",
        "matched_keywords",
        "tech_categories",
        "website",
        "source",
        "first_seen_date",
    ]

    def __init__(self, output_dir: Path = None):
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_companies(
        self,
        companies: List[Dict],
        filename: Optional[str] = None,
        fields: Optional[List[str]] = None,
    ) -> Path:
        """
        Export companies to CSV.

        Args:
            companies: List of company dicts
            filename: Output filename (default: companies_YYYYMMDD.csv)
            fields: Fields to include (default: DEFAULT_FIELDS)

        Returns:
            Path to the created file
        """
        if not companies:
            raise ValueError("No companies to export")

        fields = fields or self.DEFAULT_FIELDS
        filename = filename or f"companies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = self.output_dir / filename

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()

            for company in companies:
                # Convert JSON fields to strings
                row = company.copy()
                if "matched_keywords" in row and isinstance(row["matched_keywords"], list):
                    row["matched_keywords"] = ", ".join(row["matched_keywords"])
                if "tech_categories" in row and isinstance(row["tech_categories"], list):
                    row["tech_categories"] = ", ".join(row["tech_categories"])

                writer.writerow(row)

        return filepath

    def export_capital_events(
        self,
        events: List[Dict],
        filename: Optional[str] = None,
    ) -> Path:
        """Export capital events to CSV."""
        if not events:
            raise ValueError("No events to export")

        fields = [
            "company_name",
            "event_type",
            "event_date",
            "previous_amount",
            "new_amount",
            "change_amount",
            "currency",
            "confidence_score",
            "detected_at",
            "city",
        ]

        filename = filename or f"capital_events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = self.output_dir / filename

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(events)

        return filepath


class JSONExporter:
    """Export company data to JSON files."""

    def __init__(self, output_dir: Path = None):
        self.output_dir = Path(output_dir) if output_dir else Path(".")
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_companies(
        self,
        companies: List[Dict],
        filename: Optional[str] = None,
        include_officers: bool = False,
        include_capital_events: bool = False,
    ) -> Path:
        """
        Export companies to JSON.

        Args:
            companies: List of company dicts
            filename: Output filename
            include_officers: Include officer data
            include_capital_events: Include capital event data

        Returns:
            Path to the created file
        """
        if not companies:
            raise ValueError("No companies to export")

        filename = filename or f"companies_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = self.output_dir / filename

        # Parse JSON string fields
        for company in companies:
            if "matched_keywords" in company and isinstance(company["matched_keywords"], str):
                try:
                    company["matched_keywords"] = json.loads(company["matched_keywords"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if "tech_categories" in company and isinstance(company["tech_categories"], str):
                try:
                    company["tech_categories"] = json.loads(company["tech_categories"])
                except (json.JSONDecodeError, TypeError):
                    pass

        data = {
            "exported_at": datetime.now().isoformat(),
            "count": len(companies),
            "companies": companies,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        return filepath

    def export_full_database(
        self,
        db: "Database",
        filename: Optional[str] = None,
    ) -> Path:
        """
        Export full database to JSON.

        Args:
            db: Database instance
            filename: Output filename

        Returns:
            Path to the created file
        """
        filename = filename or f"full_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = self.output_dir / filename

        # Get all data
        companies = db.search_companies(limit=1000000)
        stats = db.get_statistics()
        recent_runs = db.get_recent_scrape_runs()
        recent_events = db.get_recent_capital_events(days=365)

        data = {
            "exported_at": datetime.now().isoformat(),
            "statistics": stats,
            "recent_scrape_runs": recent_runs,
            "recent_capital_events": recent_events,
            "companies": companies,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        return filepath


class ReportGenerator:
    """Generate summary reports."""

    def __init__(self, db: "Database"):
        self.db = db

    def generate_summary_report(self) -> str:
        """Generate a text summary report."""
        stats = self.db.get_statistics()
        recent_events = self.db.get_recent_capital_events(days=30)
        recent_runs = self.db.get_recent_scrape_runs(limit=5)

        lines = [
            "=" * 60,
            "HANDELSREGISTER SCRAPER - SUMMARY REPORT",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
            "DATABASE STATISTICS",
            "-" * 40,
            f"Total companies: {stats.get('total_companies', 0):,}",
            "",
            "Companies by source:",
        ]

        for source, count in stats.get("companies_by_source", {}).items():
            lines.append(f"  {source}: {count:,}")

        lines.extend(
            [
                "",
                "Companies by enrichment status:",
            ]
        )

        for status, count in stats.get("companies_by_enrichment", {}).items():
            lines.append(f"  {status}: {count:,}")

        lines.extend(
            [
                "",
                f"Total officers tracked: {stats.get('total_officers', 0):,}",
                f"Total capital events: {stats.get('total_capital_events', 0):,}",
                f"Enrichment queue size: {stats.get('enrichment_queue_size', 0):,}",
                "",
                "TOP CITIES",
                "-" * 40,
            ]
        )

        for city, count in stats.get("top_cities", [])[:10]:
            lines.append(f"  {city}: {count:,}")

        lines.extend(
            [
                "",
                "AI RELEVANCE SCORE DISTRIBUTION",
                "-" * 40,
            ]
        )

        for score, count in stats.get("ai_score_distribution", []):
            lines.append(f"  Score {score}: {count:,} companies")

        lines.extend(
            [
                "",
                f"RECENT CAPITAL EVENTS (last 30 days): {len(recent_events)}",
                "-" * 40,
            ]
        )

        for event in recent_events[:10]:
            lines.append(
                f"  {event.get('company_name', 'Unknown')}: "
                f"{event.get('event_type', 'unknown')} "
                f"({event.get('change_amount', 0):,.0f} EUR)"
            )

        lines.extend(
            [
                "",
                "RECENT SCRAPE RUNS",
                "-" * 40,
            ]
        )

        for run in recent_runs:
            lines.append(
                f"  {run.get('started_at', 'Unknown')[:19]} | "
                f"{run.get('run_type', 'unknown'):15} | "
                f"New: {run.get('records_new', 0):,}"
            )

        lines.extend(
            [
                "",
                "=" * 60,
            ]
        )

        return "\n".join(lines)

    def generate_new_companies_report(self, days: int = 7) -> str:
        """Generate report of newly discovered companies."""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        # Get new companies
        companies = self.db.search_companies(limit=10000)
        new_companies = [c for c in companies if c.get("first_seen_date", "") >= cutoff]

        lines = [
            "=" * 60,
            f"NEW COMPANIES REPORT (last {days} days)",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "=" * 60,
            "",
            f"Total new companies: {len(new_companies)}",
            "",
        ]

        # Group by source
        by_source = {}
        for c in new_companies:
            source = c.get("source", "unknown")
            by_source.setdefault(source, []).append(c)

        for source, companies_list in by_source.items():
            lines.extend(
                [
                    f"\nFrom {source}: {len(companies_list)} companies",
                    "-" * 40,
                ]
            )

            # Sort by AI score
            sorted_companies = sorted(companies_list, key=lambda x: x.get("ai_robotics_score", 0), reverse=True)

            for c in sorted_companies[:20]:
                lines.append(
                    f"  [{c.get('ai_robotics_score', 0)}] {c.get('name', 'Unknown')} ({c.get('city', 'Unknown')})"
                )

            if len(sorted_companies) > 20:
                lines.append(f"  ... and {len(sorted_companies) - 20} more")

        lines.extend(
            [
                "",
                "=" * 60,
            ]
        )

        return "\n".join(lines)
