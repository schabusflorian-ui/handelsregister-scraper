"""
Brand Name Scorer — Identify tech startup brand names in company names.

Traditional German companies use German words (Verwaltung, Immobilien, Bau)
or surnames (Müller, Schmidt). Tech startups use English or invented brands
(Allonic, constellr, Naboo, Celonis). This module detects the difference.

Used by the new-registration scanner to discover startups whose names
contain no AI/tech keywords.
"""

import re
from typing import List, Optional, Tuple
from dataclasses import dataclass, field

from processing.filters import extract_legal_form
from processing.startup_scorer import (
    STARTUP_NAME_PATTERNS,
    SME_NAME_PATTERNS,
    STARTUP_HUB_CITIES,
)


# ============================================================================
# Legal form scoring
# ============================================================================

LEGAL_FORM_SCORES = {
    'UG (haftungsbeschränkt)': 4,
    'UG': 4,
    'GmbH': 0,
    # Everything below is skipped (not startup material)
    'gGmbH': None,              # Non-profit
    'AG': None,
    'SE': None,
    'KGaA': None,
    'e.V.': None,
    'eV': None,
    'KG': None,
    'OHG': None,
    'GbR': None,
    'PartG': None,
    'PartGmbB': None,
    'GmbH & Co. KG': None,
    'GmbH & Co. KGaA': None,
    'AG & Co. KG': None,
    'UG (haftungsbeschränkt) & Co. KG': None,
}

# ============================================================================
# German word lists for non-German name detection
# ============================================================================

# Common German business words found in traditional company names
GERMAN_SME_WORDS = [
    # From SME_NAME_PATTERNS
    'verwaltung', 'beteiligungs', 'holding', 'immobilien',
    'grundstück', 'vermögens', 'treuhand', 'steuerberater',
    'steuerberatung', 'rechtsanwalt', 'rechtsanwält',
    'wirtschaftsprüf', 'sanierung', 'wohnungsbau',
    'lebensmittel', 'schiffsinvest', 'inkasso', 'autohaus',
    # Trade & commerce
    'handel', 'handels', 'handlung', 'vertrieb', 'import', 'export',
    # Services
    'dienst', 'dienstleistung', 'beratung', 'service',
    'pflege', 'reinigung', 'wartung', 'reparatur',
    # Transport & logistics
    'transport', 'logistik', 'spedition', 'fracht', 'kurier',
    # Finance & insurance
    'versicherung', 'makler', 'finanz', 'kredit', 'fonds',
    # Hospitality
    'gastro', 'gastronomie', 'hotel', 'pension', 'reise', 'touristik',
    # Construction
    'baugesellschaft', 'bauträger', 'bauunternehm', 'tiefbau', 'hochbau',
    'dachdecker', 'maler', 'gerüst', 'estrich', 'montage',
    # Trades & crafts
    'elektro', 'sanitär', 'heizung', 'klempner', 'tischler',
    'schreiner', 'zimmerer', 'metallbau', 'schlosserei',
    # Agriculture & nature
    'garten', 'landschaft', 'forst', 'agrar', 'landwirtschaft',
    # Food trades
    'metzger', 'fleisch', 'bäcker', 'konditor',
    # Personal care
    'friseur', 'kosmetik', 'fußpflege',
    # Health
    'apotheke', 'praxis', 'klinik', 'labor', 'physiotherapie',
    # Professional services
    'kanzlei', 'notar', 'gutachter', 'sachverständig',
    # Media & print
    'verlag', 'druckerei', 'buchhandlung', 'medien',
    # Marketing & advertising
    'agentur', 'werbung',
    # HR & education
    'personal', 'zeitarbeit', 'bildung', 'schule', 'akademie',
    # Engineering & architecture
    'planung', 'architektur', 'ingenieur', 'statik',
    # Automotive
    'fahrzeug', 'kfz', 'zweirad', 'werkstatt',
    # Textiles & fashion
    'textil', 'mode', 'bekleidung', 'schmuck',
    # Home & furniture
    'möbel', 'küchen', 'fenster', 'türen', 'bodenbelag',
    # Waste & environment
    'entsorgung', 'abfall', 'umwelt',
    # Social & community
    'stiftung', 'gemeinnütz', 'sozial', 'verein',
    # Real estate
    'grundbesitz', 'hausverwaltung', 'wohnbau', 'liegenschaft',
    # Investment & holding
    'invest', 'kapital', 'anlage', 'beteiligung',
    # General business suffixes
    'gesellschaft', 'unternehmen', 'gruppe', 'zentrum',
]

