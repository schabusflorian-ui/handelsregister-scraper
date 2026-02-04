"""
Investor Matcher - Fuzzy matching of investor names against known VCs.

Supports multiple matching strategies:
1. Exact match on legal entity names (confidence: 1.0)
2. Normalized match removing legal suffixes (confidence: 0.95)
3. Fuzzy match using Levenshtein distance (confidence: 0.8)
4. Alias match (confidence: 0.9)
"""

import re
import logging
import yaml
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Set
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Try to import rapidfuzz, fall back to simple matching
try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not installed, using basic matching only")


@dataclass
class InvestorMatch:
    """Result of an investor match."""
    investor_id: int
    investor_name: str
    matched_text: str
    match_type: str  # exact/normalized/fuzzy/alias/pattern
    confidence: float


@dataclass
class Investor:
    """Investor record for matching."""
    id: int
    canonical_name: str
    type: str
    aliases: List[str]
    legal_entities: List[str]
    partners: List[str]


class InvestorMatcher:
    """
    Match text against known investors/VCs.

    Loads investor data from database or YAML and provides
    fast matching with multiple strategies.
    """

    # Legal form suffixes to strip for normalization
    LEGAL_SUFFIXES = [
        'gmbh & co. kg', 'gmbh & co kg', 'gmbh &co. kg',
        'gmbh & co. kgaa', 'ag & co. kg', 'ag & co kg',
        'gmbh', 'ug', 'ag', 'kg', 'kgaa', 'se', 'ohg',
        'haftungsbeschränkt', '(haftungsbeschränkt)',
        'mbh', 'e.v.', 'ev', 'gbr',
        'limited', 'ltd', 'llc', 'llp', 'lp', 'inc', 'corp',
        'plc', 'sa', 'sas', 'sarl', 'ab', 'oy', 'bv', 'nv'
    ]

    def __init__(self, db=None, yaml_path: Optional[str] = None):
        """
        Initialize matcher.

        Args:
            db: Database instance (if loading from DB)
            yaml_path: Path to investors.yaml (if loading from file)
        """
        self.db = db
        self.yaml_path = yaml_path or str(Path(__file__).parent.parent / 'config' / 'investors.yaml')

        # Lookup dictionaries for fast matching
        self.investors: Dict[int, Investor] = {}
        self.exact_lookup: Dict[str, int] = {}  # normalized name -> investor_id
        self.alias_lookup: Dict[str, int] = {}  # alias -> investor_id
        self.partner_lookup: Dict[str, int] = {}  # partner name -> investor_id

        # Load data
        self._load_investors()

    def _normalize(self, text: str) -> str:
        """
        Normalize text for matching.

        - Lowercase
        - Remove legal suffixes
        - Strip extra whitespace
        - Remove common punctuation
        """
        if not text:
            return ""

        text = text.lower().strip()

        # Remove legal suffixes (longest first)
        for suffix in sorted(self.LEGAL_SUFFIXES, key=len, reverse=True):
            if text.endswith(suffix):
                text = text[:-len(suffix)].strip()
            # Also check with various separators
            for sep in [' ', ', ', ' - ']:
                pattern = sep + suffix
                if text.endswith(pattern):
                    text = text[:-len(pattern)].strip()

        # Remove common punctuation
        text = re.sub(r'[,\.\-\(\)]+', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        return text

    def _load_investors(self):
        """Load investor data from DB or YAML."""
        if self.db:
            self._load_from_db()
        else:
            self._load_from_yaml()

        logger.info(
            "Loaded %d investors, %d aliases, %d partners",
            len(self.investors),
            len(self.alias_lookup),
            len(self.partner_lookup)
        )

    def _load_from_db(self):
        """Load investors from database."""
        conn = self.db._get_connection()

        # Load investors
        rows = conn.execute("SELECT id, canonical_name, type FROM investors").fetchall()
        for row in rows:
            inv = Investor(
                id=row['id'],
                canonical_name=row['canonical_name'],
                type=row['type'],
                aliases=[],
                legal_entities=[],
                partners=[]
            )
            self.investors[inv.id] = inv

            # Add canonical name to lookup
            normalized = self._normalize(inv.canonical_name)
            self.exact_lookup[normalized] = inv.id

        # Load aliases
        rows = conn.execute("""
            SELECT investor_id, alias, alias_type
            FROM investor_aliases
        """).fetchall()
        for row in rows:
            inv_id = row['investor_id']
            alias = row['alias']
            alias_type = row['alias_type']

            if inv_id in self.investors:
                if alias_type == 'partner_name':
                    self.investors[inv_id].partners.append(alias)
                    self.partner_lookup[self._normalize(alias)] = inv_id
                else:
                    self.investors[inv_id].aliases.append(alias)
                    self.alias_lookup[self._normalize(alias)] = inv_id

        # Load legal entities
        rows = conn.execute("""
            SELECT investor_id, entity_name
            FROM investor_legal_entities
        """).fetchall()
        for row in rows:
            inv_id = row['investor_id']
            entity = row['entity_name']

            if inv_id in self.investors:
                self.investors[inv_id].legal_entities.append(entity)
                # Add both original and normalized
                self.exact_lookup[entity.lower()] = inv_id
                self.exact_lookup[self._normalize(entity)] = inv_id

    def _load_from_yaml(self):
        """Load investors from YAML file."""
        try:
            with open(self.yaml_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            logger.warning("Investors YAML not found at %s", self.yaml_path)
            return

        investors = data.get('investors', [])

        for idx, inv_data in enumerate(investors, start=1):
            inv = Investor(
                id=idx,
                canonical_name=inv_data.get('name', ''),
                type=inv_data.get('type', 'vc'),
                aliases=inv_data.get('aliases', []),
                legal_entities=inv_data.get('legal_entities', []),
                partners=inv_data.get('partners', [])
            )
            self.investors[inv.id] = inv

            # Add canonical name
            normalized = self._normalize(inv.canonical_name)
            self.exact_lookup[normalized] = inv.id

            # Add aliases
            for alias in inv.aliases:
                self.alias_lookup[self._normalize(alias)] = inv.id

            # Add legal entities (both exact and normalized)
            for entity in inv.legal_entities:
                self.exact_lookup[entity.lower()] = inv.id
                self.exact_lookup[self._normalize(entity)] = inv.id

            # Add partners
            for partner in inv.partners:
                self.partner_lookup[self._normalize(partner)] = inv.id

    def match(self, text: str, min_confidence: float = 0.7) -> List[InvestorMatch]:
        """
        Find investor matches in text.

        Args:
            text: Text to search for investor names
            min_confidence: Minimum confidence threshold

        Returns:
            List of InvestorMatch objects, sorted by confidence desc
        """
        if not text:
            return []

        matches = []
        text_lower = text.lower()
        text_normalized = self._normalize(text)

        # 1. Exact match on full text (confidence: 1.0)
        if text_lower in self.exact_lookup:
            inv_id = self.exact_lookup[text_lower]
            inv = self.investors[inv_id]
            matches.append(InvestorMatch(
                investor_id=inv_id,
                investor_name=inv.canonical_name,
                matched_text=text,
                match_type='exact',
                confidence=1.0
            ))

        # 2. Normalized match (confidence: 0.95)
        elif text_normalized in self.exact_lookup:
            inv_id = self.exact_lookup[text_normalized]
            inv = self.investors[inv_id]
            matches.append(InvestorMatch(
                investor_id=inv_id,
                investor_name=inv.canonical_name,
                matched_text=text,
                match_type='normalized',
                confidence=0.95
            ))

        # 3. Alias match (confidence: 0.9)
        elif text_normalized in self.alias_lookup:
            inv_id = self.alias_lookup[text_normalized]
            inv = self.investors[inv_id]
            matches.append(InvestorMatch(
                investor_id=inv_id,
                investor_name=inv.canonical_name,
                matched_text=text,
                match_type='alias',
                confidence=0.9
            ))

        # 4. Partner name match (confidence: 0.85)
        elif text_normalized in self.partner_lookup:
            inv_id = self.partner_lookup[text_normalized]
            inv = self.investors[inv_id]
            matches.append(InvestorMatch(
                investor_id=inv_id,
                investor_name=inv.canonical_name,
                matched_text=text,
                match_type='partner',
                confidence=0.85
            ))

        # 5. Fuzzy matching if no exact match found
        elif RAPIDFUZZ_AVAILABLE and min_confidence <= 0.8:
            fuzzy_matches = self._fuzzy_match(text_normalized, min_confidence)
            matches.extend(fuzzy_matches)

        # Filter by confidence and dedupe
        matches = [m for m in matches if m.confidence >= min_confidence]
        matches = self._dedupe_matches(matches)

        return sorted(matches, key=lambda m: m.confidence, reverse=True)

    def _fuzzy_match(self, text: str, min_confidence: float) -> List[InvestorMatch]:
        """
        Perform fuzzy matching using rapidfuzz.

        Returns matches with confidence based on similarity score.
        """
        matches = []

        # Build list of all searchable names
        all_names = list(self.exact_lookup.keys()) + list(self.alias_lookup.keys())

        if not all_names:
            return matches

        # Find best matches
        results = process.extract(
            text,
            all_names,
            scorer=fuzz.ratio,
            limit=5
        )

        for match_text, score, _ in results:
            # Convert score (0-100) to confidence (0-1)
            confidence = score / 100.0 * 0.8  # Max 0.8 for fuzzy

            if confidence < min_confidence:
                continue

            # Find investor ID
            inv_id = self.exact_lookup.get(match_text) or self.alias_lookup.get(match_text)
            if inv_id and inv_id in self.investors:
                inv = self.investors[inv_id]
                matches.append(InvestorMatch(
                    investor_id=inv_id,
                    investor_name=inv.canonical_name,
                    matched_text=text,
                    match_type='fuzzy',
                    confidence=confidence
                ))

        return matches

    def _dedupe_matches(self, matches: List[InvestorMatch]) -> List[InvestorMatch]:
        """Remove duplicate matches, keeping highest confidence."""
        seen = {}
        for m in matches:
            if m.investor_id not in seen or m.confidence > seen[m.investor_id].confidence:
                seen[m.investor_id] = m
        return list(seen.values())

    def search_in_text(self, text: str, min_confidence: float = 0.8) -> List[InvestorMatch]:
        """
        Search for any investor mentions within a longer text.

        Splits text into potential entity names and matches each.

        Args:
            text: Longer text that may contain investor names
            min_confidence: Minimum confidence threshold

        Returns:
            List of unique InvestorMatch objects found
        """
        if not text:
            return []

        all_matches = []

        # Try matching the full text first
        full_matches = self.match(text, min_confidence)
        all_matches.extend(full_matches)

        # Split by common delimiters and try each segment
        segments = re.split(r'[,;\n\r\t]+', text)
        for segment in segments:
            segment = segment.strip()
            if len(segment) > 3:  # Skip very short segments
                matches = self.match(segment, min_confidence)
                all_matches.extend(matches)

        # Look for patterns like "investor X" or "backed by Y"
        patterns = [
            r'(?:investor|backed by|funded by|investment from|capital from)\s+([A-Za-z0-9\s&\.\-]+)',
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:GmbH|UG|AG|KG|Ventures|Capital|Partners)',
        ]

        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                candidate = match.group(1).strip() if match.lastindex else match.group(0).strip()
                matches = self.match(candidate, min_confidence)
                all_matches.extend(matches)

        # Dedupe and return
        return self._dedupe_matches(all_matches)

    def get_investor(self, investor_id: int) -> Optional[Investor]:
        """Get investor by ID."""
        return self.investors.get(investor_id)

    def get_all_investors(self) -> List[Investor]:
        """Get all loaded investors."""
        return list(self.investors.values())

    def seed_to_database(self, db) -> int:
        """
        Seed investors from YAML to database.

        Args:
            db: Database instance

        Returns:
            Number of investors added
        """
        count = 0
        conn = db._get_connection()

        for inv in self.investors.values():
            try:
                # Insert investor
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO investors
                    (canonical_name, type, headquarters_city, stage_focus, sector_focus)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    inv.canonical_name,
                    inv.type,
                    None,  # headquarters from YAML if available
                    None,  # JSON array
                    None,  # JSON array
                ))

                # Get the ID (either new or existing)
                row = conn.execute(
                    "SELECT id FROM investors WHERE canonical_name = ?",
                    (inv.canonical_name,)
                ).fetchone()

                if not row:
                    continue

                db_id = row[0]

                # Insert aliases
                for alias in inv.aliases:
                    conn.execute("""
                        INSERT OR IGNORE INTO investor_aliases
                        (investor_id, alias, alias_type)
                        VALUES (?, ?, 'alias')
                    """, (db_id, alias))

                # Insert legal entities
                for entity in inv.legal_entities:
                    conn.execute("""
                        INSERT OR IGNORE INTO investor_legal_entities
                        (investor_id, entity_name, entity_type)
                        VALUES (?, ?, 'legal_entity')
                    """, (db_id, entity))

                # Insert partners
                for partner in inv.partners:
                    conn.execute("""
                        INSERT OR IGNORE INTO investor_aliases
                        (investor_id, alias, alias_type)
                        VALUES (?, ?, 'partner_name')
                    """, (db_id, partner))

                count += 1

            except Exception as e:
                logger.error("Error seeding investor %s: %s", inv.canonical_name, e)

        conn.commit()
        logger.info("Seeded %d investors to database", count)
        return count


def create_matcher(db=None) -> InvestorMatcher:
    """
    Create an InvestorMatcher instance.

    Loads from database if db provided, otherwise from YAML.
    """
    return InvestorMatcher(db=db)
