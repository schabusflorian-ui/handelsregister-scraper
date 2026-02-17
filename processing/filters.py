"""
AI/Robotics keyword filtering for company classification.

Filters companies based on keyword matches in their name and business purpose.
Provides separate AI/robotics and climate tech scoring.
"""

import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field


# =============================================================================
# AI / Robotics / Deeptech Keywords
# =============================================================================
#
# DESIGN DECISIONS:
# - "ki-" and "ai-" prefixes removed: cause false positives (e.g., "Kai-Uwe")
# - "smart" removed: 90%+ false positives (Smart GmbH, Smart Repair, etc.)
# - "agv"/"amr" kept but require word boundaries to avoid substring matches
# - "automation"/"autonom" removed as standalone: too many false positives
#   (building automation, Gebäudeautomation, etc.) — kept only in compounds
# - Healthcare AI removed: not in scope
# - Added standalone " AI " and ".ai" domain patterns (see STANDALONE_PATTERNS)

DEFAULT_AI_KEYWORDS = [
    # === AI Core (High Signal) ===
    "künstliche intelligenz",
    "artificial intelligence",
    "maschinelles lernen",
    "machine learning",
    "deep learning",
    "neural network",
    "neuronale netze",
    "neuronales netz",
    "generative ai",
    "generative ki",
    "large language model",
    "foundation model",

    # === Agentic AI / Modern AI (High Signal) ===
    "agentic ai",
    "ai agent",
    "ki-agent",
    "diffusion model",
    "text-to-image",
    "text-to-video",
    "retrieval augmented generation",
    "rag",
    "vector database",

    # === Robotics (High Signal) ===
    "robotik",
    "robotics",
    "roboter",
    "cobot",
    "cobots",
    "industrieroboter",
    "serviceroboter",
    "humanoide",
    "humanoid",
    "exoskelett",
    "exoskeleton",
    "drone",
    "uav",

    # === Automation & Autonomous (Only compound forms — standalone "automation"/"autonom" removed) ===
    "rpa",
    "process automation",
    "robotic process automation",
    "industrial automation",
    "autonomes fahren",
    "autonomous vehicle",
    "autonomous systems",
    "autonome systeme",
    "selbstfahrend",
    "self-driving",

    # === Computer Vision (High Signal) ===
    "computer vision",
    "bildverarbeitung",
    "bilderkennung",
    "image recognition",
    "objekterkennung",
    "object detection",
    "visual ai",
    "bildanalyse",
    "video analytics",
    "videoanalyse",
    "gesichtserkennung",
    "face recognition",
    "lidar",
    "3d vision",
    "machine vision",

    # === NLP / Language AI (High Signal) ===
    "natural language processing",
    "sprachverarbeitung",
    "nlp",
    "chatbot",
    "chat bot",
    "conversational ai",
    "language model",
    "sprachmodell",
    "spracherkennung",
    "speech recognition",
    "voice ai",
    "text mining",
    "textanalyse",
    "sentiment analysis",
    "named entity",

    # === Data Science / ML (Medium Signal) ===
    "data science",
    "datenwissenschaft",
    "predictive analytics",
    "prädiktive analytik",
    "predictive maintenance",
    "recommendation engine",
    "empfehlungssystem",
    "anomaly detection",
    "anomalieerkennung",
    "pattern recognition",
    "mustererkennung",

    # === Specific AI Applications (High Signal) ===
    "ai platform",
    "ki plattform",
    "ai software",
    "ki software",
    "ai solutions",
    "ki lösungen",
    "ai consulting",
    "ki beratung",
    "mlops",
    "aiops",
    "automl",

    # === Industry 4.0 / Smart Manufacturing ===
    "industrie 4.0",
    "industry 4.0",
    "smart factory",
    "smart manufacturing",
    "digitaler zwilling",
    "digital twin",
    "cyber physical",
    "iot platform",
    "iot analytics",

    # === Fintech AI ===
    "algorithmic trading",
    "algo trading",
    "robo advisor",
    "robo-advisor",
    "fraud detection",
    "betrugserkennung",

    # === General Tech Terms (Lower Signal - kept selective) ===
    "intelligente systeme",
    "cognitive computing",
    "kognitiv",
    "neural",
    "neuronale",

    # === Edge/Embedded AI ===
    "edge ai",
    "embedded ai",
    "inference engine",
    "tinyml",

    # === Vision/Sensor ===
    "sensorfusion",
    "sensor fusion",

    # === Deeptech / Research ===
    "quantum computing",
    "quantencomputer",
    "qubit",
    "photonics",
    "photonik",
    "nanotechnologie",
    "nanotechnology",
    "nanomaterial",
    "advanced materials",
    "neue materialien",
    "synthetische biologie",
    "synthetic biology",
    "genomics",
    "proteomics",
    "crispr",
    "bioinformatik",
    "computational biology",
    "drug discovery",
    "wirkstoffforschung",
]