# Top German surnames (used as first word in traditional company names)
GERMAN_SURNAMES = [
    'müller', 'schmidt', 'schneider', 'fischer', 'weber',
    'wagner', 'becker', 'schulz', 'hoffmann', 'schäfer',
    'koch', 'richter', 'wolf', 'klein', 'schröder',
    'neumann', 'schwarz', 'braun', 'zimmermann', 'krüger',
    'hartmann', 'lange', 'werner', 'krause', 'lehmann',
    'köhler', 'herrmann', 'könig', 'mayer', 'walter',
    'huber', 'kaiser', 'fuchs', 'scholz', 'schulze',
    'weiß', 'jung', 'hahn', 'vogel', 'friedrich',
    'keller', 'günther', 'berger', 'frank', 'brandt',
    'peters', 'sauer', 'winter', 'sommer', 'haas',
    'beck', 'baumann', 'franke', 'albrecht', 'pfeifer',
    'simon', 'horn', 'ludwig', 'böhm', 'kuhn',
    'meier', 'maier', 'meyer',
]

# German surname endings that indicate a person name
GERMAN_SURNAME_ENDINGS = [
    'mann', 'berg', 'burg', 'stein', 'bach', 'feld',
    'dorf', 'haus', 'bauer', 'meier', 'maier', 'mayer', 'meyer',
]


# ============================================================================
# Result dataclass
# ============================================================================

@dataclass
class BrandNameScore:
    """Result of brand-name startup heuristic scoring."""
    total_score: int
    is_likely_tech_startup: bool
    legal_form_signal: int
    location_signal: int
    name_pattern_signal: int
    non_german_signal: int
    short_brand_signal: int
    signals: List[str] = field(default_factory=list)
    negative_signals: List[str] = field(default_factory=list)
    brand_part: str = ''


# ============================================================================
# Scorer
# ============================================================================

BRAND_NAME_THRESHOLD = 5


