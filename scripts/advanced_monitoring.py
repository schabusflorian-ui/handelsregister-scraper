"""
Advanced Example: Database Integration & Change Monitoring

This example shows how to:
1. Store scraping results in SQLite database
2. Track changes over time (new companies, capital raises)
3. Send notifications for new matches
4. Generate reports
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Dict, List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.legacy_scraper import HandelsregisterAIScraper


class StartupDatabase:
    """Database for storing and tracking startup data"""

    def __init__(self, db_path: str = "startups.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.create_tables()

    def create_tables(self):
        """Create database tables"""
        cursor = self.conn.cursor()

        # Companies table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                entity_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT,
                purpose TEXT,
                registration_date TEXT,
                capital_amount REAL,
                capital_currency TEXT,
                website TEXT,
                address_city TEXT,
                address_full TEXT,
                ai_robotics_score INTEGER,
                first_seen_date TEXT,
                last_updated TEXT
            )
        """)

        # Capital raises table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS capital_raises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT,
                date TEXT,
                type TEXT,
                text TEXT,
                detected_date TEXT,
                FOREIGN KEY (entity_id) REFERENCES companies(entity_id)
            )
        """)

        # Management table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS management (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id TEXT,
                person_name TEXT,
                role TEXT,
                start_date TEXT,
                is_current BOOLEAN,
                FOREIGN KEY (entity_id) REFERENCES companies(entity_id)
            )
        """)

        # Scrape history table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scrape_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scrape_date TEXT,
                companies_found INTEGER,
                new_companies INTEGER,
                new_capital_raises INTEGER,
                keywords_used TEXT
            )
        """)

        self.conn.commit()

    def upsert_company(self, company_data: Dict) -> bool:
        """
        Insert or update company data

        Returns:
            True if this is a new company, False if updating existing
        """
        cursor = self.conn.cursor()

        # Check if company exists
        cursor.execute("SELECT entity_id FROM companies WHERE entity_id = ?", (company_data["entity_id"],))
        is_new = cursor.fetchone() is None

        # Prepare data
        now = datetime.now().isoformat()
        first_seen = now if is_new else None

        cursor.execute(
            """
            INSERT OR REPLACE INTO companies (
                entity_id, name, status, purpose, registration_date,
                capital_amount, capital_currency, website, address_city,
                address_full, ai_robotics_score, first_seen_date, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 
                     COALESCE(?, (SELECT first_seen_date FROM companies WHERE entity_id = ?)), 
                     ?)
        """,
            (
                company_data["entity_id"],
                company_data["name"],
                company_data["status"],
                company_data["purpose"],
                company_data["registration_date"],
                company_data["capital"].get("amount"),
                company_data["capital"].get("currency"),
                company_data.get("website"),
                company_data.get("address", {}).get("city"),
                self._format_address(company_data.get("address", {})),
                company_data["ai_robotics_score"],
                first_seen,
                company_data["entity_id"],  # For COALESCE
                now,
            ),
        )

        self.conn.commit()
        return is_new

    def add_capital_raises(self, entity_id: str, raises: List[Dict]) -> int:
        """
        Add capital raises for a company

        Returns:
            Number of new capital raises added
        """
        cursor = self.conn.cursor()
        new_count = 0
        now = datetime.now().isoformat()

        for raise_event in raises:
            # Check if this raise already exists
            cursor.execute(
                """
                SELECT id FROM capital_raises 
                WHERE entity_id = ? AND date = ? AND text = ?
            """,
                (entity_id, raise_event["date"], raise_event["text"]),
            )

            if cursor.fetchone() is None:
                cursor.execute(
                    """
                    INSERT INTO capital_raises (
                        entity_id, date, type, text, detected_date
                    ) VALUES (?, ?, ?, ?, ?)
                """,
                    (entity_id, raise_event["date"], raise_event["type"], raise_event["text"], now),
                )
                new_count += 1

        self.conn.commit()
        return new_count

    def update_management(self, entity_id: str, management: List[Dict]):
        """Update management information for a company"""
        cursor = self.conn.cursor()

        # Clear existing management
        cursor.execute("DELETE FROM management WHERE entity_id = ?", (entity_id,))

        # Insert current management
        for person in management:
            cursor.execute(
                """
                INSERT INTO management (
                    entity_id, person_name, role, start_date, is_current
                ) VALUES (?, ?, ?, ?, ?)
            """,
                (entity_id, person["name"], person["role"], person.get("start_date"), True),
            )

        self.conn.commit()

    def log_scrape(self, keywords: List[str], companies_found: int, new_companies: int, new_raises: int):
        """Log scraping session"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO scrape_history (
                scrape_date, companies_found, new_companies, 
                new_capital_raises, keywords_used
            ) VALUES (?, ?, ?, ?, ?)
        """,
            (datetime.now().isoformat(), companies_found, new_companies, new_raises, json.dumps(keywords)),
        )
        self.conn.commit()

    def get_new_companies_since(self, date: str) -> List[Dict]:
        """Get companies discovered since a certain date"""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM companies 
            WHERE first_seen_date >= ?
            ORDER BY first_seen_date DESC
        """,
            (date,),
        )

        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_recent_capital_raises(self, days: int = 30) -> List[Dict]:
        """Get capital raises detected in the last N days"""
        cursor = self.conn.cursor()
        cutoff = datetime.now().replace(microsecond=0)

        cursor.execute(
            """
            SELECT c.name, cr.date, cr.type, cr.text, cr.detected_date
            FROM capital_raises cr
            JOIN companies c ON cr.entity_id = c.entity_id
            WHERE cr.detected_date >= datetime('now', '-' || ? || ' days')
            ORDER BY cr.detected_date DESC
        """,
            (days,),
        )

        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def generate_report(self) -> Dict:
        """Generate summary statistics"""
        cursor = self.conn.cursor()

        # Total companies
        cursor.execute("SELECT COUNT(*) FROM companies")
        total_companies = cursor.fetchone()[0]

        # Active companies
        cursor.execute("SELECT COUNT(*) FROM companies WHERE status = 'ACTIVE'")
        active_companies = cursor.fetchone()[0]

        # Total capital raises
        cursor.execute("SELECT COUNT(*) FROM capital_raises")
        total_raises = cursor.fetchone()[0]

        # Companies by city
        cursor.execute("""
            SELECT address_city, COUNT(*) as count
            FROM companies
            WHERE address_city IS NOT NULL
            GROUP BY address_city
            ORDER BY count DESC
            LIMIT 10
        """)
        cities = cursor.fetchall()

        # Recent scrapes
        cursor.execute("""
            SELECT scrape_date, companies_found, new_companies, new_capital_raises
            FROM scrape_history
            ORDER BY scrape_date DESC
            LIMIT 5
        """)
        recent_scrapes = cursor.fetchall()

        return {
            "total_companies": total_companies,
            "active_companies": active_companies,
            "total_capital_raises": total_raises,
            "top_cities": cities,
            "recent_scrapes": recent_scrapes,
        }

    def _format_address(self, address: Dict) -> str:
        """Format address dictionary to string"""
        parts = [
            address.get("street", ""),
            address.get("house_number", ""),
            address.get("zip_code", ""),
            address.get("city", ""),
        ]
        return ", ".join(filter(None, parts))

    def close(self):
        """Close database connection"""
        self.conn.close()


