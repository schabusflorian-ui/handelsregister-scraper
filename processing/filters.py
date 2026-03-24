"""
AI/Robotics keyword filtering for company classification.

Filters companies based on keyword matches in their name and business purpose.
Provides separate AI/robotics and climate tech scoring.

IMPORTANT: Two keyword contexts exist:
- NAME_*_KEYWORDS: Slim lists for scoring company names (Handelsregister).
  Only terms that realistically appear in German company names.
- DEFAULT_*_KEYWORDS: Full lists kept for reference / future purpose-text scoring.
- News monitor (sources/news_monitor.py) has its own full-text pattern lists.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

# =============================================================================
# NAME-FOCUSED AI Keywords (for company name scoring)
# =============================================================================
#
# Only terms that realistically appear in German company names.
# Multi-word English academic terms like "retrieval augmented generation"
# or "convolutional neural network" are excluded — no one registers
# a company with those in the name at the Handelsregister.
#
# DESIGN DECISIONS:
# - "ki-"/"ai-" prefixes removed: false positives (e.g., "Kai-Uwe")
# - "smart" removed: 90%+ false positives (Smart GmbH, Smart Repair)
# - "automation"/"autonom" standalone removed: too generic
# - Healthcare AI removed: not in scope
# - Standalone " AI ", ".ai", "robot" handled via STANDALONE_AI_PATTERNS

NAME_AI_KEYWORDS = [
    # === AI Core — German terms in company names ===
    "künstliche intelligenz",
    "maschinelles lernen",
    # === AI Core — 2-word English terms used as branding in German company names ===
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "neural network",
    "generative ai",
    "generative ki",
    "data science",
    # === Robotics — very common in company names ===
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
    "drone",
    "drohne",
    "uav",
    # === Computer Vision — German compounds + established English terms ===
    "computer vision",
    "bildverarbeitung",
    "bilderkennung",
    "bildanalyse",
    "objekterkennung",
    "gesichtserkennung",
    "machine vision",
    "lidar",
    "videoanalyse",
    # === NLP / Language — German compounds in names ===
    # NOTE: "nlp" removed — nearly 100% false positives (Neuro-Linguistic Programming)
    "sprachverarbeitung",
    "spracherkennung",
    "chatbot",
    "textanalyse",
    # === Automation — compound forms only ===
    "rpa",
    "autonomes fahren",
    "autonome systeme",
    "selbstfahrend",
    # === Industry 4.0 — established branding terms ===
    "industrie 4.0",
    "smart factory",
    "digitaler zwilling",
    "digital twin",
    # === AI Application branding — common in company names ===
    "ai platform",
    "ki plattform",
    "ai software",
    "ki software",
    "ai solutions",
    "ki lösungen",
    "ai consulting",
    "ki beratung",
    # === General — single words that appear in company names ===
    "neural",
    "neuronale",
    "kognitiv",
    "analytics",
    "intelligent",
    # === Short acronyms / brand terms ===
    "mlops",
    "aiops",
    "automl",
    "iot",
    "edge ai",
    # === Sensor tech ===
    "sensorfusion",
    "sensor fusion",
    # === Deeptech — terms used in German company names ===
    "quantum",
    "quantencomputer",
    "photonics",
    "photonik",
    "nanotechnologie",
    "nanotechnology",
    "nanomaterial",
    "bioinformatik",
    "genomics",
    "proteomics",
    "crispr",
]


# =============================================================================
# NAME-FOCUSED Climate Keywords (for company name scoring)
# =============================================================================
#
# Only climate/cleantech terms that appear in German company names.
# Excludes multi-word English phrases like "renewable energy" or
# "solid state battery" — Germans don't register companies with those.

NAME_CLIMATE_KEYWORDS = [
    # === Core branding — very common in company names ===
    "cleantech",
    "greentech",
    "klimatechnologie",
    # === Energy — German compounds in names ===
    "solarenergie",
    "photovoltaik",
    "windenergie",
    "windkraft",
    "energiespeicher",
    "batterietechnologie",
    # === Hydrogen — single words in names ===
    "wasserstoff",
    "hydrogen",
    "brennstoffzelle",
    # === Mobility — German compounds ===
    "elektromobilität",
    "elektrofahrzeug",
    "ladeinfrastruktur",
    # === Carbon / Decarb — German compounds ===
    "dekarbonisierung",
    "co2-abscheidung",
    "co2-reduktion",
    # === Heat / Grid — German compounds ===
    "wärmepumpe",
    "geothermie",
    # === Water ===
    "wasseraufbereitung",
    # === AgriTech — brand terms ===
    "agritech",
    "agrartech",
    # === Efficiency — German compound ===
    "energieeffizienz",
]


# =============================================================================
# FULL keyword lists (for news article matching / future purpose-text scoring)
# =============================================================================
# These are the comprehensive lists used by news_monitor.py and kept for
# reference. NOT used for company name scoring — use NAME_*_KEYWORDS above.

DEFAULT_AI_KEYWORDS = [
    # Everything in NAME_AI_KEYWORDS plus multi-word English terms:
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
    "agentic ai",
    "ai agent",
    "ki-agent",
    "diffusion model",
    "text-to-image",
    "text-to-video",
    "retrieval augmented generation",
    "rag",
    "vector database",
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
    "industrie 4.0",
    "industry 4.0",
    "smart factory",
    "smart manufacturing",
    "digitaler zwilling",
    "digital twin",
    "cyber physical",
    "iot platform",
    "iot analytics",
    "algorithmic trading",
    "algo trading",
    "robo advisor",
    "robo-advisor",
    "fraud detection",
    "betrugserkennung",
    "intelligente systeme",
    "cognitive computing",
    "kognitiv",
    "neural",
    "neuronale",
    "edge ai",
    "embedded ai",
    "inference engine",
    "tinyml",
    "sensorfusion",
    "sensor fusion",
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

DEFAULT_CLIMATE_KEYWORDS = [
    # Everything in NAME_CLIMATE_KEYWORDS plus multi-word English terms:
    "cleantech",
    "clean tech",
    "greentech",
    "green tech",
    "climate tech",
    "klimatechnologie",
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
    "wasserstoff",
    "hydrogen",
    "grüner wasserstoff",
    "green hydrogen",
    "brennstoffzelle",
    "fuel cell",
    "elektromobilität",
    "electromobility",
    "elektrofahrzeug",
    "electric vehicle",
    "ladeinfrastruktur",
    "charging infrastructure",
    "energiespeicher",
    "energy storage",
    "batterietechnologie",
    "battery technology",
    "festkörperbatterie",
    "solid state battery",
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
    "smart grid",
    "intelligentes stromnetz",
    "power grid",
    "wärmepumpe",
    "heat pump",
    "geothermie",
    "geothermal",
    "agritech",
    "agrartech",
    "precision farming",
    "präzisionslandwirtschaft",
    "vertical farming",
    "insektenprotein",
    "alternative protein",
    "wasseraufbereitung",
    "water treatment",
    "water purification",
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
#
# DESIGN: \bAI\b is split into two patterns to reduce false positives:
# 1. AI NOT at start of name — very reliable (e.g., "Generative AI Studio")
# 2. AI at start of name — often false positives (company initials like
#    "AI Baumanagement", "AI Beauty Management", "AI Auto- und Industrie-Leasing",
#    Italian "AI Teatro", ~250 false positives in DB)
#    So we only match start-of-name AI when followed by a tech context word.
STANDALONE_AI_PATTERNS = [
    # AI in middle/end of name — very reliable signal
    # (?<=\s) ensures AI is preceded by whitespace (not at start of text)
    r"(?<=\s)AI\b",
    # AI at start — only if followed by tech-context word
    # Also handles "AI &" / "AI -" separators (e.g., "AI & Cognitive Computing")
    r"^AI[\s&\-]+(?:Solutions|Software|Platform|Consulting|Analytics|Analytical|"
    r"Robotics|Systems|Technologies|Technology|Labs?|Studio|Vision|Innovations?|"
    r"Research|Data|Cloud|Ventures|Machine|Deep|Neural|Tech|Digital|Intelligence|"
    r"Intelligent|Cognitive|Automation|Startup|Development|Engineering|Agent|"
    r"Agents|Computing|Science|Learning)",
    r"\.ai\b",  # Matches .ai domains (e.g., "company.ai")
    r"\brobot\b",  # Standalone "robot"
]

# High-signal keywords that strongly indicate AI/Robotics focus
# Must also appear in NAME_AI_KEYWORDS (these add bonus points)
HIGH_SIGNAL_KEYWORDS = [
    "künstliche intelligenz",
    "artificial intelligence",
    "maschinelles lernen",
    "machine learning",
    "deep learning",
    "neural network",
    "robotik",
    "robotics",
    "computer vision",
    "bildverarbeitung",
    "generative ai",
    "chatbot",
    "ai platform",
    "ki plattform",
]

# High-signal climate keywords for bonus scoring
# Must also appear in NAME_CLIMATE_KEYWORDS
HIGH_SIGNAL_CLIMATE_KEYWORDS = [
    "cleantech",
    "greentech",
    "wasserstoff",
    "brennstoffzelle",
    "dekarbonisierung",
    "photovoltaik",
]

# Keywords to use for Handelsregister portal searches
# These are the actual queries sent to the search interface
SEARCH_KEYWORDS_GERMAN = [
    # Core AI terms
    "künstliche intelligenz",
    "maschinelles lernen",
    "deep learning",
    "machine learning",
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
    # Data/Analytics
    "data science",
    "analytics",
    # Industry 4.0
    "industrie 4.0",
    # General (single words that match company names)
    "intelligent",
    "neural",
    "drohne",
    "lidar",
    "quantencomputer",
    "nanotechnologie",
    "photonics",
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
    "wärmepumpe",
    "geothermie",
    "agritech",
]

# Technology categories for classification
# These use simple substring matching against company names,
# so only name-realistic terms should be included
TECH_CATEGORIES = {
    "computer_vision": [
        "computer vision",
        "bildverarbeitung",
        "bilderkennung",
        "objekterkennung",
        "bildanalyse",
        "videoanalyse",
        "gesichtserkennung",
        "lidar",
        "machine vision",
    ],
    "nlp": [
        "sprachverarbeitung",
        "chatbot",
        "sprachmodell",
        "textanalyse",
        "spracherkennung",
        # NOTE: 'nlp' removed — nearly 100% false positives (Neuro-Linguistic Programming)
        # NOTE: 'llm' removed — false positives (LLM Ledermanufaktur, LLM Stahlbau, etc.)
    ],
    "robotics": [
        "robotik",
        "robotics",
        "robot",
        "drone",
        "drohne",
        "drohnen",
        "cobots",
        "cobot",
        "roboter",
        "exoskelett",
        "humanoid",
        "humanoide",
        "agv",
        "amr",
        "serviceroboter",
        "industrieroboter",
        "uav",
    ],
    "ml_analytics": [
        "machine learning",
        "maschinelles lernen",
        "deep learning",
        "data science",
        "neural",
        "neuronale",
        "datenwissenschaft",
        "mustererkennung",
        "mlops",
        "automl",
        "analytics",
    ],
    "generative_ai": [
        "generative ai",
        "generative ki",
        # NOTE: 'llm' removed — false positives (company initials/abbreviations)
    ],
    "autonomous_systems": [
        "autonomes fahren",
        "selbstfahrend",
        "autonome systeme",
        "autopilot",
    ],
    "industry_40": [
        "industrie 4.0",
        "smart factory",
        "digital twin",
        "digitaler zwilling",
    ],
    "fintech_ai": [
        "robo advisor",
        "robo-advisor",
    ],
    "general_ai": [
        "künstliche intelligenz",
        "artificial intelligence",
        "intelligent",
        "kognitiv",
        "ai platform",
        "ki plattform",
        "ai solutions",
        "ki lösungen",
        "ai consulting",
    ],
    "deeptech": [
        "quantum",
        "quantencomputer",
        "qubit",
        "photonics",
        "photonik",
        "nanotechnologie",
        "nanotechnology",
        "nanomaterial",
        "bioinformatik",
        "genomics",
        "proteomics",
        "crispr",
    ],
    "climate_tech": [
        "cleantech",
        "greentech",
        "klimatechnologie",
        "solar",
        "photovoltaik",
        "windenergie",
        "windkraft",
        "wasserstoff",
        "hydrogen",
        "brennstoffzelle",
        "elektromobilität",
        "energiespeicher",
        "batterie",
        "co2",
        "dekarbonisierung",
        "wärmepumpe",
        "geothermie",
        "agritech",
        "agrartech",
    ],
}


@dataclass
class FilterConfig:
    """Configuration for company filtering."""

    ai_robotics_keywords: List[str] = field(default_factory=lambda: NAME_AI_KEYWORDS.copy())
    climate_keywords: List[str] = field(default_factory=lambda: NAME_CLIMATE_KEYWORDS.copy())
    min_relevance_score: int = 1
    recent_months: Optional[int] = 24
    allowed_statuses: List[str] = field(default_factory=lambda: ["active", "ACTIVE", "currently registered"])
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
            pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
            self._keyword_patterns.append((kw, pattern))

        # Pre-compile climate keyword patterns
        self._climate_keyword_patterns = []
        for kw in self.config.climate_keywords:
            pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
            self._climate_keyword_patterns.append((kw, pattern))

        # Pre-compile high-signal patterns for bonus scoring
        self._high_signal_patterns = []
        for kw in HIGH_SIGNAL_KEYWORDS:
            pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
            self._high_signal_patterns.append((kw, pattern))

        # Pre-compile high-signal climate patterns
        self._high_signal_climate_patterns = []
        for kw in HIGH_SIGNAL_CLIMATE_KEYWORDS:
            pattern = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
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
        # Group patterns by concept to avoid double-counting
        # (e.g., mid-AI and start-AI patterns shouldn't both add +2)
        standalone_groups_matched = set()
        for pattern_name, pattern in self._standalone_patterns:
            if pattern.search(text):
                # Determine which group this pattern belongs to
                if "AI" in pattern_name and ".ai" not in pattern_name:
                    group = "AI"
                elif ".ai" in pattern_name:
                    group = ".ai"
                else:
                    group = pattern_name  # e.g., robot
                if group not in standalone_groups_matched:
                    standalone_groups_matched.add(group)
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
        # Track which pattern "groups" matched to avoid duplicates
        standalone_matched = set()
        for pattern_name, pattern in self._standalone_patterns:
            if pattern.search(text):
                # Map pattern to readable label
                if "AI" in pattern_name:
                    label = "AI"
                elif ".ai" in pattern_name:
                    label = ".AI"
                elif "robot" in pattern_name.lower():
                    label = "ROBOT"
                else:
                    label = pattern_name.replace(r"\b", "").upper()
                if label not in standalone_matched:
                    standalone_matched.add(label)
                    matched.append(f"[{label}]")

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
                rejection_reason=f"Low combined score: {combined_score} < {self.config.min_relevance_score}",
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
                    rejection_reason=f"Status not allowed: {status}",
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
                    rejection_reason=f"City not in filter: {city}",
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
                    rejection_reason=f"Legal form not in filter: {legal_form}",
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
                    rejection_reason=f"Capital below minimum: {capital} < {self.config.min_capital}",
                )

        # Check registration date
        if self.config.recent_months and registration_date:
            try:
                reg_date = datetime.fromisoformat(registration_date.replace("Z", "+00:00"))
                cutoff = datetime.now(reg_date.tzinfo) - timedelta(days=self.config.recent_months * 30)
                if reg_date < cutoff:
                    return FilterResult(
                        passes=False,
                        relevance_score=score,
                        climate_score=climate_score,
                        matched_keywords=matched_keywords,
                        tech_categories=tech_categories,
                        rejection_reason=f"Registration too old: {registration_date}",
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
        "GmbH & Co. KG",
        "GmbH & Co. KGaA",
        "AG & Co. KG",
        "UG (haftungsbeschränkt) & Co. KG",
        "UG (haftungsbeschränkt)",
        "gGmbH",
        "GmbH",
        "AG",
        "KGaA",
        "KG",
        "OHG",
        "UG",
        "e.V.",
        "eV",
        "GbR",
        "SE",
        "PartG",
        "PartGmbB",
    ]

    name_upper = name.upper()
    for form in legal_forms:
        if form.upper() in name_upper:
            return form

    return None
