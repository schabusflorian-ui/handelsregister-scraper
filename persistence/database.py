"""
SQLite database layer for Handelsregister scraper.

Handles all persistence operations including:
- Company storage and retrieval
- Officer/management tracking
- Capital raise events
- Change logging
- Enrichment queue management
"""

import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple
from dataclasses import dataclass
from contextlib import contextmanager


@dataclass
class Company:
    """Company data model."""
    id: Optional[int]
    company_number: str
    native_company_number: Optional[str]
    name: str
    legal_form: Optional[str]
    current_status: Optional[str]
    registry_court: Optional[str]
    registry_type: Optional[str]
    registration_date: Optional[str]
    street: Optional[str]
    postal_code: Optional[str]
    city: Optional[str]
    state: Optional[str]
    purpose: Optional[str]
    website: Optional[str]
    capital_amount: Optional[float]
    capital_currency: Optional[str]
    ai_robotics_score: int
    climate_score: int  # Climate tech relevance score (separate from AI)
    matched_keywords: Optional[str]
    tech_categories: Optional[str]
    startup_score: int  # Startup likelihood score
    startup_classification: Optional[str]  # 'startup', 'tech_company', 'traditional'
    source: str
    first_seen_date: Optional[str]
    last_updated: Optional[str]
    enrichment_status: str


@dataclass
class Officer:
    """Officer/management data model."""
    id: Optional[int]
    company_id: int
    name: str
    role: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    is_current: bool


@dataclass
class CapitalEvent:
    """Capital raise/change event."""
    id: Optional[int]
    company_id: int
    event_type: str
    event_date: Optional[str]
    previous_amount: Optional[float]
    new_amount: Optional[float]
    change_amount: Optional[float]
    currency: str
    publication_text: Optional[str]
    confidence_score: float
    detected_at: Optional[str]