def run_monitoring_scrape():
    """
    Run a monitoring scrape that:
    1. Scrapes for new companies
    2. Updates the database
    3. Tracks new companies and capital raises
    4. Generates notifications
    """
    import os

    # Initialize
    api_key = os.getenv("HANDELSREGISTER_API_KEY")
    if not api_key:
        print("ERROR: Set HANDELSREGISTER_API_KEY environment variable")
        return

    scraper = HandelsregisterAIScraper(api_key=api_key)
    db = StartupDatabase("startups.db")

    print("=" * 70)
    print("Running Monitoring Scrape")
    print("=" * 70)

    # Run scrape
    keywords = ["künstliche intelligenz", "robotik", "machine learning"]
    results = scraper.scrape_ai_robotics_startups(
        keywords=keywords,
        recent_months=24,
        min_relevance_score=1,
    )

    # Process results
    new_companies = 0
    new_raises = 0

    for company in results:
        # Add/update company
        is_new = db.upsert_company(company)
        if is_new:
            new_companies += 1
            print(f"\n🆕 NEW COMPANY: {company['name']}")
            print(f"   Purpose: {(company['purpose'] or '')[:100]}...")

        # Add capital raises
        raises_count = db.add_capital_raises(company["entity_id"], company["capital_raises"])
        new_raises += raises_count
        if raises_count > 0:
            print(f"\n💰 NEW CAPITAL RAISE: {company['name']}")
            print(f"   Count: {raises_count} new event(s)")

        # Update management
        db.update_management(company["entity_id"], company["management"])

    # Log scrape
    db.log_scrape(keywords, len(results), new_companies, new_raises)

    # Generate report
    report = db.generate_report()

    print("\n" + "=" * 70)
    print("SCRAPE SUMMARY")
    print("=" * 70)
    print(f"Companies found this run: {len(results)}")
    print(f"New companies: {new_companies}")
    print(f"New capital raises: {new_raises}")
    print("\nDatabase Statistics:")
    print(f"  Total companies: {report['total_companies']}")
    print(f"  Active companies: {report['active_companies']}")
    print(f"  Total capital raises: {report['total_capital_raises']}")
    print("\nTop Cities:")
    for city, count in report["top_cities"][:5]:
        print(f"  {city}: {count}")

    # Close database
    db.close()

    # Send notifications (placeholder)
    if new_companies > 0 or new_raises > 0:
        print(f"\n📧 Notification: {new_companies} new companies, {new_raises} capital raises")
        # TODO: Implement email/Slack notification here


def export_recent_to_csv():
    """Export companies discovered in the last 7 days to CSV"""
    import csv
    from datetime import timedelta

    db = StartupDatabase("startups.db")

    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    companies = db.get_new_companies_since(cutoff)

    if not companies:
        print("No new companies in the last 7 days")
        db.close()
        return

    filename = f"new_startups_{datetime.now().strftime('%Y%m%d')}.csv"

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=companies[0].keys())
        writer.writeheader()
        writer.writerows(companies)

    print(f"✓ Exported {len(companies)} companies to {filename}")
    db.close()


if __name__ == "__main__":
    # Run monitoring scrape
    run_monitoring_scrape()

    # Export recent discoveries
    export_recent_to_csv()