# =============================================================================
# Climate Tech / Cleantech Keywords (separate scoring)
# =============================================================================
#
# EXCLUDED (too generic / false-positive-heavy):
# - nachhaltigkeit, sustainability, nachhaltig, sustainable
# - kreislaufwirtschaft, circular economy (matches waste disposal)
# - esg, impact investing, green finance (financial, not tech)
# - abfallwirtschaft, waste management, recycling technologie (traditional)
# - biomasse, biomass, bioenergie, bioenergy (traditional energy)

DEFAULT_CLIMATE_KEYWORDS = [
    # === Core Climate Tech ===
    "cleantech",
    "clean tech",
    "greentech",
    "green tech",
    "climate tech",
    "klimatechnologie",

    # === Renewable Energy ===
    "erneuerbare energie",
    "renewable energy",
    "solar energy",
    "solarenergie",
    "photovoltaik",
    "photovoltaic",
    "windenergie",
    "wind energy",
    "wind turbine",
    "windkraft",

    # === Hydrogen / Fuel Cells ===
    "wasserstoff",
    "hydrogen",
    "grüner wasserstoff",
    "green hydrogen",
    "brennstoffzelle",
    "fuel cell",

    # === E-Mobility ===
    "elektromobilität",
    "electromobility",
    "elektrofahrzeug",
    "electric vehicle",
    "ladeinfrastruktur",
    "charging infrastructure",

    # === Energy Storage / Batteries ===
    "energiespeicher",
    "energy storage",
    "batterietechnologie",
    "battery technology",
    "festkörperbatterie",
    "solid state battery",

    # === Carbon / Decarbonization ===
    "carbon capture",
    "co2-abscheidung",
    "co2 capture",
    "kohlenstoffabscheidung",
    "co2-reduktion",
    "dekarbonisierung",
    "decarbonization",
    "emissionshandel",
    "carbon trading",
    "carbon credit",
    "carbon footprint",
    "co2-fußabdruck",

    # === Grid / Heat ===
    "smart grid",
    "intelligentes stromnetz",
    "power grid",
    "wärmepumpe",
    "heat pump",
    "geothermie",
    "geothermal",

    # === AgriTech / Food ===
    "agritech",
    "agrartech",
    "precision farming",
    "präzisionslandwirtschaft",
    "vertical farming",
    "insektenprotein",
    "alternative protein",

    # === Water ===
    "wasseraufbereitung",
    "water treatment",
    "water purification",

    # === Energy Efficiency ===
    "energieeffizienz",
    "energy efficiency",
    "gebäudeenergie",
    "building energy",
    "klimaneutral",
    "climate neutral",
    "net zero",
    "netto null",
]


# Standalone patterns that need special regex handling
# These catch "AI" as standalone word and .ai domains
STANDALONE_AI_PATTERNS = [
    r'\bAI\b',           # Matches " AI " but not "HAIR" or "FAIR"
    r'\.ai\b',           # Matches .ai domains (e.g., "company.ai")
    r'\brobot\b',        # Standalone "robot"
]

