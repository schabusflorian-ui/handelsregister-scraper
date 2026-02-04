"""
Startup Likelihood Scoring System

Distinguishes high-growth tech startups from traditional SMEs based on
multiple signals available in German company registry data.

Key Insight: Without registration dates in OffeneRegister, we rely on
structural and naming signals to identify likely startups.
"""

import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass


# ============================================================================
# SCORING CONSTANTS
# ============================================================================

# Legal forms that strongly indicate startups (require minimal capital)
STARTUP_LEGAL_FORMS = {
    'UG (haftungsbeschränkt)': 5,  # "Mini-GmbH" - only needs 1 EUR capital
    'UG': 5,                        # Short form
}

# Traditional/established legal forms (less likely to be startups)
ESTABLISHED_LEGAL_FORMS = {
    'AG': -1,                       # Stock corporation - usually larger
    'SE': -1,                       # European company - usually larger
    'KGaA': -1,                     # Partnership limited by shares
    'e.V.': -2,                     # Association - typically non-profit
    'eV': -2,
}

# Startup hub cities (major points)
STARTUP_HUB_CITIES = {
    'Berlin': 3,
    'München': 3,
    'Munich': 3,
    'Hamburg': 2,
    'Frankfurt am Main': 1,
    'Frankfurt': 1,
    'Köln': 1,
    'Cologne': 1,
    'Düsseldorf': 1,
    'Stuttgart': 1,
}

# Secondary tech cities
TECH_CITIES = {
    'Karlsruhe': 1,      # KIT / tech hub
    'Aachen': 1,         # RWTH / tech hub
    'Dresden': 1,        # Silicon Saxony
    'Darmstadt': 1,      # TU Darmstadt
    'Heidelberg': 1,     # BioTech hub
    'Tübingen': 1,       # AI hub (Cyber Valley)
    'Leipzig': 1,        # Growing startup scene
    'Nürnberg': 1,       # Digital hub
    'Bonn': 1,           # AI / Cyber Security
    'Potsdam': 1,        # Digital / Media hub
}

# Name patterns that indicate startups/tech companies
# Tuple of (pattern, score, description)
STARTUP_NAME_PATTERNS = [
    # Tech suffix patterns (high signal)
    (r'\bLabs?\b', 3, 'Labs'),
    (r'\b\.io\b', 2, '.io domain hint'),
    (r'\b\.ai\b', 3, '.ai domain hint'),
    (r'\b\.co\b', 1, '.co domain hint'),
    (r'\b\.dev\b', 2, '.dev domain hint'),

    # English tech terms (medium signal)
    (r'\bTech\b', 2, 'Tech'),
    (r'\bSolutions\b', 1, 'Solutions'),
    (r'\bSoftware\b', 1, 'Software'),
    (r'\bDigital\b', 1, 'Digital'),
    (r'\bData\b', 1, 'Data'),
    (r'\bCloud\b', 2, 'Cloud'),
    (r'\bApp\b', 1, 'App'),
    (r'\bApps\b', 1, 'Apps'),
    (r'\bPlatform\b', 2, 'Platform'),
    (r'\bAnalytics\b', 2, 'Analytics'),
    (r'\bVentures?\b', 2, 'Ventures'),
    (r'\bAPI\b', 2, 'API'),
    (r'\bSaaS\b', 3, 'SaaS'),
    (r'\bFintech\b', 3, 'Fintech'),
    (r'\bHealthtech\b', 3, 'Healthtech'),
    (r'\bEdtech\b', 3, 'Edtech'),
    (r'\bInsurtech\b', 3, 'Insurtech'),
    (r'\bProptech\b', 3, 'Proptech'),
    (r'\bDeeptech\b', 3, 'Deeptech'),
    (r'\bCleantech\b', 3, 'Cleantech'),
    (r'\bBiotech\b', 2, 'Biotech'),
    (r'\bMedtech\b', 2, 'Medtech'),

    # Trendy startup naming patterns
    (r'ly$', 1, '-ly suffix'),          # e.g., Spotify, Shopify
    (r'ify$', 2, '-ify suffix'),        # e.g., Shopify, Spotify
    (r'ia$', 1, '-ia suffix'),          # e.g., Nvidia
    (r'io$', 1, '-io suffix'),          # e.g., Twilio
    (r'^[A-Z][a-z]+[A-Z]', 1, 'CamelCase'),  # e.g., GitHub, YouTube
    (r'^[a-z]+\.[a-z]+$', 2, 'domain style name'),  # e.g., scout24

    # Modern startup naming (single word, no GmbH suffix in brand)
    (r'^[A-Z][a-z]{3,8} (?:GmbH|UG)', 1, 'Short modern name'),

    # Innovation/Growth terms
    (r'\bInnovation\b', 1, 'Innovation'),
    (r'\bNext\b', 1, 'Next'),
    (r'\bFuture\b', 1, 'Future'),
    (r'\bSmart\b', 0, 'Smart'),  # 0 because it's also in traditional names
    (r'\bAgile\b', 1, 'Agile'),
    (r'\bScale\b', 1, 'Scale'),
    (r'\bGrowth\b', 1, 'Growth'),
    (r'\bDisrupt\b', 2, 'Disrupt'),
    (r'\bAccelerator\b', 2, 'Accelerator'),
    (r'\bIncubator\b', 2, 'Incubator'),
]

