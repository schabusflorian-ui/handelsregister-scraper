"""
Brand Name Scorer — Identify tech startup brand names in company names.

Traditional German companies use German words (Verwaltung, Immobilien, Bau)
or surnames (Müller, Schmidt). Tech startups use English or invented brands
(Allonic, constellr, Naboo, Celonis). This module detects the difference.

Used by the new-registration scanner to discover startups whose names
contain no AI/tech keywords.

v2 improvements:
- Stronger negative signals for shell/holding/Verwaltungs patterns
- Detects numbered shell companies (aptus 2635., Lindentor 1307. V V)
- Penalizes 2-4 letter abbreviation + number patterns (DFI 24, BRG 19)
- Detects Vorratsgesellschaft (shelf company) patterns
- Broader positive heuristics: English word detection, neologism patterns
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
# Shell / holding company detection (strong negative signals)
# ============================================================================

# Regex patterns for numbered shell companies (Vorratsgesellschaften)
# Examples: "aptus 2635.", "Lindentor 1307. V V", "SCUR-Alpha 1971",
#           "M&L 427", "firma.de Vorratsgesellschaft 1234"
SHELL_COMPANY_PATTERNS = [
    # "aptus NNNN." or "word NNNN." — mass-created shelf companies
    (re.compile(r'^\w+\s+\d{2,5}\.?\s*$', re.IGNORECASE),
     'numbered shell (word + number)'),
    # "word NNNN. V V" — numbered shell with placeholder initials
    (re.compile(r'\d{3,5}\.\s*[A-Z]\s+[A-Z]\b'),
     'numbered shell with initials'),
    # "SCUR-Alpha NNNN" — coded shelf company patterns
    (re.compile(r'^[A-Z]{2,5}[\-\s]?(Alpha|Beta|Gamma|Delta)\s+\d+', re.IGNORECASE),
     'coded shelf company'),
    # Explicit Vorratsgesellschaft / shelf company
    (re.compile(r'vorrats', re.IGNORECASE),
     'Vorratsgesellschaft'),
    (re.compile(r'shelf\s*compan', re.IGNORECASE),
     'shelf company'),
    # "firma.de Vorratsgesellschaft" pattern
    (re.compile(r'firma\.de', re.IGNORECASE),
     'firma.de shelf company'),
]

# Pattern: 2-4 uppercase letters + space + number (e.g., "DFI 24", "BRG 19", "VR 500")
# These are typically abbreviated holding/investment vehicles, not startups
ABBREVIATION_NUMBER_PATTERN = re.compile(
    r'^[A-Z&]{1,4}[\s\-]+\d{1,4}\b'
)

# Words that indicate financial/administrative vehicles (not tech startups)
# These get a stronger penalty than SME_NAME_PATTERNS
VEHICLE_WORDS = [
    'verwaltung', 'verwaltungs',
    'holding',
    'investment', 'investments',
    'capital',
    'beteiligungs', 'beteiligung',
    'immobilien',
    'vermögens', 'vermögensverwaltung',
    'treuhand',
    'vorratsgesellschaft',
    'windpark', 'solarpark',
    'grundbesitz',
]


# ============================================================================
# English word detection (positive signal for false negative reduction)
# ============================================================================

# Common English words found in startup names — distinct from German
# Only words that are clearly English and unlikely in traditional German names
ENGLISH_STARTUP_WORDS = {
    # Technology
    'cloud', 'smart', 'cyber', 'deep', 'next', 'fast', 'flash',
    'swift', 'flow', 'hub', 'hive', 'nest', 'core', 'forge',
    'wave', 'spark', 'pulse', 'edge', 'grid', 'node',
    'link', 'sync', 'loop', 'mesh', 'stack', 'scope',
    # Business/Product
    'scout', 'fleet', 'freight', 'trade', 'snap', 'shift',
    'boost', 'craft', 'match', 'spot', 'sprint',
    'dock', 'drop', 'pitch', 'dash', 'rush',
    # Modern branding
    'urban', 'fresh', 'bright', 'bold', 'pure', 'prime',
    'vivid', 'rapid', 'agile', 'lean', 'flex',
    'club', 'crew', 'tribe', 'space', 'world',
    'group', 'systems', 'works', 'point',
    # Compound starters
    'insta', 'ever', 'super', 'hyper', 'ultra', 'meta',
    'auto', 'open', 'true', 'real', 'clear',
}

# Startup-typical name endings (neologism suffixes)
# These catch invented brand names like Spotify, Shopify, Brainly, etc.
NEOLOGISM_SUFFIXES = [
    'ify', 'fy',    # Spotify, Shopify, Testify
    'ly',           # Brainly, Grammarly, Bitly
    'oo',           # Bamboo, Shazoo
    'io',           # Rubio, Twilio
    'ia',           # Personia, Insignia
    'ix',           # Nutanix, Citrix
    'yx',           # Onyx-style
    'eo',           # Cameo, Stereo
    'ry',           # Foundry, Pantry
    'er',           # (only for short coined words, handled separately)
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
    shell_penalty: int = 0
    english_signal: int = 0
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

        # Step 3: Shell company / vehicle detection (EARLY, strong negative)
        shell_penalty = self._detect_shell_company(brand_part)
        if shell_penalty < 0:
            negative_signals.append(f'Shell/vehicle pattern ({shell_penalty})')

        # Step 4: Location signal
        location_signal = 0
        if city:
            for hub_city, score in STARTUP_HUB_CITIES.items():
                if hub_city.lower() in city.lower():
                    location_signal = score
                    signals.append(f'Startup hub: {hub_city} (+{score})')
                    break

        # Step 5: Name pattern signal (reuse from StartupScorer)
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

        # Step 6: Non-German brand name detection
        non_german_signal = 0
        is_non_german, reasons = self._is_non_german_brand(brand_part)
        if is_non_german:
            non_german_signal = 3
            signals.append(f'Non-German brand (+3): {", ".join(reasons)}')

        # Step 7: Short single-word brand signal
        short_brand_signal = 0
        brand_words = brand_part.split()
        if len(brand_words) == 1 and 3 <= len(brand_part) <= 12:
            short_brand_signal = 2
            signals.append(f'Short brand: "{brand_part}" (+2)')

        # Step 8: English word / neologism detection (positive, false negative reduction)
        english_signal = self._detect_english_brand(brand_part)
        if english_signal > 0:
            signals.append(f'English/neologism brand (+{english_signal})')

        # Calculate total
        total = (legal_form_signal + location_signal + name_pattern_signal
                 + non_german_signal + short_brand_signal
                 + shell_penalty + english_signal)

        return BrandNameScore(
            total_score=total,
            is_likely_tech_startup=total >= BRAND_NAME_THRESHOLD,
            legal_form_signal=legal_form_signal,
            location_signal=location_signal,
            name_pattern_signal=name_pattern_signal,
            non_german_signal=non_german_signal,
            short_brand_signal=short_brand_signal,
            shell_penalty=shell_penalty,
            english_signal=english_signal,
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

    def _detect_shell_company(self, brand_part: str) -> int:
        """
        Detect shell companies, Vorratsgesellschaften, and financial vehicles.

        Returns a negative penalty (e.g., -8, -5, -3) or 0 if no match.
        """
        if not brand_part:
            return 0

        brand_lower = brand_part.lower()

        # Check explicit shell company patterns (strongest penalty)
        for pattern, desc in SHELL_COMPANY_PATTERNS:
            if pattern.search(brand_part):
                return -8  # Almost always kills the score

        # Check abbreviation + number patterns: "DFI 24", "BRG 19", "VR 500"
        if ABBREVIATION_NUMBER_PATTERN.match(brand_part):
            return -5

        # Check for vehicle words (Verwaltung, Holding, Investment, etc.)
        # Stronger penalty than the SME patterns from StartupScorer
        vehicle_count = 0
        matched_vehicles = []
        for word in VEHICLE_WORDS:
            if word in brand_lower:
                vehicle_count += 1
                matched_vehicles.append(word)

        if vehicle_count >= 2:
            return -6  # Multiple vehicle words = very likely not a startup
        elif vehicle_count == 1:
            # Stronger penalty if brand = abbreviation + vehicle word
            # (e.g., "DMN Investment", "JRI Holding", "ALE Verwaltungs")
            brand_words = brand_part.split()
            if (len(brand_words) == 2
                    and len(brand_words[0]) <= 4
                    and brand_words[0].upper() == brand_words[0]):
                return -5  # Abbreviation + vehicle word = very likely a vehicle
            return -3  # Single vehicle word = moderate penalty

        # Check for names that are purely numbers/codes: "1234", "42"
        stripped = re.sub(r'[\s\.\-&]', '', brand_part)
        if stripped.isdigit():
            return -8

        return 0

    def _detect_english_brand(self, brand_part: str) -> int:
        """
        Detect English words and neologism patterns in brand names.

        This is a positive signal that helps catch startups whose names
        don't trigger the "non-German" detector but still look startup-ish.

        Returns 0-2 bonus points.
        """
        if not brand_part or len(brand_part) < 3:
            return 0

        brand_lower = brand_part.lower()
        words = brand_lower.split()

        signal = 0

        # Check for English startup words in the brand
        for word in words:
            # Check both exact match and as substring (for compounds like InstaFreight)
            if word in ENGLISH_STARTUP_WORDS:
                signal = max(signal, 2)
                break
            # Check if any English word appears as prefix/suffix in compound words
            for eng_word in ENGLISH_STARTUP_WORDS:
                if len(eng_word) >= 4 and eng_word in word and word != eng_word:
                    signal = max(signal, 1)

        # Check for neologism suffixes (invented brand endings)
        if len(words) == 1 and len(brand_part) >= 4:
            for suffix in NEOLOGISM_SUFFIXES:
                if brand_lower.endswith(suffix) and len(brand_lower) > len(suffix) + 2:
                    signal = max(signal, 1)
                    break

        return signal

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