class Database:
    """SQLite database manager for Handelsregister data."""

    def __init__(self, db_path: str = 'handelsregister.db'):
        self.db_path = db_path
        self.conn = None
        self._connect()
        self._create_tables()

    def _connect(self):
        """Establish database connection."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # Enable foreign keys
        self.conn.execute('PRAGMA foreign_keys = ON')

    @contextmanager
    def transaction(self):
        """Context manager for transactions."""
        try:
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _create_tables(self):
        """Create database tables if they don't exist."""
        cursor = self.conn.cursor()

        # Companies table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_number TEXT UNIQUE NOT NULL,
                native_company_number TEXT,
                name TEXT NOT NULL,
                legal_form TEXT,
                current_status TEXT,
                registry_court TEXT,
                registry_type TEXT,
                registration_date TEXT,
                street TEXT,
                postal_code TEXT,
                city TEXT,
                state TEXT,
                purpose TEXT,
                website TEXT,
                website_confidence REAL,
                website_lookup_at TEXT,
                capital_amount REAL,
                capital_currency TEXT DEFAULT 'EUR',
                ai_robotics_score INTEGER DEFAULT 0,
                climate_score INTEGER DEFAULT 0,
                matched_keywords TEXT,
                tech_categories TEXT,
                startup_score INTEGER DEFAULT 0,
                startup_classification TEXT,
                source TEXT,
                first_seen_date TEXT,
                last_updated TEXT,
                enrichment_status TEXT DEFAULT 'pending'
            )
        ''')

        # Officers table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS officers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT,
                start_date TEXT,
                end_date TEXT,
                is_current INTEGER DEFAULT 1,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
        ''')

        # Capital events table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS capital_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_date TEXT,
                previous_amount REAL,
                new_amount REAL,
                change_amount REAL,
                currency TEXT DEFAULT 'EUR',
                publication_text TEXT,
                confidence_score REAL,
                detected_at TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
        ''')

        # Change log table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                change_type TEXT NOT NULL,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                detected_at TEXT,
                notified INTEGER DEFAULT 0,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
        ''')

        # Enrichment queue table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS enrichment_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER UNIQUE NOT NULL,
                priority INTEGER DEFAULT 5,
                reason TEXT,
                queued_at TEXT,
                attempts INTEGER DEFAULT 0,
                last_attempt TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
            )
        ''')

        # Scrape runs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_type TEXT NOT NULL,
                source TEXT,
                started_at TEXT,
                completed_at TEXT,
                records_processed INTEGER DEFAULT 0,
                records_new INTEGER DEFAULT 0,
                records_updated INTEGER DEFAULT 0,
                capital_raises_detected INTEGER DEFAULT 0,
                errors_count INTEGER DEFAULT 0,
                parameters TEXT,
                error_log TEXT
            )
        ''')

        # Announcements table (Registerbekanntmachungen)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                native_company_number TEXT,
                company_name TEXT NOT NULL,
                announcement_type TEXT,
                announcement_date TEXT,
                state TEXT,
                registry_court TEXT,
                registry_type TEXT,
                text TEXT,
                capital_old REAL,
                capital_new REAL,
                source TEXT DEFAULT 'bundesapi',
                fetched_at TEXT,
                processed INTEGER DEFAULT 0,
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL
            )
        ''')

        # Create indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_city ON companies(city)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(current_status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_ai_score ON companies(ai_robotics_score)')
        # climate_score index created in _migrate_companies_table for existing DBs
        try:
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_climate_score ON companies(climate_score)')
        except Exception:
            pass  # Column added via migration later
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_source ON companies(source)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_enrichment ON companies(enrichment_status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_officers_company ON officers(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_capital_events_company ON capital_events(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_change_log_company ON change_log(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_enrichment_queue_priority ON enrichment_queue(priority, queued_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_startup_score ON companies(startup_score)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_startup_class ON companies(startup_classification)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_announcements_company ON announcements(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_announcements_type ON announcements(announcement_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_announcements_date ON announcements(announcement_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_announcements_native ON announcements(native_company_number)')

        # Investors table (VCs, Corporate VCs, Angels, etc.)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS investors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_name TEXT NOT NULL UNIQUE,
                type TEXT NOT NULL,
                website TEXT,
                linkedin TEXT,
                headquarters_city TEXT,
                stage_focus TEXT,
                sector_focus TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Investor aliases for fuzzy matching
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS investor_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                alias_type TEXT,
                UNIQUE(investor_id, alias),
                FOREIGN KEY (investor_id) REFERENCES investors(id) ON DELETE CASCADE
            )
        ''')

        # Investor legal entities (GmbH, KG, etc.)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS investor_legal_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                investor_id INTEGER NOT NULL,
                entity_name TEXT NOT NULL,
                entity_type TEXT,
                register_number TEXT,
                UNIQUE(investor_id, entity_name),
                FOREIGN KEY (investor_id) REFERENCES investors(id) ON DELETE CASCADE
            )
        ''')

        # Investment records (company <-> investor link)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS investments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                investor_id INTEGER NOT NULL,
                round_type TEXT,
                amount REAL,
                currency TEXT DEFAULT 'EUR',
                investment_date TEXT,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                detection_source TEXT,
                confidence REAL DEFAULT 0.5,
                notes TEXT,
                UNIQUE(company_id, investor_id, investment_date),
                FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY (investor_id) REFERENCES investors(id) ON DELETE CASCADE
            )
        ''')

        # Investor indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investor_aliases_alias ON investor_aliases(alias)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investor_aliases_investor ON investor_aliases(investor_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investor_entities_investor ON investor_legal_entities(investor_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investments_company ON investments(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investments_investor ON investments(investor_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_investments_date ON investments(investment_date)')

        # News articles table (RSS feed articles)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                source TEXT,
                published_date TEXT,
                content_hash TEXT,
                is_funding_related INTEGER DEFAULT 0,
                is_ai_related INTEGER DEFAULT 0,
                is_early_stage_related INTEGER DEFAULT 0,
                fetched_at TEXT
            )
        ''')

        # News alerts table (actionable signals from articles)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER,
                article_url TEXT,
                article_title TEXT,
                source TEXT,
                alert_type TEXT,
                amount REAL,
                currency TEXT,
                round_type TEXT,
                investors TEXT,
                early_stage_signals TEXT,
                created_at TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            )
        ''')

        # News indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_articles_url ON news_articles(url)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_articles_source ON news_articles(source)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_articles_early_stage ON news_articles(is_early_stage_related)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_alerts_company ON news_alerts(company_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_alerts_type ON news_alerts(alert_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_alerts_created ON news_alerts(created_at)')

        # Job runs table (scheduler job history)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'running',
                companies_found INTEGER DEFAULT 0,
                companies_new INTEGER DEFAULT 0,
                requests_used INTEGER DEFAULT 0
            )
        ''')

        # Stealth founders table (LinkedIn profiles of potential founders)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stealth_founders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                linkedin_url TEXT UNIQUE,
                name TEXT,
                headline TEXT,
                location TEXT,
                summary TEXT,
                current_company TEXT,
                previous_companies TEXT,

                detection_source TEXT,
                search_query TEXT,
                stealth_signals TEXT,
                confidence_score REAL DEFAULT 0.0,

                first_seen_at TEXT,
                last_checked_at TEXT,
                profile_changed INTEGER DEFAULT 0,

                company_id INTEGER REFERENCES companies(id),
                emerged_at TEXT,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Stealth founder indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stealth_founders_confidence ON stealth_founders(confidence_score DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stealth_founders_location ON stealth_founders(location)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stealth_founders_company ON stealth_founders(company_id)')

        # Founder history table (track profile changes over time)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS founder_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                founder_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                change_type TEXT,
                changed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (founder_id) REFERENCES stealth_founders(id) ON DELETE CASCADE
            )
        ''')

        # Founder history indexes
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_founder_history_founder ON founder_history(founder_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_founder_history_type ON founder_history(change_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_founder_history_date ON founder_history(changed_at)')

        # Saved filter presets
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS filter_presets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                params TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        ''')

        self.conn.commit()

        # Migration: add is_early_stage_related column if table exists without it
        self._migrate_news_tables(cursor)
        self._migrate_companies_table(cursor)
        self._migrate_officers_table(cursor)

    def _migrate_news_tables(self, cursor):
        """Add missing columns to news tables (safe migration)."""
        try:
            # Check if is_early_stage_related column exists
            cursor.execute("PRAGMA table_info(news_articles)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'is_early_stage_related' not in columns and columns:
                cursor.execute(
                    "ALTER TABLE news_articles ADD COLUMN is_early_stage_related INTEGER DEFAULT 0"
                )
                self.conn.commit()
        except Exception:
            pass  # Table might not exist yet or column already added

        try:
            cursor.execute("PRAGMA table_info(news_alerts)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'early_stage_signals' not in columns and columns:
                cursor.execute(
                    "ALTER TABLE news_alerts ADD COLUMN early_stage_signals TEXT"
                )
                self.conn.commit()
        except Exception:
            pass

    def _migrate_companies_table(self, cursor):
        """Add missing columns to companies table (safe migration)."""
        try:
            cursor.execute("PRAGMA table_info(companies)")
            columns = [row[1] for row in cursor.fetchall()]
            for col, col_type in [
                ('website_confidence', 'REAL'),
                ('website_lookup_at', 'TEXT'),
                ('contacted', 'INTEGER DEFAULT 0'),
                ('contacted_at', 'TEXT'),
                ('viewed', 'INTEGER DEFAULT 0'),
                ('viewed_at', 'TEXT'),
                ('notes', 'TEXT'),
                ('relevance', 'TEXT'),
                ('climate_score', 'INTEGER DEFAULT 0'),
            ]:
                if col not in columns and columns:
                    cursor.execute(f"ALTER TABLE companies ADD COLUMN {col} {col_type}")
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_companies_climate_score ON companies(climate_score)')
            self.conn.commit()
        except Exception:
            pass

    def _migrate_officers_table(self, cursor):
        """Add LinkedIn enrichment columns to officers table (safe migration)."""
        try:
            cursor.execute("PRAGMA table_info(officers)")
            columns = [row[1] for row in cursor.fetchall()]
            for col, col_type in [
                ('linkedin_url', 'TEXT'),
                ('linkedin_headline', 'TEXT'),
                ('linkedin_location', 'TEXT'),
                ('linkedin_previous_companies', 'TEXT'),
                ('linkedin_snippet', 'TEXT'),
                ('linkedin_match_confidence', 'REAL DEFAULT 0.0'),
                ('linkedin_enriched_at', 'TEXT'),
                ('linkedin_enrichment_source', 'TEXT'),
            ]:
                if col not in columns and columns:
                    cursor.execute(f"ALTER TABLE officers ADD COLUMN {col} {col_type}")
            self.conn.commit()
        except Exception:
            pass

    # =========================================================================
    # Company Operations
    # =========================================================================

    def insert_company(
        self,
        company_number: str,
        name: str,
        source: str,
        native_company_number: Optional[str] = None,
        legal_form: Optional[str] = None,
        current_status: Optional[str] = None,
        registry_court: Optional[str] = None,
        registry_type: Optional[str] = None,
        registration_date: Optional[str] = None,
        street: Optional[str] = None,
        postal_code: Optional[str] = None,
        city: Optional[str] = None,
        state: Optional[str] = None,
        purpose: Optional[str] = None,
        website: Optional[str] = None,
        capital_amount: Optional[float] = None,
        capital_currency: str = 'EUR',
        ai_robotics_score: int = 0,
        climate_score: int = 0,
        matched_keywords: Optional[List[str]] = None,
        tech_categories: Optional[List[str]] = None,
        startup_score: int = 0,
        startup_classification: Optional[str] = None,
    ) -> int:
        """
        Insert a new company record.

        Returns:
            The ID of the inserted company.
        """
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()

        cursor.execute('''
            INSERT INTO companies (
                company_number, native_company_number, name, legal_form,
                current_status, registry_court, registry_type, registration_date,
                street, postal_code, city, state, purpose, website,
                capital_amount, capital_currency, ai_robotics_score, climate_score,
                matched_keywords, tech_categories, startup_score, startup_classification,
                source, first_seen_date, last_updated, enrichment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            company_number, native_company_number, name, legal_form,
            current_status, registry_court, registry_type, registration_date,
            street, postal_code, city, state, purpose, website,
            capital_amount, capital_currency, ai_robotics_score, climate_score,
            json.dumps(matched_keywords) if matched_keywords else None,
            json.dumps(tech_categories) if tech_categories else None,
            startup_score, startup_classification,
            source, now, now, 'pending'
        ))

        self.conn.commit()
        return cursor.lastrowid

    def upsert_company(
        self,
        company_number: str,
        name: str,
        source: str,
        **kwargs
    ) -> Tuple[int, bool]:
        """
        Insert or update a company record.

        Returns:
            Tuple of (company_id, is_new)
        """
        existing = self.get_company_by_number(company_number)

        if existing:
            # Update existing
            self.update_company(existing['id'], name=name, source=source, **kwargs)
            return existing['id'], False
        else:
            # Insert new
            company_id = self.insert_company(company_number, name, source, **kwargs)
            return company_id, True

    def update_company(self, company_id: int, **kwargs):
        """Update company fields."""
        if not kwargs:
            return

        # Handle JSON fields
        if 'matched_keywords' in kwargs and isinstance(kwargs['matched_keywords'], list):
            kwargs['matched_keywords'] = json.dumps(kwargs['matched_keywords'])
        if 'tech_categories' in kwargs and isinstance(kwargs['tech_categories'], list):
            kwargs['tech_categories'] = json.dumps(kwargs['tech_categories'])

        kwargs['last_updated'] = datetime.now().isoformat()

        set_clause = ', '.join(f'{k} = ?' for k in kwargs.keys())
        values = list(kwargs.values()) + [company_id]

        cursor = self.conn.cursor()
        cursor.execute(f'UPDATE companies SET {set_clause} WHERE id = ?', values)
        self.conn.commit()

    def get_company(self, company_id: int) -> Optional[Dict]:
        """Get company by ID."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM companies WHERE id = ?', (company_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_company_by_number(self, company_number: str) -> Optional[Dict]:
        """Get company by company_number."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM companies WHERE company_number = ?', (company_number,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_company_by_native_number(self, native_number: str) -> Optional[Dict]:
        """Get company by native_company_number."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM companies WHERE native_company_number = ?', (native_number,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def search_companies(
        self,
        name_pattern: Optional[str] = None,
        city: Optional[str] = None,
        min_ai_score: Optional[int] = None,
        status: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """Search companies with filters."""
        conditions = []
        params = []

        if name_pattern:
            conditions.append('name LIKE ?')
            params.append(f'%{name_pattern}%')
        if city:
            conditions.append('city = ?')
            params.append(city)
        if min_ai_score is not None:
            conditions.append('ai_robotics_score >= ?')
            params.append(min_ai_score)
        if status:
            conditions.append('current_status = ?')
            params.append(status)
        if source:
            conditions.append('source = ?')
            params.append(source)

        where_clause = ' AND '.join(conditions) if conditions else '1=1'

        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT * FROM companies
            WHERE {where_clause}
            ORDER BY ai_robotics_score DESC, name
            LIMIT ? OFFSET ?
        ''', params + [limit, offset])

        return [dict(row) for row in cursor.fetchall()]

    def get_companies_for_enrichment(self, limit: int = 10) -> List[Dict]:
        """Get companies from enrichment queue."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT c.* FROM companies c
            JOIN enrichment_queue eq ON c.id = eq.company_id
            ORDER BY eq.priority ASC, eq.queued_at ASC
            LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def count_companies(self, source: Optional[str] = None) -> int:
        """Count companies, optionally filtered by source."""
        cursor = self.conn.cursor()
        if source:
            cursor.execute('SELECT COUNT(*) FROM companies WHERE source = ?', (source,))
        else:
            cursor.execute('SELECT COUNT(*) FROM companies')
        return cursor.fetchone()[0]

    # =========================================================================
    # Officer Operations
    # =========================================================================

    def insert_officer(
        self,
        company_id: int,
        name: str,
        role: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        is_current: bool = True,
    ) -> int:
        """Insert an officer record."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO officers (company_id, name, role, start_date, end_date, is_current)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (company_id, name, role, start_date, end_date, 1 if is_current else 0))
        self.conn.commit()
        return cursor.lastrowid

    def get_officers(self, company_id: int, current_only: bool = False) -> List[Dict]:
        """Get officers for a company."""
        cursor = self.conn.cursor()
        if current_only:
            cursor.execute('SELECT * FROM officers WHERE company_id = ? AND is_current = 1', (company_id,))
        else:
            cursor.execute('SELECT * FROM officers WHERE company_id = ?', (company_id,))
        return [dict(row) for row in cursor.fetchall()]

    def clear_officers(self, company_id: int):
        """Remove all officers for a company."""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM officers WHERE company_id = ?', (company_id,))
        self.conn.commit()

    def officer_exists(self, company_id: int, name: str) -> bool:
        """Check if an officer already exists for this company (case-insensitive)."""
        cursor = self.conn.cursor()
        cursor.execute(
            'SELECT 1 FROM officers WHERE company_id = ? AND LOWER(name) = LOWER(?)',
            (company_id, name),
        )
        return cursor.fetchone() is not None

    def get_officers_for_linkedin_enrichment(self, limit: int = 10) -> List[Dict]:
        """Get current officers not yet enriched via LinkedIn, prioritized by AI score."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT o.*, c.name AS company_name, c.city AS company_city,
                   c.ai_robotics_score, c.startup_score
            FROM officers o
            JOIN companies c ON o.company_id = c.id
            WHERE o.linkedin_enriched_at IS NULL
              AND o.is_current = 1
              AND c.ai_robotics_score >= 1
            ORDER BY c.ai_robotics_score DESC, c.startup_score DESC
            LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]

    def update_officer_linkedin(self, officer_id: int, **kwargs):
        """Update LinkedIn enrichment fields for an officer."""
        if not kwargs:
            return
        kwargs['linkedin_enriched_at'] = datetime.now().isoformat()
        set_clause = ', '.join(f'{k} = ?' for k in kwargs.keys())
        values = list(kwargs.values()) + [officer_id]
        cursor = self.conn.cursor()
        cursor.execute(f'UPDATE officers SET {set_clause} WHERE id = ?', values)
        self.conn.commit()

    # =========================================================================
    # Announcement Processing Helpers
    # =========================================================================

    def get_unprocessed_announcements(
        self,
        announcement_types: Optional[List[str]] = None,
        limit: int = 200,
    ) -> List[Dict]:
        """Get announcements not yet processed for officer extraction."""
        cursor = self.conn.cursor()
        if announcement_types:
            placeholders = ','.join('?' for _ in announcement_types)
            cursor.execute(
                f'SELECT * FROM announcements WHERE processed = 0 AND company_id IS NOT NULL '
                f'AND announcement_type IN ({placeholders}) LIMIT ?',
                announcement_types + [limit],
            )
        else:
            cursor.execute(
                'SELECT * FROM announcements WHERE processed = 0 AND company_id IS NOT NULL LIMIT ?',
                (limit,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def mark_announcement_processed(self, announcement_id: int):
        """Mark an announcement as processed for officer extraction."""
        cursor = self.conn.cursor()
        cursor.execute('UPDATE announcements SET processed = 1 WHERE id = ?', (announcement_id,))
        self.conn.commit()

    # =========================================================================
    # Capital Event Operations
    # =========================================================================

    def insert_capital_event(
        self,
        company_id: int,
        event_type: str,
        event_date: Optional[str] = None,
        previous_amount: Optional[float] = None,
        new_amount: Optional[float] = None,
        change_amount: Optional[float] = None,
        currency: str = 'EUR',
        publication_text: Optional[str] = None,
        confidence_score: float = 0.5,
    ) -> int:
        """Insert a capital event."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO capital_events (
                company_id, event_type, event_date, previous_amount,
                new_amount, change_amount, currency, publication_text,
                confidence_score, detected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            company_id, event_type, event_date, previous_amount,
            new_amount, change_amount, currency, publication_text,
            confidence_score, now
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_capital_events(self, company_id: int) -> List[Dict]:
        """Get capital events for a company."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM capital_events
            WHERE company_id = ?
            ORDER BY event_date DESC, detected_at DESC
        ''', (company_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_capital_events(self, days: int = 30) -> List[Dict]:
        """Get recent capital events with company info."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT ce.*, c.name as company_name, c.city
            FROM capital_events ce
            JOIN companies c ON ce.company_id = c.id
            WHERE ce.detected_at >= datetime('now', '-' || ? || ' days')
            ORDER BY ce.detected_at DESC
        ''', (days,))
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Announcement Operations (Registerbekanntmachungen)
    # =========================================================================

    def insert_announcement(
        self,
        company_name: str,
        native_company_number: Optional[str] = None,
        announcement_type: Optional[str] = None,
        announcement_date: Optional[str] = None,
        state: Optional[str] = None,
        registry_court: Optional[str] = None,
        registry_type: Optional[str] = None,
        text: Optional[str] = None,
        capital_old: Optional[float] = None,
        capital_new: Optional[float] = None,
        company_id: Optional[int] = None,
    ) -> int:
        """Insert an announcement record."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO announcements (
                company_id, native_company_number, company_name, announcement_type,
                announcement_date, state, registry_court, registry_type,
                text, capital_old, capital_new, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            company_id, native_company_number, company_name, announcement_type,
            announcement_date, state, registry_court, registry_type,
            text, capital_old, capital_new, now
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_announcements(
        self,
        announcement_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict]:
        """Get announcements with optional filters."""
        conditions = []
        params = []

        if announcement_type:
            conditions.append('announcement_type = ?')
            params.append(announcement_type)
        if date_from:
            conditions.append('announcement_date >= ?')
            params.append(date_from)
        if date_to:
            conditions.append('announcement_date <= ?')
            params.append(date_to)

        where_clause = ' AND '.join(conditions) if conditions else '1=1'

        cursor = self.conn.cursor()
        cursor.execute(f'''
            SELECT * FROM announcements
            WHERE {where_clause}
            ORDER BY announcement_date DESC, fetched_at DESC
            LIMIT ? OFFSET ?
        ''', params + [limit, offset])

        return [dict(row) for row in cursor.fetchall()]

    def count_announcements(self, announcement_type: Optional[str] = None) -> int:
        """Count announcements, optionally by type."""
        cursor = self.conn.cursor()
        if announcement_type:
            cursor.execute('SELECT COUNT(*) FROM announcements WHERE announcement_type = ?', (announcement_type,))
        else:
            cursor.execute('SELECT COUNT(*) FROM announcements')
        return cursor.fetchone()[0]

    def get_announcement_stats(self) -> Dict[str, int]:
        """Get announcement counts by type."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT announcement_type, COUNT(*) as count
            FROM announcements
            GROUP BY announcement_type
            ORDER BY count DESC
        ''')
        return {row['announcement_type']: row['count'] for row in cursor.fetchall()}

    def link_announcement_to_company(self, announcement_id: int, company_id: int):
        """Link an announcement to a company."""
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE announcements SET company_id = ? WHERE id = ?
        ''', (company_id, announcement_id))
        self.conn.commit()

    # =========================================================================
    # Enrichment Queue Operations
    # =========================================================================

    def add_to_enrichment_queue(
        self,
        company_id: int,
        priority: int = 5,
        reason: str = 'new_company'
    ):
        """Add company to enrichment queue."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO enrichment_queue (company_id, priority, reason, queued_at, attempts)
            VALUES (?, ?, ?, ?, COALESCE((SELECT attempts FROM enrichment_queue WHERE company_id = ?), 0))
        ''', (company_id, priority, reason, now, company_id))
        self.conn.commit()

    def remove_from_enrichment_queue(self, company_id: int):
        """Remove company from enrichment queue."""
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM enrichment_queue WHERE company_id = ?', (company_id,))
        self.conn.commit()

    def mark_enriched(self, company_id: int, success: bool = True):
        """Mark company as enriched."""
        status = 'enriched' if success else 'failed'
        self.update_company(company_id, enrichment_status=status)
        self.remove_from_enrichment_queue(company_id)

    def get_enrichment_queue_size(self) -> int:
        """Get size of enrichment queue."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM enrichment_queue')
        return cursor.fetchone()[0]

    # =========================================================================
    # Change Log Operations
    # =========================================================================

    def log_change(
        self,
        company_id: int,
        change_type: str,
        field_name: Optional[str] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
    ):
        """Log a change to a company."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO change_log (company_id, change_type, field_name, old_value, new_value, detected_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (company_id, change_type, field_name, old_value, new_value, now))
        self.conn.commit()

    def get_unnotified_changes(self) -> List[Dict]:
        """Get changes that haven't been notified."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT cl.*, c.name as company_name
            FROM change_log cl
            JOIN companies c ON cl.company_id = c.id
            WHERE cl.notified = 0
            ORDER BY cl.detected_at DESC
        ''')
        return [dict(row) for row in cursor.fetchall()]

    def mark_changes_notified(self, change_ids: List[int]):
        """Mark changes as notified."""
        if not change_ids:
            return
        placeholders = ','.join('?' * len(change_ids))
        cursor = self.conn.cursor()
        cursor.execute(f'UPDATE change_log SET notified = 1 WHERE id IN ({placeholders})', change_ids)
        self.conn.commit()

    # =========================================================================
    # Scrape Run Operations
    # =========================================================================

    def start_scrape_run(self, run_type: str, source: str, parameters: Optional[Dict] = None) -> int:
        """Start a new scrape run."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO scrape_runs (run_type, source, started_at, parameters)
            VALUES (?, ?, ?, ?)
        ''', (run_type, source, now, json.dumps(parameters) if parameters else None))
        self.conn.commit()
        return cursor.lastrowid

    def complete_scrape_run(
        self,
        run_id: int,
        records_processed: int = 0,
        records_new: int = 0,
        records_updated: int = 0,
        capital_raises_detected: int = 0,
        errors_count: int = 0,
        error_log: Optional[str] = None,
    ):
        """Complete a scrape run with statistics."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE scrape_runs
            SET completed_at = ?, records_processed = ?, records_new = ?,
                records_updated = ?, capital_raises_detected = ?,
                errors_count = ?, error_log = ?
            WHERE id = ?
        ''', (now, records_processed, records_new, records_updated,
              capital_raises_detected, errors_count, error_log, run_id))
        self.conn.commit()

    def get_recent_scrape_runs(self, limit: int = 10) -> List[Dict]:
        """Get recent scrape runs."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM scrape_runs
            ORDER BY started_at DESC
            LIMIT ?
        ''', (limit,))
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Statistics and Reports
    # =========================================================================

    def get_statistics(self) -> Dict[str, Any]:
        """Get overall database statistics."""
        cursor = self.conn.cursor()

        stats = {}

        # Total companies
        cursor.execute('SELECT COUNT(*) FROM companies')
        stats['total_companies'] = cursor.fetchone()[0]

        # Companies by source
        cursor.execute('SELECT source, COUNT(*) FROM companies GROUP BY source')
        stats['companies_by_source'] = dict(cursor.fetchall())

        # Companies by enrichment status
        cursor.execute('SELECT enrichment_status, COUNT(*) FROM companies GROUP BY enrichment_status')
        stats['companies_by_enrichment'] = dict(cursor.fetchall())

        # Total officers
        cursor.execute('SELECT COUNT(*) FROM officers')
        stats['total_officers'] = cursor.fetchone()[0]

        # Total capital events
        cursor.execute('SELECT COUNT(*) FROM capital_events')
        stats['total_capital_events'] = cursor.fetchone()[0]

        # Enrichment queue size
        cursor.execute('SELECT COUNT(*) FROM enrichment_queue')
        stats['enrichment_queue_size'] = cursor.fetchone()[0]

        # Top cities
        cursor.execute('''
            SELECT city, COUNT(*) as count
            FROM companies
            WHERE city IS NOT NULL AND city != ''
            GROUP BY city
            ORDER BY count DESC
            LIMIT 10
        ''')
        stats['top_cities'] = cursor.fetchall()

        # AI score distribution
        cursor.execute('''
            SELECT ai_robotics_score, COUNT(*) as count
            FROM companies
            GROUP BY ai_robotics_score
            ORDER BY ai_robotics_score DESC
        ''')
        stats['ai_score_distribution'] = cursor.fetchall()

        # Startup classification distribution
        cursor.execute('''
            SELECT startup_classification, COUNT(*) as count
            FROM companies
            WHERE startup_classification IS NOT NULL
            GROUP BY startup_classification
            ORDER BY count DESC
        ''')
        stats['startup_classification'] = dict(cursor.fetchall())

        # Startup score distribution
        cursor.execute('''
            SELECT
                CASE
                    WHEN startup_score >= 5 THEN 'high (5+)'
                    WHEN startup_score >= 3 THEN 'medium (3-4)'
                    WHEN startup_score >= 0 THEN 'low (0-2)'
                    ELSE 'negative (<0)'
                END as score_range,
                COUNT(*) as count
            FROM companies
            GROUP BY score_range
            ORDER BY count DESC
        ''')
        stats['startup_score_distribution'] = dict(cursor.fetchall())

        return stats

    # =========================================================================
    # News Operations
    # =========================================================================

    def get_news_alerts(
        self,
        alert_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Get news alerts, optionally filtered by type."""
        cursor = self.conn.cursor()
        if alert_type:
            cursor.execute(
                "SELECT * FROM news_alerts WHERE alert_type = ? ORDER BY created_at DESC LIMIT ?",
                (alert_type, limit)
            )
        else:
            cursor.execute(
                "SELECT * FROM news_alerts ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        return [dict(row) for row in cursor.fetchall()]

    def get_early_stage_articles(self, limit: int = 100) -> List[Dict]:
        """Get articles flagged as early-stage related."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM news_articles WHERE is_early_stage_related = 1 ORDER BY fetched_at DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]

    # =========================================================================
    # Stealth Founder Operations
    # =========================================================================

    def get_stealth_founder(self, founder_id: int) -> Optional[Dict]:
        """Get stealth founder by ID."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM stealth_founders WHERE id = ?', (founder_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_stealth_founder_by_url(self, linkedin_url: str) -> Optional[Dict]:
        """Get stealth founder by LinkedIn URL."""
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM stealth_founders WHERE linkedin_url = ?', (linkedin_url,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_stealth_founders_for_recheck(self, days_since_check: int = 7, limit: int = 100) -> List[Dict]:
        """Get high-confidence founders that need re-checking."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM stealth_founders
            WHERE confidence_score >= 0.4
              AND company_id IS NULL
              AND (last_checked_at IS NULL
                   OR last_checked_at < datetime('now', '-' || ? || ' days'))
            ORDER BY confidence_score DESC, last_checked_at ASC
            LIMIT ?
        ''', (days_since_check, limit))
        return [dict(row) for row in cursor.fetchall()]

    def get_unemerged_founders(self, min_confidence: float = 0.3, limit: int = 100) -> List[Dict]:
        """Get stealth founders who haven't been linked to a company yet."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM stealth_founders
            WHERE company_id IS NULL
              AND confidence_score >= ?
            ORDER BY confidence_score DESC
            LIMIT ?
        ''', (min_confidence, limit))
        return [dict(row) for row in cursor.fetchall()]

    def log_founder_change(
        self,
        founder_id: int,
        field_name: str,
        old_value: Optional[str],
        new_value: Optional[str],
        change_type: Optional[str] = None,
    ) -> int:
        """Log a change to a founder's profile."""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO founder_history (founder_id, field_name, old_value, new_value, change_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (founder_id, field_name, old_value, new_value, change_type))
        self.conn.commit()
        return cursor.lastrowid

    def get_founder_history(self, founder_id: int) -> List[Dict]:
        """Get change history for a founder."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM founder_history
            WHERE founder_id = ?
            ORDER BY changed_at DESC
        ''', (founder_id,))
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_founder_changes(self, days: int = 7, change_type: Optional[str] = None) -> List[Dict]:
        """Get recent founder profile changes with founder info."""
        cursor = self.conn.cursor()
        if change_type:
            cursor.execute('''
                SELECT fh.*, sf.name, sf.linkedin_url, sf.confidence_score
                FROM founder_history fh
                JOIN stealth_founders sf ON fh.founder_id = sf.id
                WHERE fh.changed_at >= datetime('now', '-' || ? || ' days')
                  AND fh.change_type = ?
                ORDER BY fh.changed_at DESC
            ''', (days, change_type))
        else:
            cursor.execute('''
                SELECT fh.*, sf.name, sf.linkedin_url, sf.confidence_score
                FROM founder_history fh
                JOIN stealth_founders sf ON fh.founder_id = sf.id
                WHERE fh.changed_at >= datetime('now', '-' || ? || ' days')
                ORDER BY fh.changed_at DESC
            ''', (days,))
        return [dict(row) for row in cursor.fetchall()]

    def mark_founder_emerged(self, founder_id: int, company_id: int):
        """Mark a stealth founder as emerged (linked to a company)."""
        now = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute('''
            UPDATE stealth_founders
            SET company_id = ?, emerged_at = ?
            WHERE id = ?
        ''', (company_id, now, founder_id))
        self.conn.commit()

        # Log the emergence
        self.log_founder_change(
            founder_id=founder_id,
            field_name='company_id',
            old_value=None,
            new_value=str(company_id),
            change_type='emerged'
        )

    def update_stealth_founder(self, founder_id: int, **kwargs) -> List[Dict]:
        """
        Update stealth founder fields and track changes.

        Returns list of changes detected.
        """
        if not kwargs:
            return []

        # Get current values
        current = self.get_stealth_founder(founder_id)
        if not current:
            return []

        changes = []
        fields_to_update = {}

        # Detect changes
        for field, new_value in kwargs.items():
            old_value = current.get(field)

            # Convert to comparable strings
            old_str = str(old_value) if old_value is not None else None
            new_str = str(new_value) if new_value is not None else None

            if old_str != new_str:
                # Determine change type
                change_type = self._classify_founder_change(field, old_value, new_value)

                changes.append({
                    'field': field,
                    'old_value': old_str,
                    'new_value': new_str,
                    'change_type': change_type,
                })

                fields_to_update[field] = new_value

        # Update fields
        if fields_to_update:
            fields_to_update['last_checked_at'] = datetime.now().isoformat()
            if changes:
                fields_to_update['profile_changed'] = 1

            set_clause = ', '.join(f'{k} = ?' for k in fields_to_update.keys())
            values = list(fields_to_update.values()) + [founder_id]

            cursor = self.conn.cursor()
            cursor.execute(f'UPDATE stealth_founders SET {set_clause} WHERE id = ?', values)
            self.conn.commit()

            # Log changes
            for change in changes:
                self.log_founder_change(
                    founder_id=founder_id,
                    field_name=change['field'],
                    old_value=change['old_value'],
                    new_value=change['new_value'],
                    change_type=change['change_type'],
                )

        return changes

    def _classify_founder_change(
        self,
        field: str,
        old_value: Optional[str],
        new_value: Optional[str]
    ) -> str:
        """Classify the type of change for a founder profile field."""
        # Handle confidence_score separately (it's a float)
        if field == 'confidence_score':
            try:
                old_score = float(old_value) if old_value else 0
                new_score = float(new_value) if new_value else 0
                if new_score > old_score:
                    return 'confidence_increased'
                else:
                    return 'confidence_decreased'
            except (ValueError, TypeError):
                return 'score_change'

        # Convert to lowercase strings for text comparison
        old_lower = (str(old_value) if old_value is not None else '').lower()
        new_lower = (str(new_value) if new_value is not None else '').lower()

        if field == 'headline':
            # Detect stealth transitions
            stealth_words = ['stealth', 'building', 'something new', 'next chapter', 'exploring']
            founder_words = ['founder', 'co-founder', 'ceo', 'gründer']

            went_stealth = any(w in new_lower for w in stealth_words) and not any(w in old_lower for w in stealth_words)
            became_founder = any(w in new_lower for w in founder_words) and not any(w in old_lower for w in founder_words)

            if went_stealth:
                return 'went_stealth'
            elif became_founder:
                return 'became_founder'
            else:
                return 'headline_change'

        elif field == 'current_company':
            if old_value and not new_value:
                return 'left_company'
            elif not old_value and new_value:
                return 'joined_company'
            else:
                return 'company_change'

        elif field == 'location':
            return 'location_change'

        return 'field_change'

    def search_founders_by_name(self, name_pattern: str, limit: int = 10) -> List[Dict]:
        """Search stealth founders by name pattern."""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT * FROM stealth_founders
            WHERE name LIKE ?
            ORDER BY confidence_score DESC
            LIMIT ?
        ''', (f'%{name_pattern}%', limit))
        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