# Traditional SME name patterns (negative signals)
SME_NAME_PATTERNS = [
    (r'\bVerwaltung\b', -2, 'Verwaltung (management/administration)'),
    (r'\bVerwaltungs\b', -2, 'Verwaltungs'),
    (r'\bBeteiligungs\b', -1, 'Beteiligungs (holding)'),
    (r'\bHolding\b', -1, 'Holding'),
    (r'\bImmobilien\b', -2, 'Immobilien (real estate)'),
    (r'\bHandels\b', -1, 'Handels (trading)'),
    (r'\bGrundstück\b', -2, 'Grundstück (real estate)'),
    (r'\bVermögens\b', -2, 'Vermögens (asset management)'),
    (r'\bTreuhand\b', -2, 'Treuhand (trust)'),
    (r'\bSteuerberater\b', -1, 'Steuerberater (tax advisor)'),
    (r'\bRechtsanwält\b', -1, 'Rechtsanwälte (lawyers)'),
    (r'\bWirtschaftsprüf\b', -1, 'Wirtschaftsprüfer (auditor)'),
    (r'\bBau\b', -1, 'Bau (construction)'),
    (r'\bSanierung\b', -1, 'Sanierung (renovation)'),
    (r'\bHaus\b', -1, 'Haus (house/building)'),
    (r'\bWohnungsbau\b', -2, 'Wohnungsbau (residential construction)'),
    (r'\bLebensmittel\b', -2, 'Lebensmittel (food)'),
    (r'\bSchiffsinvest\b', -2, 'Schiffsinvest (ship investment)'),
    (r'\bSchiffs\b', -1, 'Schiffs (shipping)'),
    (r'\bInkasso\b', -2, 'Inkasso (debt collection)'),
    (r'\bAutohaus\b', -2, 'Autohaus (car dealership)'),
]


@dataclass
class StartupScore:
    """Result of startup likelihood scoring."""
    total_score: int
    is_likely_startup: bool
    legal_form_score: int
    location_score: int
    name_pattern_score: int
    ai_relevance_bonus: int
    signals: List[str]
    negative_signals: List[str]

    def __str__(self):
        return f"StartupScore({self.total_score}, startup={self.is_likely_startup})"