# High-signal keywords that strongly indicate AI/Robotics focus
# Companies matching these get bonus relevance score
HIGH_SIGNAL_KEYWORDS = [
    "künstliche intelligenz",
    "artificial intelligence",
    "machine learning",
    "maschinelles lernen",
    "deep learning",
    "robotik",
    "robotics",
    "computer vision",
    "bildverarbeitung",
    "nlp",
    "neural network",
    "neuronale netze",
    "generative ai",
    "chatbot",
    "ai platform",
    "ki plattform",
    "agentic ai",
    "large language model",
    "diffusion model",
]

# High-signal climate keywords for bonus scoring
HIGH_SIGNAL_CLIMATE_KEYWORDS = [
    "cleantech",
    "greentech",
    "climate tech",
    "grüner wasserstoff",
    "green hydrogen",
    "carbon capture",
    "dekarbonisierung",
    "brennstoffzelle",
    "fuel cell",
    "solid state battery",
    "festkörperbatterie",
]

# Keywords to use for Handelsregister portal searches
# These are optimized for the search interface (single words work better)
SEARCH_KEYWORDS_GERMAN = [
    # Core AI terms (highest priority)
    "künstliche intelligenz",
    "maschinelles lernen",
    "deep learning",
    "machine learning",
    "neural",
    "neuronale",

    # Robotics
    "robotik",
    "roboter",
    "robotics",
    "cobot",

    # NLP/Language
    "chatbot",
    "sprachverarbeitung",
    "spracherkennung",

    # Vision
    "computer vision",
    "bildverarbeitung",
    "bilderkennung",
    "machine vision",

    # Data/Analytics
    "data science",
    "predictive",
    "analytics",

    # Industry 4.0
    "industrie 4.0",
    "digital twin",

    # General (kept selective)
    "intelligent",
    "cognitive",
]

# Separate search keywords for climate tech discovery
SEARCH_KEYWORDS_CLIMATE = [
    "cleantech",
    "greentech",
    "wasserstoff",
    "hydrogen",
    "brennstoffzelle",
    "photovoltaik",
    "solarenergie",
    "windenergie",
    "energiespeicher",
    "batterie",
    "elektromobilität",
    "ladeinfrastruktur",
    "dekarbonisierung",
    "klimaneutral",
    "wärmepumpe",
    "geothermie",
    "agritech",
    "carbon capture",
]

# Technology categories for classification
TECH_CATEGORIES = {
    'computer_vision': [
        'computer vision', 'bildverarbeitung', 'image recognition',
        'objekterkennung', 'visual ai', 'bilderkennung', 'visuell',
        'video analytics', 'videoanalyse', 'gesichtserkennung',
        'face recognition', 'lidar', '3d vision', 'object detection',
    ],
    'nlp': [
        'natural language processing', 'sprachverarbeitung', 'nlp',
        'text analytics', 'chatbot', 'conversational ai', 'language model',
        'sprachmodell', 'textanalyse', 'spracherkennung', 'speech recognition',
        'voice ai', 'text mining', 'sentiment analysis', 'llm',
    ],
    'robotics': [
        'robotik', 'robotics', 'robot', 'drone', 'drohne', 'drohnen',
        'cobots', 'cobot', 'industrial automation', 'roboter',
        'exoskelett', 'humanoid', 'humanoide', 'agv', 'amr',
        'serviceroboter', 'industrieroboter', 'uav',
    ],
    'ml_analytics': [
        'machine learning', 'maschinelles lernen', 'deep learning',
        'predictive analytics', 'forecasting', 'prognose', 'data science',
        'neural', 'neuronale', 'datenwissenschaft', 'anomaly detection',
        'pattern recognition', 'mustererkennung', 'recommendation',
        'empfehlungssystem', 'mlops', 'automl',
    ],
    'generative_ai': [
        'generative ai', 'generative ki', 'llm', 'large language model',
        'foundation model', 'transformer', 'gpt', 'stable diffusion',
        'text generation', 'image generation', 'diffusion model',
        'text-to-image', 'text-to-video', 'agentic ai', 'ai agent',
    ],
    'autonomous_systems': [
        'autonomous vehicle', 'autonomes fahren', 'selbstfahrend',
        'autonomous systems', 'autonome systeme', 'self-driving',
        'adas', 'autopilot',
    ],
    'industry_40': [
        'industrie 4.0', 'industry 4.0', 'smart factory', 'smart manufacturing',
        'digital twin', 'digitaler zwilling', 'cyber physical', 'iot analytics',
        'predictive maintenance',
    ],
    'fintech_ai': [
        'algorithmic trading', 'algo trading', 'quantitative', 'robo advisor',
        'fraud detection', 'betrugserkennung', 'credit scoring',
    ],
    'general_ai': [
        'künstliche intelligenz', 'artificial intelligence',
        'intelligent', 'cognitive', 'kognitiv', 'ai platform',
        'ki plattform', 'ai solutions', 'ki lösungen', 'ai consulting',
    ],
    'deeptech': [
        'quantum computing', 'quantencomputer', 'qubit', 'photonics', 'photonik',
        'nanotechnologie', 'nanotechnology', 'nanomaterial', 'advanced materials',
        'neue materialien', 'synthetische biologie', 'synthetic biology',
        'genomics', 'proteomics', 'crispr', 'bioinformatik',
        'computational biology', 'drug discovery', 'wirkstoffforschung',
    ],
    'climate_tech': [
        'cleantech', 'greentech', 'climate tech', 'klimatechnologie',
        'erneuerbare energie', 'renewable energy', 'solar', 'photovoltaik',
        'windenergie', 'wind energy', 'wasserstoff', 'hydrogen',
        'brennstoffzelle', 'fuel cell', 'elektromobilität', 'electric vehicle',
        'energiespeicher', 'energy storage', 'batterie', 'carbon capture',
        'co2', 'dekarbonisierung', 'decarbonization',
        'smart grid', 'wärmepumpe', 'heat pump', 'geothermie',
        'agritech', 'agrartech', 'vertical farming', 'net zero', 'klimaneutral',
    ],
}


