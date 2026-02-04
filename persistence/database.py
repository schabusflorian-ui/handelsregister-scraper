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
                capital_amount REAL,
                capital_currency TEXT DEFAULT 'EUR',
                ai_robotics_score INTEGER DEFAULT 0,
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

        self.conn.commit()

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
                capital_amount, capital_currency, ai_robotics_score,
                matched_keywords, tech_categories, startup_score, startup_classification,
                source, first_seen_date, last_updated, enrichment_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            company_number, native_company_number, name, legal_form,
            current_status, registry_court, registry_type, registration_date,
            street, postal_code, city, state, purpose, website,
            capital_amount, capital_currency, ai_robotics_score,
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

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