class StartupScorer:
    """
    Score companies on startup likelihood.

    Higher scores indicate more likely to be a tech startup vs traditional SME.

    Scoring breakdown:
    - Legal form: -2 to +5 points
    - Location: 0 to +3 points
    - Name patterns: -2 to +3 points each
    - High AI relevance score: +1 to +2 bonus

    Threshold: score >= 3 is "likely startup"
    """

    STARTUP_THRESHOLD = 3

    def __init__(self):
        # Pre-compile patterns
        self._startup_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc)
            for p, score, desc in STARTUP_NAME_PATTERNS
        ]
        self._sme_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc)
            for p, score, desc in SME_NAME_PATTERNS
        ]

    def score_company(
        self,
        name: str,
        legal_form: Optional[str] = None,
        city: Optional[str] = None,
        ai_relevance_score: int = 0,
    ) -> StartupScore:
        """
        Calculate startup likelihood score.

        Args:
            name: Company name
            legal_form: Legal form (GmbH, UG, AG, etc.)
            city: City of registration
            ai_relevance_score: AI/robotics relevance score from keyword filter

        Returns:
            StartupScore with breakdown
        """
        signals = []
        negative_signals = []

        # 1. Legal form scoring
        legal_form_score = 0
        if legal_form:
            # Check startup forms
            for form, score in STARTUP_LEGAL_FORMS.items():
                if form.lower() in legal_form.lower():
                    legal_form_score = score
                    signals.append(f"Legal form: {form} (+{score})")
                    break

            # Check established forms (only if not already matched)
            if legal_form_score == 0:
                for form, score in ESTABLISHED_LEGAL_FORMS.items():
                    if form.lower() in legal_form.lower():
                        legal_form_score = score
                        negative_signals.append(f"Legal form: {form} ({score})")
                        break

        # 2. Location scoring
        location_score = 0
        if city:
            city_normalized = city.strip()
            # Check startup hubs
            for hub_city, score in STARTUP_HUB_CITIES.items():
                if hub_city.lower() in city_normalized.lower():
                    location_score = score
                    signals.append(f"Startup hub: {hub_city} (+{score})")
                    break

            # Check tech cities (only if not hub)
            if location_score == 0:
                for tech_city, score in TECH_CITIES.items():
                    if tech_city.lower() in city_normalized.lower():
                        location_score = score
                        signals.append(f"Tech city: {tech_city} (+{score})")
                        break

        # 3. Name pattern scoring
        name_pattern_score = 0

        # Positive patterns
        for pattern, score, desc in self._startup_patterns:
            if pattern.search(name):
                name_pattern_score += score
                if score > 0:
                    signals.append(f"Name pattern: {desc} (+{score})")

        # Negative patterns
        for pattern, score, desc in self._sme_patterns:
            if pattern.search(name):
                name_pattern_score += score  # score is already negative
                negative_signals.append(f"SME pattern: {desc} ({score})")

        # 4. AI relevance bonus
        ai_bonus = 0
        if ai_relevance_score >= 4:
            ai_bonus = 2
            signals.append(f"High AI score: {ai_relevance_score} (+2)")
        elif ai_relevance_score >= 2:
            ai_bonus = 1
            signals.append(f"Medium AI score: {ai_relevance_score} (+1)")

        # Calculate total
        total_score = legal_form_score + location_score + name_pattern_score + ai_bonus

        return StartupScore(
            total_score=total_score,
            is_likely_startup=total_score >= self.STARTUP_THRESHOLD,
            legal_form_score=legal_form_score,
            location_score=location_score,
            name_pattern_score=name_pattern_score,
            ai_relevance_bonus=ai_bonus,
            signals=signals,
            negative_signals=negative_signals,
        )

    def classify(self, score: StartupScore, ai_relevance_score: int = 0) -> str:
        """
        Classify company based on startup score and AI relevance.

        Returns:
            'startup' - high-growth potential AI/robotics startup
            'tech_company' - tech-adjacent company
            'traditional' - traditional SME
        """
        # Require at least some AI relevance for startup classification
        # This prevents non-AI UGs from being classified as AI startups
        if score.total_score >= 5 and ai_relevance_score >= 1:
            return 'startup'
        elif score.total_score >= 2:
            return 'tech_company'
        else:
            return 'traditional'


def score_companies_batch(
    companies: List[Dict],
    scorer: Optional[StartupScorer] = None,
) -> List[Tuple[Dict, StartupScore, str]]:
    """
    Score a batch of companies for startup likelihood.

    Args:
        companies: List of company dicts with keys: name, legal_form, city, ai_robotics_score
        scorer: Optional pre-initialized scorer

    Returns:
        List of (company, score, classification) tuples
    """
    if scorer is None:
        scorer = StartupScorer()

    results = []
    for company in companies:
        ai_score = company.get('ai_robotics_score', 0)
        score = scorer.score_company(
            name=company.get('name', ''),
            legal_form=company.get('legal_form'),
            city=company.get('city'),
            ai_relevance_score=ai_score,
        )
        classification = scorer.classify(score, ai_relevance_score=ai_score)
        results.append((company, score, classification))

    return results


# Quick test function
if __name__ == '__main__':
    scorer = StartupScorer()

    test_cases = [
        # Likely startups
        ("KI Labs UG (haftungsbeschränkt)", "UG (haftungsbeschränkt)", "Berlin", 4),
        ("DeepTech AI GmbH", "GmbH", "München", 5),
        ("Robo Solutions UG", "UG", "Hamburg", 3),
        ("DataAnalytics.io GmbH", "GmbH", "Berlin", 2),

        # Borderline cases
        ("Smart Factory Tech GmbH", "GmbH", "Stuttgart", 2),
        ("AI Software GmbH", "GmbH", "Köln", 3),

        # Likely traditional SMEs
        ("Müller Verwaltungs GmbH", "GmbH", "Passau", 0),
        ("Erste Immobilien Beteiligungs AG", "AG", "Frankfurt", 0),
        ("Handelshaus Schmidt GmbH & Co. KG", "GmbH & Co. KG", "Bremen", 0),
    ]

    print("Startup Scoring Test Results")
    print("=" * 80)

    for name, legal_form, city, ai_score in test_cases:
        score = scorer.score_company(name, legal_form, city, ai_score)
        classification = scorer.classify(score, ai_relevance_score=ai_score)

        print(f"\n{name}")
        print(f"  Legal form: {legal_form}, City: {city}, AI Score: {ai_score}")
        print(f"  Total Score: {score.total_score} -> {classification.upper()}")
        print(f"  Breakdown: legal={score.legal_form_score}, location={score.location_score}, "
              f"name={score.name_pattern_score}, ai_bonus={score.ai_relevance_bonus}")
        if score.signals:
            print(f"  + Signals: {', '.join(score.signals)}")
        if score.negative_signals:
            print(f"  - Negative: {', '.join(score.negative_signals)}")