@dataclass
class FilterConfig:
    """Configuration for company filtering."""
    ai_robotics_keywords: List[str] = field(default_factory=lambda: DEFAULT_AI_KEYWORDS.copy())
    climate_keywords: List[str] = field(default_factory=lambda: DEFAULT_CLIMATE_KEYWORDS.copy())
    min_relevance_score: int = 1
    recent_months: Optional[int] = 24
    allowed_statuses: List[str] = field(default_factory=lambda: ['active', 'ACTIVE', 'currently registered'])
    cities_filter: List[str] = field(default_factory=list)
    min_capital: Optional[float] = None
    legal_forms_filter: List[str] = field(default_factory=list)


@dataclass
class FilterResult:
    """Result of filtering a company."""
    passes: bool
    relevance_score: int
    climate_score: int
    matched_keywords: List[str]
    tech_categories: List[str]
    rejection_reason: Optional[str] = None


class AIRoboticsFilter:
    """
    Filter companies by AI/robotics and climate tech relevance.

    Analyzes company names and business purposes to identify
    AI, robotics, deeptech, and climate tech companies.

    AI/Robotics Scoring:
    - Each keyword match: +1 point
    - High-signal keyword match: +1 bonus point
    - Standalone AI/.ai/robot match: +2 points (strong signal)

    Climate Scoring (separate):
    - Each keyword match: +1 point
    - High-signal climate keyword: +1 bonus point
    """

    def __init__(self, config: Optional[FilterConfig] = None):
        self.config = config or FilterConfig()

        # Pre-compile AI keyword patterns
        self._keyword_patterns = []
        for kw in self.config.ai_robotics_keywords:
            pattern = re.compile(rf'\b{re.escape(kw)}\b', re.IGNORECASE)
            self._keyword_patterns.append((kw, pattern))

        # Pre-compile climate keyword patterns
        self._climate_keyword_patterns = []
        for kw in self.config.climate_keywords:
            pattern = re.compile(rf'\b{re.escape(kw)}\b', re.IGNORECASE)
            self._climate_keyword_patterns.append((kw, pattern))

        # Pre-compile high-signal patterns for bonus scoring
        self._high_signal_patterns = []
        for kw in HIGH_SIGNAL_KEYWORDS:
            pattern = re.compile(rf'\b{re.escape(kw)}\b', re.IGNORECASE)
            self._high_signal_patterns.append((kw, pattern))

        # Pre-compile high-signal climate patterns
        self._high_signal_climate_patterns = []
        for kw in HIGH_SIGNAL_CLIMATE_KEYWORDS:
            pattern = re.compile(rf'\b{re.escape(kw)}\b', re.IGNORECASE)
            self._high_signal_climate_patterns.append((kw, pattern))

        # Pre-compile standalone AI patterns
        self._standalone_patterns = []
        for pattern_str in STANDALONE_AI_PATTERNS:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            self._standalone_patterns.append((pattern_str, pattern))

    def calculate_relevance_score(self, text: str) -> int:
        """
        Calculate AI/robotics relevance score (excludes climate).

        Args:
            text: Company name + purpose text

        Returns:
            AI/robotics relevance score (0+):
            - Each unique keyword match: +1
            - Each high-signal keyword: +1 bonus
            - Standalone AI/.ai/robot: +2 (strong indicator)
        """
        if not text:
            return 0

        score = 0
        matched_keywords = set()

        # Count standard keyword matches
        for kw, pattern in self._keyword_patterns:
            if pattern.search(text):
                matched_keywords.add(kw)
                score += 1

        # Add bonus for high-signal keywords
        for kw, pattern in self._high_signal_patterns:
            if pattern.search(text) and kw in matched_keywords:
                score += 1  # Bonus point for high-signal match

        # Check standalone AI/robot patterns (strong signals)
        for pattern_name, pattern in self._standalone_patterns:
            if pattern.search(text):
                matched_keywords.add(pattern_name)
                score += 2  # Strong signal for standalone AI/.ai/robot

        return score

    def calculate_climate_score(self, text: str) -> int:
        """
        Calculate climate tech relevance score (separate from AI).

        Args:
            text: Company name + purpose text

        Returns:
            Climate tech score (0+):
            - Each unique keyword match: +1
            - Each high-signal climate keyword: +1 bonus
        """
        if not text:
            return 0

        score = 0
        matched_keywords = set()

        for kw, pattern in self._climate_keyword_patterns:
            if pattern.search(text):
                matched_keywords.add(kw)
                score += 1

        # Add bonus for high-signal climate keywords
        for kw, pattern in self._high_signal_climate_patterns:
            if pattern.search(text) and kw in matched_keywords:
                score += 1

        return score

    def get_matched_keywords(self, text: str) -> List[str]:
        """Return list of matched AI keywords including standalone patterns."""
        if not text:
            return []

        matched = []
        for keyword, pattern in self._keyword_patterns:
            if pattern.search(text):
                matched.append(keyword)

        # Also check standalone patterns
        for pattern_name, pattern in self._standalone_patterns:
            if pattern.search(text):
                readable = pattern_name.replace(r'\b', '').upper()
                matched.append(f"[{readable}]")

        # Also include climate keyword matches (tagged)
        for keyword, pattern in self._climate_keyword_patterns:
            if pattern.search(text):
                matched.append(f"[climate] {keyword}")

        return matched

    def classify_tech_categories(self, text: str) -> List[str]:
        """Classify company into technology categories."""
        if not text:
            return []

        text_lower = text.lower()
        categories = []

        for category, keywords in TECH_CATEGORIES.items():
            if any(kw in text_lower for kw in keywords):
                categories.append(category)

        return categories

    def filter_company(
        self,
        name: str,
        purpose: Optional[str] = None,
        status: Optional[str] = None,
        city: Optional[str] = None,
        capital: Optional[float] = None,
        registration_date: Optional[str] = None,
        legal_form: Optional[str] = None,
    ) -> FilterResult:
        """
        Apply all filters to a company.

        Args:
            name: Company name
            purpose: Business purpose (Geschäftszweck)
            status: Company status
            city: City
            capital: Capital amount
            registration_date: Registration date (ISO format)
            legal_form: Legal form (GmbH, AG, etc.)

        Returns:
            FilterResult with pass/fail, AI score, climate score, and details
        """
        from datetime import datetime, timedelta

        # Combine name and purpose for keyword matching
        text = f"{name} {purpose or ''}"

        # Calculate both scores
        score = self.calculate_relevance_score(text)
        climate_score = self.calculate_climate_score(text)
        matched_keywords = self.get_matched_keywords(text)
        tech_categories = self.classify_tech_categories(text)

        # Company passes if either AI or climate score meets threshold
        combined_score = score + climate_score
        if combined_score < self.config.min_relevance_score:
            return FilterResult(
                passes=False,
                relevance_score=score,
                climate_score=climate_score,
                matched_keywords=matched_keywords,
                tech_categories=tech_categories,
                rejection_reason=f'Low combined score: {combined_score} < {self.config.min_relevance_score}'
            )

        # Check status
        if status and self.config.allowed_statuses:
            status_lower = status.lower()
            allowed_lower = [s.lower() for s in self.config.allowed_statuses]
            if status_lower not in allowed_lower:
                return FilterResult(
                    passes=False,
                    relevance_score=score,
                    climate_score=climate_score,
                    matched_keywords=matched_keywords,
                    tech_categories=tech_categories,
                    rejection_reason=f'Status not allowed: {status}'
                )

        # Check city filter
        if self.config.cities_filter and city:
            if city not in self.config.cities_filter:
                return FilterResult(
                    passes=False,
                    relevance_score=score,
                    climate_score=climate_score,
                    matched_keywords=matched_keywords,
                    tech_categories=tech_categories,
                    rejection_reason=f'City not in filter: {city}'
                )

        # Check legal form filter
        if self.config.legal_forms_filter and legal_form:
            if legal_form not in self.config.legal_forms_filter:
                return FilterResult(
                    passes=False,
                    relevance_score=score,
                    climate_score=climate_score,
                    matched_keywords=matched_keywords,
                    tech_categories=tech_categories,
                    rejection_reason=f'Legal form not in filter: {legal_form}'
                )

        # Check capital minimum
        if self.config.min_capital is not None and capital is not None:
            if capital < self.config.min_capital:
                return FilterResult(
                    passes=False,
                    relevance_score=score,
                    climate_score=climate_score,
                    matched_keywords=matched_keywords,
                    tech_categories=tech_categories,
                    rejection_reason=f'Capital below minimum: {capital} < {self.config.min_capital}'
                )

        # Check registration date
        if self.config.recent_months and registration_date:
            try:
                reg_date = datetime.fromisoformat(registration_date.replace('Z', '+00:00'))
                cutoff = datetime.now(reg_date.tzinfo) - timedelta(days=self.config.recent_months * 30)
                if reg_date < cutoff:
                    return FilterResult(
                        passes=False,
                        relevance_score=score,
                        climate_score=climate_score,
                        matched_keywords=matched_keywords,
                        tech_categories=tech_categories,
                        rejection_reason=f'Registration too old: {registration_date}'
                    )
            except (ValueError, TypeError):
                pass

        # All filters passed
        return FilterResult(
            passes=True,
            relevance_score=score,
            climate_score=climate_score,
            matched_keywords=matched_keywords,
            tech_categories=tech_categories,
        )

    def quick_filter(self, name: str, purpose: Optional[str] = None) -> bool:
        """
        Quick check if company name/purpose contains any relevant keywords.

        Use this for fast filtering during bulk processing.
        """
        text = f"{name} {purpose or ''}"
        ai = self.calculate_relevance_score(text)
        climate = self.calculate_climate_score(text)
        return (ai + climate) >= self.config.min_relevance_score


def extract_legal_form(name: str) -> Optional[str]:
    """Extract legal form from company name."""
    legal_forms = [
        'GmbH & Co. KG',
        'GmbH & Co. KGaA',
        'AG & Co. KG',
        'UG (haftungsbeschränkt) & Co. KG',
        'UG (haftungsbeschränkt)',
        'gGmbH',
        'GmbH',
        'AG',
        'KGaA',
        'KG',
        'OHG',
        'UG',
        'e.V.',
        'eV',
        'GbR',
        'SE',
        'PartG',
        'PartGmbB',
    ]

    name_upper = name.upper()
    for form in legal_forms:
        if form.upper() in name_upper:
            return form

    return None