class BrandNameScorer:
    """
    Score company names for tech-startup brand heuristics.

    Combines legal form, location, existing startup name patterns,
    and a novel "non-German name" detector to identify likely tech startups
    even when they have no AI/tech keywords in their name.
    """

    def __init__(self):
        self._startup_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc)
            for p, score, desc in STARTUP_NAME_PATTERNS
        ]
        self._sme_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc)
            for p, score, desc in SME_NAME_PATTERNS
        ]

    def score(self, name: str, city: Optional[str] = None) -> BrandNameScore:
        """
        Score a company name for tech-startup brand likelihood.

        Args:
            name: Full company name including legal form
            city: City of registration (optional)

        Returns:
            BrandNameScore with breakdown of signals
        """
        if not name:
            return BrandNameScore(
                total_score=0, is_likely_tech_startup=False,
                legal_form_signal=0, location_signal=0,
                name_pattern_signal=0, non_german_signal=0,
                short_brand_signal=0, brand_part='',
            )

        signals = []
        negative_signals = []

        # Step 1: Extract legal form and brand part
        legal_form = extract_legal_form(name)
        brand_part = self._extract_brand_part(name, legal_form)

        # Step 2: Legal form signal
        legal_form_signal = self._score_legal_form(legal_form)
        if legal_form_signal is None:
            # Skip — not a valid startup legal form
            if legal_form:
                negative_signals.append(f'Skip legal form: {legal_form}')
            return BrandNameScore(
                total_score=-99, is_likely_tech_startup=False,
                legal_form_signal=-99, location_signal=0,
                name_pattern_signal=0, non_german_signal=0,
                short_brand_signal=0, brand_part=brand_part,
                negative_signals=negative_signals,
            )
        if legal_form_signal > 0:
            signals.append(f'Legal form: {legal_form} (+{legal_form_signal})')

        # Step 3: Location signal
        location_signal = 0
        if city:
            for hub_city, score in STARTUP_HUB_CITIES.items():
                if hub_city.lower() in city.lower():
                    location_signal = score
                    signals.append(f'Startup hub: {hub_city} (+{score})')
                    break

        # Step 4: Name pattern signal (reuse from StartupScorer)
        name_pattern_signal = 0
        for pattern, score, desc in self._startup_patterns:
            if pattern.search(name):
                name_pattern_signal += score
                if score > 0:
                    signals.append(f'Startup pattern: {desc} (+{score})')

        for pattern, score, desc in self._sme_patterns:
            if pattern.search(name):
                name_pattern_signal += score
                negative_signals.append(f'SME pattern: {desc} ({score})')

        # Step 5: Non-German brand name detection
        non_german_signal = 0
        is_non_german, reasons = self._is_non_german_brand(brand_part)
        if is_non_german:
            non_german_signal = 3
            signals.append(f'Non-German brand (+3): {", ".join(reasons)}')

        # Step 6: Short single-word brand signal
        short_brand_signal = 0
        brand_words = brand_part.split()
        if len(brand_words) == 1 and 3 <= len(brand_part) <= 12:
            short_brand_signal = 2
            signals.append(f'Short brand: "{brand_part}" (+2)')

        # Calculate total
        total = (legal_form_signal + location_signal + name_pattern_signal
                 + non_german_signal + short_brand_signal)

        return BrandNameScore(
            total_score=total,
            is_likely_tech_startup=total >= BRAND_NAME_THRESHOLD,
            legal_form_signal=legal_form_signal,
            location_signal=location_signal,
            name_pattern_signal=name_pattern_signal,
            non_german_signal=non_german_signal,
            short_brand_signal=short_brand_signal,
            signals=signals,
            negative_signals=negative_signals,
            brand_part=brand_part,
        )

    def _extract_brand_part(self, name: str, legal_form: Optional[str]) -> str:
        """Strip legal form to get the brand part of the name."""
        if not legal_form:
            return name.strip()
        # Remove legal form and clean up trailing separators
        brand = name.replace(legal_form, '').strip()
        brand = brand.rstrip('&-,').strip()
        # Remove "(haftungsbeschränkt)" if leftover
        brand = re.sub(r'\(haftungsbeschränkt\)', '', brand).strip()
        return brand

    def _score_legal_form(self, legal_form: Optional[str]) -> Optional[int]:
        """
        Score legal form. Returns None if the form should be skipped entirely.
        """
        if not legal_form:
            return 0  # Unknown — don't skip, just no bonus

        # Check from longest to shortest to match compound forms first
        for form in sorted(LEGAL_FORM_SCORES.keys(), key=len, reverse=True):
            if form.lower() in legal_form.lower():
                return LEGAL_FORM_SCORES[form]

        return 0  # Unknown form — don't skip

    def _is_non_german_brand(self, brand_part: str) -> Tuple[bool, List[str]]:
        """
        Determine if the brand part is likely non-German (English or invented).

        Returns:
            (is_non_german, reasons)
        """
        if not brand_part or len(brand_part) < 2:
            return False, []

        reasons = []
        brand_lower = brand_part.lower()
        words = brand_lower.split()

        # Check 1: Contains any common German SME words?
        has_german_word = False
        for german_word in GERMAN_SME_WORDS:
            if german_word in brand_lower:
                has_german_word = True
                break

        # Check 2: First word is a common German surname?
        has_german_surname = False
        if words:
            first_word = words[0]
            if first_word in GERMAN_SURNAMES:
                has_german_surname = True
            else:
                # Check surname endings (only for longer words)
                for ending in GERMAN_SURNAME_ENDINGS:
                    if first_word.endswith(ending) and len(first_word) >= len(ending) + 2:
                        has_german_surname = True
                        break

        if has_german_word:
            return False, ['contains German business word']
        if has_german_surname:
            return False, ['starts with German surname']

        reasons.append('no German words or surnames')

        # Bonus checks for extra confidence
        if brand_part and re.search(r'[a-z][A-Z]', brand_part):
            reasons.append('CamelCase')
        if re.search(r'[bcdfghjklmnpqrstvwxyz]{4,}', brand_lower):
            reasons.append('consonant cluster')

        return True, reasons
