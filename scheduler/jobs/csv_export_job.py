"""
CSV Export Job - Daily export of discovered companies to CSV files.

Exports AI/robotics companies to CSV files stored in the data directory.
No external API credentials required.
"""

import csv
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class CSVExportJob:
    """
    Export discovered companies to CSV files.

    Creates:
    - all_companies.csv: All AI/Robotics companies
    - new_companies.csv: Companies discovered in last 7 days
    - statistics.csv: Summary statistics
    """

    def __init__(self, db, export_dir: str = "/data/exports"):
        """
        Initialize export job.

        Args:
            db: Database instance
            export_dir: Directory to write CSV files
        """
        self.db = db
        self.export_dir = export_dir

    def _ensure_export_dir(self):
        """Create export directory if it doesn't exist."""
        os.makedirs(self.export_dir, exist_ok=True)
        logger.info(f"Export directory: {self.export_dir}")

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
        rows = conn.execute("""
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
        """, (cutoff,)).fetchall()

        return [dict(row) for row in rows]

    def _get_investments(self) -> List[Dict]:
        """Get all detected investments with company and investor details."""
        conn = self.db._get_connection()
        rows = conn.execute("""
            SELECT
                c.name as company_name,
                c.register_number,
                c.city,
                c.ai_robotics_score,
                c.startup_classification,
                i.canonical_name as investor_name,
                i.type as investor_type,
                i.headquarters_city as investor_hq,
                inv.round_type,
                inv.amount,
                inv.currency,
                inv.investment_date,
                inv.detection_source,
                inv.confidence,
                inv.detected_at,
                inv.notes
            FROM investments inv
            JOIN companies c ON inv.company_id = c.id
            JOIN investors i ON inv.investor_id = i.id
            ORDER BY inv.confidence DESC, inv.detected_at DESC
        """).fetchall()

        return [dict(row) for row in rows]

    def _get_investor_portfolio(self) -> List[Dict]:
        """Get summary of companies per investor (for finding new relevant companies)."""
        conn = self.db._get_connection()
        rows = conn.execute("""
            SELECT
                i.canonical_name as investor_name,
                i.type as investor_type,
                i.headquarters_city as investor_hq,
                COUNT(DISTINCT inv.company_id) as portfolio_count,
                GROUP_CONCAT(DISTINCT c.name, ' | ') as companies,
                AVG(inv.confidence) as avg_confidence,
                MAX(inv.detected_at) as last_detection
            FROM investors i
            JOIN investments inv ON i.id = inv.investor_id
            JOIN companies c ON inv.company_id = c.id
            GROUP BY i.id
            ORDER BY portfolio_count DESC, avg_confidence DESC
        """).fetchall()

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
            'total_companies': stats.get('total_companies', 0),
            'ai_robotics_count': stats.get('ai_robotics_count', 0),
            'score_distribution': [dict(r) for r in score_dist],
            'classification_distribution': [dict(r) for r in class_dist],
            'top_cities': [dict(r) for r in top_cities],
            'last_updated': datetime.utcnow().isoformat(),
        }

    def _write_all_companies_csv(self, companies: List[Dict]) -> str:
        """Write all companies to CSV."""
        filepath = os.path.join(self.export_dir, 'all_companies.csv')

        fieldnames = [
            'name', 'register_number', 'register_court', 'register_type',
            'city', 'state', 'legal_form',
            'ai_robotics_score', 'tech_categories',
            'startup_score', 'startup_classification',
            'capital_amount', 'current_status',
            'registration_date', 'discovered_at',
            'business_purpose'
        ]

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for company in companies:
                # Truncate business purpose
                if company.get('business_purpose'):
                    company['business_purpose'] = company['business_purpose'][:500]
                writer.writerow(company)

        logger.info(f"Wrote {len(companies)} companies to {filepath}")
        return filepath

    def _write_new_companies_csv(self, companies: List[Dict]) -> str:
        """Write new companies to CSV."""
        filepath = os.path.join(self.export_dir, 'new_companies.csv')

        fieldnames = [
            'name', 'register_number', 'register_court',
            'city', 'state', 'legal_form',
            'ai_robotics_score', 'tech_categories',
            'startup_classification', 'capital_amount',
            'discovered_at'
        ]

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(companies)

        logger.info(f"Wrote {len(companies)} new companies to {filepath}")
        return filepath

    def _write_statistics_csv(self, stats: Dict) -> str:
        """Write statistics to CSV."""
        filepath = os.path.join(self.export_dir, 'statistics.csv')

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)

            writer.writerow(['Handelsregister AI/Robotics Companies - Statistics'])
            writer.writerow(['Last Updated', stats['last_updated']])
            writer.writerow([])

            writer.writerow(['Summary'])
            writer.writerow(['Total Companies in Database', stats['total_companies']])
            writer.writerow(['AI/Robotics Companies', stats['ai_robotics_count']])
            writer.writerow([])

            writer.writerow(['Score Distribution'])
            writer.writerow(['AI Score', 'Count'])
            for item in stats['score_distribution']:
                writer.writerow([item['ai_robotics_score'], item['count']])
            writer.writerow([])

            writer.writerow(['Classification Distribution'])
            writer.writerow(['Classification', 'Count'])
            for item in stats['classification_distribution']:
                writer.writerow([item['startup_classification'] or 'Unknown', item['count']])
            writer.writerow([])

            writer.writerow(['Top Cities'])
            writer.writerow(['City', 'Count'])
            for item in stats['top_cities']:
                writer.writerow([item['city'], item['count']])

        logger.info(f"Wrote statistics to {filepath}")
        return filepath

    def _write_investments_csv(self, investments: List[Dict]) -> str:
        """Write detected investments to CSV."""
        filepath = os.path.join(self.export_dir, 'investments.csv')

        fieldnames = [
            'company_name', 'register_number', 'city',
            'ai_robotics_score', 'startup_classification',
            'investor_name', 'investor_type', 'investor_hq',
            'round_type', 'amount', 'currency',
            'investment_date', 'detection_source', 'confidence',
            'detected_at', 'notes'
        ]

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(investments)

        logger.info(f"Wrote {len(investments)} investments to {filepath}")
        return filepath

    def _write_investor_portfolio_csv(self, portfolios: List[Dict]) -> str:
        """Write investor portfolio summary to CSV."""
        filepath = os.path.join(self.export_dir, 'investor_portfolios.csv')

        fieldnames = [
            'investor_name', 'investor_type', 'investor_hq',
            'portfolio_count', 'companies', 'avg_confidence', 'last_detection'
        ]

        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for portfolio in portfolios:
                # Truncate companies list if too long
                if portfolio.get('companies') and len(portfolio['companies']) > 500:
                    portfolio['companies'] = portfolio['companies'][:500] + '...'
                writer.writerow(portfolio)

        logger.info(f"Wrote {len(portfolios)} investor portfolios to {filepath}")
        return filepath

    def run(self) -> Dict[str, Any]:
        """
        Run the export job.

        Returns:
            Statistics about the export
        """
        try:
            self._ensure_export_dir()

            # Get data
            all_companies = self._get_all_companies()
            new_companies = self._get_new_companies(days=7)
            stats = self._get_statistics()
            investments = self._get_investments()
            portfolios = self._get_investor_portfolio()

            # Write CSVs
            all_path = self._write_all_companies_csv(all_companies)
            new_path = self._write_new_companies_csv(new_companies)
            stats_path = self._write_statistics_csv(stats)
            investments_path = self._write_investments_csv(investments)
            portfolios_path = self._write_investor_portfolio_csv(portfolios)

            logger.info(
                "CSV export complete: %d total companies, %d new, %d investments",
                len(all_companies), len(new_companies), len(investments)
            )

            return {
                'status': 'success',
                'total_exported': len(all_companies),
                'new_companies': len(new_companies),
                'investments_exported': len(investments),
                'investors_with_portfolio': len(portfolios),
                'files': {
                    'all_companies': all_path,
                    'new_companies': new_path,
                    'statistics': stats_path,
                    'investments': investments_path,
                    'investor_portfolios': portfolios_path,
                },
                'export_dir': self.export_dir,
            }

        except Exception as e:
            logger.exception("CSV export failed: %s", e)
            return {'status': 'error', 'error': str(e)}


def run_csv_export(db_path: str, export_dir: str = "/data/exports") -> Dict[str, Any]:
    """Convenience function to run export."""
    from persistence.database import Database

    db = Database(db_path)
    try:
        job = CSVExportJob(db=db, export_dir=export_dir)
        return job.run()
    finally:
        db.close()
