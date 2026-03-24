"""
Startup Likelihood Scoring System

Distinguishes high-growth tech startups from traditional SMEs based on
multiple signals available in German company registry data.

Key Insight: Without registration dates in OffeneRegister, we rely on
structural and naming signals to identify likely startups.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ============================================================================
# SCORING CONSTANTS
# ============================================================================

# Legal forms that strongly indicate startups (require minimal capital)
STARTUP_LEGAL_FORMS = {
    "UG (haftungsbeschränkt)": 5,  # "Mini-GmbH" - only needs 1 EUR capital
    "UG": 5,  # Short form
}

# Traditional/established legal forms (less likely to be startups)
ESTABLISHED_LEGAL_FORMS = {
    "AG": -1,  # Stock corporation - usually larger
    "SE": -1,  # European company - usually larger
    "KGaA": -1,  # Partnership limited by shares
    "e.V.": -2,  # Association - typically non-profit
    "eV": -2,
}

# Startup hub cities (major points)
STARTUP_HUB_CITIES = {
    "Berlin": 3,
    "München": 3,
    "Munich": 3,
    "Hamburg": 2,
    "Frankfurt am Main": 1,
    "Frankfurt": 1,
    "Köln": 1,
    "Cologne": 1,
    "Düsseldorf": 1,
    "Stuttgart": 1,
}

# Secondary tech cities
TECH_CITIES = {
    "Karlsruhe": 1,  # KIT / tech hub
    "Aachen": 1,  # RWTH / tech hub
    "Dresden": 1,  # Silicon Saxony
    "Darmstadt": 1,  # TU Darmstadt
    "Heidelberg": 1,  # BioTech hub
    "Tübingen": 2,  # AI hub (Cyber Valley)
    "Leipzig": 1,  # Growing startup scene / SpinLab
    "Nürnberg": 1,  # Digital hub
    "Bonn": 1,  # AI / Cyber Security
    "Potsdam": 1,  # Digital / Media hub
    "Garching": 2,  # TUM / UnternehmerTUM
    "Jülich": 1,  # Forschungszentrum Jülich
    "Kaiserslautern": 1,  # DFKI / AI research
    "Erlangen": 1,  # FAU / Fraunhofer
    "Jena": 1,  # Optics / photonics hub
    "Greifswald": 1,  # Helmholtz / plasma research
}

# Name patterns that indicate startups/tech companies
# Tuple of (pattern, score, description)
STARTUP_NAME_PATTERNS = [
    # Tech suffix patterns (high signal)
    (r"\bLabs?\b", 3, "Labs"),
    (r"\b\.io\b", 2, ".io domain hint"),
    (r"\b\.ai\b", 3, ".ai domain hint"),
    (r"\b\.co\b", 1, ".co domain hint"),
    (r"\b\.dev\b", 2, ".dev domain hint"),
    # English tech terms (medium signal)
    (r"\bTech\b", 2, "Tech"),
    (r"\bSolutions\b", 1, "Solutions"),
    (r"\bSoftware\b", 1, "Software"),
    (r"\bDigital\b", 1, "Digital"),
    (r"\bData\b", 1, "Data"),
    (r"\bCloud\b", 2, "Cloud"),
    (r"\bApp\b", 1, "App"),
    (r"\bApps\b", 1, "Apps"),
    (r"\bPlatform\b", 2, "Platform"),
    (r"\bAnalytics\b", 2, "Analytics"),
    (r"\bVentures?\b", 2, "Ventures"),
    (r"\bAPI\b", 2, "API"),
    (r"\bSaaS\b", 3, "SaaS"),
    (r"\bFintech\b", 3, "Fintech"),
    (r"\bHealthtech\b", 3, "Healthtech"),
    (r"\bEdtech\b", 3, "Edtech"),
    (r"\bInsurtech\b", 3, "Insurtech"),
    (r"\bProptech\b", 3, "Proptech"),
    (r"\bDeeptech\b", 3, "Deeptech"),
    (r"\bCleantech\b", 3, "Cleantech"),
    (r"\bGreentech\b", 3, "Greentech"),
    (r"\bClimatech\b", 3, "Climatech"),
    (r"\bClimate\b", 2, "Climate"),
    (r"\bBiotech\b", 2, "Biotech"),
    (r"\bMedtech\b", 2, "Medtech"),
    (r"\bAgritech\b", 3, "Agritech"),
    (r"\bFoodtech\b", 3, "Foodtech"),
    (r"\bEnergytech\b", 3, "Energytech"),
    (r"\bMobility\b", 2, "Mobility"),
    (r"\bSolar\b", 2, "Solar"),
    (r"\bHydrogen\b", 2, "Hydrogen"),
    (r"\bWasserstoff\b", 2, "Wasserstoff"),
    (r"\bEnergy\b", 1, "Energy"),
    (r"\bEnergie\b", 1, "Energie"),
    (r"\bSustainab", 2, "Sustainable"),
    (r"\bNachhaltig", 2, "Nachhaltig"),
    (r"\bCircular\b", 2, "Circular"),
    (r"\bCarbon\b", 2, "Carbon"),
    (r"\bBattery\b", 2, "Battery"),
    (r"\bBatterie\b", 2, "Batterie"),
    (r"\bCharging\b", 1, "Charging"),
    (r"\bRecycling\b", 1, "Recycling"),
    # Trendy startup naming patterns
    (r"ly$", 1, "-ly suffix"),  # e.g., Spotify, Shopify
    (r"ify$", 2, "-ify suffix"),  # e.g., Shopify, Spotify
    (r"ia$", 1, "-ia suffix"),  # e.g., Nvidia
    (r"io$", 1, "-io suffix"),  # e.g., Twilio
    (r"^[A-Z][a-z]+[A-Z]", 1, "CamelCase"),  # e.g., GitHub, YouTube
    (r"^[a-z]+\.[a-z]+$", 2, "domain style name"),  # e.g., scout24
    # Modern startup naming (single word, no GmbH suffix in brand)
    (r"^[A-Z][a-z]{3,8} (?:GmbH|UG)", 1, "Short modern name"),
    # Innovation/Growth terms
    (r"\bInnovation\b", 1, "Innovation"),
    (r"\bNext\b", 1, "Next"),
    (r"\bFuture\b", 1, "Future"),
    (r"\bSmart\b", 0, "Smart"),  # 0 because it's also in traditional names
    (r"\bAgile\b", 1, "Agile"),
    (r"\bScale\b", 1, "Scale"),
    (r"\bGrowth\b", 1, "Growth"),
    (r"\bDisrupt\b", 2, "Disrupt"),
    (r"\bAccelerator\b", 2, "Accelerator"),
    (r"\bIncubator\b", 2, "Incubator"),
    # University spinoff / research-based patterns
    (r"\bAusgründung\b", 3, "Ausgründung (spinoff)"),
    (r"\bSpin-?off\b", 2, "Spinoff"),
    (r"\bTransfer\b", 1, "Transfer"),
    (r"\bForschung\b", 1, "Forschung (research)"),
    (r"\bResearch\b", 1, "Research"),
    (r"\bScience\b", 1, "Science"),
    (r"\bBio\b", 1, "Bio"),
    (r"\bNano\b", 2, "Nano"),
    (r"\bQuantum\b", 3, "Quantum"),
    (r"\bQuanten\b", 3, "Quanten"),
    (r"\bPhoton\b", 2, "Photon"),
    (r"\bGenomics?\b", 2, "Genomics"),
]

# Traditional SME name patterns (negative signals)
SME_NAME_PATTERNS = [
    (r"\bVerwaltung\b", -2, "Verwaltung (management/administration)"),
    (r"\bVerwaltungs\b", -2, "Verwaltungs"),
    (r"\bBeteiligungs\b", -1, "Beteiligungs (holding)"),
    (r"\bHolding\b", -1, "Holding"),
    (r"\bImmobilien\b", -2, "Immobilien (real estate)"),
    (r"\bHandels\b", -1, "Handels (trading)"),
    (r"\bGrundstück\b", -2, "Grundstück (real estate)"),
    (r"\bVermögens\b", -2, "Vermögens (asset management)"),
    (r"\bTreuhand\b", -2, "Treuhand (trust)"),
    (r"\bSteuerberater\b", -1, "Steuerberater (tax advisor)"),
    (r"\bRechtsanwält\b", -1, "Rechtsanwälte (lawyers)"),
    (r"\bWirtschaftsprüf\b", -1, "Wirtschaftsprüfer (auditor)"),
    (r"\bBau\b", -1, "Bau (construction)"),
    (r"\bSanierung\b", -1, "Sanierung (renovation)"),
    (r"\bHaus\b", -1, "Haus (house/building)"),
    (r"\bWohnungsbau\b", -2, "Wohnungsbau (residential construction)"),
    (r"\bLebensmittel\b", -2, "Lebensmittel (food)"),
    (r"\bSchiffsinvest\b", -2, "Schiffsinvest (ship investment)"),
    (r"\bSchiffs\b", -1, "Schiffs (shipping)"),
    (r"\bInkasso\b", -2, "Inkasso (debt collection)"),
    (r"\bAutohaus\b", -2, "Autohaus (car dealership)"),
]


@dataclass
class StartupScore:
    """Result of startup likelihood scoring."""

    total_score: int
    is_likely_startup: bool
    legal_form_score: int
    location_score: int
    name_pattern_score: int
    purpose_score: int
    capital_score: int
    ai_relevance_bonus: int
    signals: List[str]
    negative_signals: List[str]

    def __str__(self):
        return f"StartupScore({self.total_score}, startup={self.is_likely_startup})"


# Purpose-text indicators that boost startup score
# These are terms found in the German business purpose (Geschäftszweck)
# that indicate a tech/innovation-oriented company.
PURPOSE_TECH_KEYWORDS = [
    # Software / Digital
    (r"\bsoftwareentwicklung\b", 2, "Software development"),
    (r"\bsoftware\b", 1, "Software"),
    (r"\bapp-entwicklung\b", 2, "App development"),
    (r"\bsaas\b", 2, "SaaS"),
    (r"\bplattform\b", 1, "Platform"),
    (r"\bdigitalisierung\b", 1, "Digitalization"),
    # AI / Data
    (r"\bkünstliche\s+intelligenz\b", 3, "AI (purpose)"),
    (r"\bki-gestützt\b", 2, "AI-supported (purpose)"),
    (r"\bmaschinelles\s+lernen\b", 3, "ML (purpose)"),
    (r"\bdeep\s+learning\b", 3, "Deep Learning (purpose)"),
    (r"\bdatenanalyse\b", 2, "Data analysis"),
    (r"\bbildanalyse\b", 2, "Image analysis (purpose)"),
    (r"\bbildverarbeitung\b", 2, "Image processing (purpose)"),
    (r"\balgorithm\b", 2, "Algorithm"),
    (r"\bautomatisierung\b", 1, "Automation"),
    # Biotech / Health
    (r"\bbiotechnologie\b", 2, "Biotech (purpose)"),
    (r"\bgenomsequenzierung\b", 3, "Genomics (purpose)"),
    (r"\bpersonalisierte\s+medizin\b", 3, "Personalized medicine"),
    (r"\bdiagnostik\b", 1, "Diagnostics"),
    (r"\bmedizinprodukt\b", 1, "Medical device"),
    # Climate / Energy
    (r"\bsolarenergie\b", 2, "Solar (purpose)"),
    (r"\bphotovoltaik\b", 2, "PV (purpose)"),
    (r"\bwasserstoff\b", 2, "Hydrogen (purpose)"),
    (r"\bbrennstoffzelle\b", 2, "Fuel cell (purpose)"),
    (r"\benergiespeicher\b", 2, "Energy storage (purpose)"),
    (r"\belektromobilität\b", 2, "E-mobility (purpose)"),
    (r"\bdekarbonisierung\b", 2, "Decarbonization (purpose)"),
    (r"\bwärmepumpe\b", 2, "Heat pump (purpose)"),
    (r"\bkreislaufwirtschaft\b", 2, "Circular economy (purpose)"),
    (r"\brecycling-technologie\b", 2, "Recycling tech (purpose)"),
    # Fintech
    (r"\bfinanzdienstleistung\b", 1, "Financial services"),
    (r"\bopen\s+banking\b", 2, "Open banking (purpose)"),
    (r"\bzahlungsverkehr\b", 1, "Payments (purpose)"),
    (r"\bblockchain\b", 2, "Blockchain (purpose)"),
    # Cybersecurity
    (r"\bit-sicherheit\b", 2, "IT security (purpose)"),
    (r"\bcybersecurity\b", 2, "Cybersecurity (purpose)"),
    (r"\bpenetrations\w*\b", 2, "Pentest (purpose)"),
    # Robotics / Hardware
    (r"\brobotik\b", 2, "Robotics (purpose)"),
    (r"\bdrohnen\b", 2, "Drones (purpose)"),
    (r"\bsensorik\b", 1, "Sensors (purpose)"),
    (r"\b3d-druck\b", 2, "3D printing (purpose)"),
    (r"\bhalbleiter\b", 2, "Semiconductor (purpose)"),
    # Traditional SME purpose patterns (negative)
    (r"\bvermietung\s+von\s+immobilien\b", -2, "Real estate rental (purpose)"),
    (r"\bverwaltung\s+von\s+immobilien\b", -2, "Property management (purpose)"),
    (r"\bverwaltung\s+eigenen\s+vermögens\b", -2, "Asset management (purpose)"),
    (r"\bgastronomie\b", -1, "Gastronomy (purpose)"),
    (r"\bbäckerei\b", -1, "Bakery (purpose)"),
    (r"\bfriseursalon\b", -1, "Hair salon (purpose)"),
    (r"\breinigung\b", -1, "Cleaning (purpose)"),
    (r"\bsteuerberatung\b", -1, "Tax advisory (purpose)"),
    (r"\bwirtschaftsprüfung\b", -1, "Audit (purpose)"),
    (r"\bbackwaren\b", -1, "Bakery products (purpose)"),
]

# Capital amount tiers for scoring
# Capital is a strong signal: most tech startups start with minimum GmbH
# capital (25K) or UG (1 EUR), while funded ones raise significantly.
CAPITAL_TIERS = [
    # (min_amount, max_amount, score, description)
    (500_000, None, 2, "Significant capital (likely funded)"),
    (100_000, 500_000, 1, "Above-minimum capital"),
    (25_000, 100_000, 0, "Standard GmbH capital"),
    (1, 25_000, 0, "Minimal capital"),  # UG/low capital — neutral
    (0, 1, -1, "Zero/shell capital"),
]


class StartupScorer:
    """
    Score companies on startup likelihood.

    Higher scores indicate more likely to be a tech startup vs traditional SME.

    Scoring breakdown:
    - Legal form: -2 to +5 points
    - Location: 0 to +3 points
    - Name patterns: -2 to +3 points each
    - Purpose analysis: -2 to +3 points each (NEW)
    - Capital tier: -1 to +2 points (NEW)
    - Domain relevance bonus: +1 to +2 bonus

    Threshold: score >= 3 is "likely startup"
    """

    STARTUP_THRESHOLD = 3

    def __init__(self):
        # Pre-compile patterns
        self._startup_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc) for p, score, desc in STARTUP_NAME_PATTERNS
        ]
        self._sme_patterns = [(re.compile(p, re.IGNORECASE), score, desc) for p, score, desc in SME_NAME_PATTERNS]
        self._purpose_patterns = [
            (re.compile(p, re.IGNORECASE), score, desc) for p, score, desc in PURPOSE_TECH_KEYWORDS
        ]

    def score_company(
        self,
        name: str,
        legal_form: Optional[str] = None,
        city: Optional[str] = None,
        ai_relevance_score: int = 0,
        climate_score: int = 0,
        purpose: Optional[str] = None,
        capital_amount: Optional[float] = None,
        tech_categories: Optional[List[str]] = None,
    ) -> StartupScore:
        """
        Calculate startup likelihood score.

        Args:
            name: Company name
            legal_form: Legal form (GmbH, UG, AG, etc.)
            city: City of registration
            ai_relevance_score: AI/robotics relevance score from keyword filter
            climate_score: Climate tech relevance score from keyword filter
            purpose: Business purpose (Geschäftszweck) — rich text signal
            capital_amount: Registered capital in EUR
            tech_categories: Detected tech categories from filter

        Returns:
            StartupScore with breakdown
        """
        signals = []
        negative_signals = []

        # 1. Legal form scoring
        legal_form_score = 0
        if legal_form:
            for form, score in STARTUP_LEGAL_FORMS.items():
                if form.lower() in legal_form.lower():
                    legal_form_score = score
                    signals.append(f"Legal form: {form} (+{score})")
                    break

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
            for hub_city, score in STARTUP_HUB_CITIES.items():
                if hub_city.lower() in city_normalized.lower():
                    location_score = score
                    signals.append(f"Startup hub: {hub_city} (+{score})")
                    break

            if location_score == 0:
                for tech_city, score in TECH_CITIES.items():
                    if tech_city.lower() in city_normalized.lower():
                        location_score = score
                        signals.append(f"Tech city: {tech_city} (+{score})")
                        break

        # 3. Name pattern scoring
        name_pattern_score = 0
        for pattern, score, desc in self._startup_patterns:
            if pattern.search(name):
                name_pattern_score += score
                if score > 0:
                    signals.append(f"Name: {desc} (+{score})")

        for pattern, score, desc in self._sme_patterns:
            if pattern.search(name):
                name_pattern_score += score
                negative_signals.append(f"SME name: {desc} ({score})")

        # 4. Purpose text scoring (NEW — rich signal from Geschäftszweck)
        purpose_score = 0
        if purpose:
            purpose_matched = 0
            for pattern, score, desc in self._purpose_patterns:
                if pattern.search(purpose):
                    purpose_score += score
                    purpose_matched += 1
                    if score > 0:
                        signals.append(f"Purpose: {desc} (+{score})")
                    else:
                        negative_signals.append(f"Purpose: {desc} ({score})")
            # Cap purpose score to avoid dominating
            purpose_score = max(-3, min(purpose_score, 5))

        # 5. Capital tier scoring (NEW)
        capital_score = 0
        if capital_amount is not None:
            for min_amt, max_amt, score, desc in CAPITAL_TIERS:
                in_range = capital_amount >= min_amt
                if max_amt is not None:
                    in_range = in_range and capital_amount < max_amt
                if in_range:
                    capital_score = score
                    if score != 0:
                        tag = signals if score > 0 else negative_signals
                        tag.append(f"Capital: {desc} ({'+' if score > 0 else ''}{score})")
                    break

        # 6. Relevance bonus (AI or climate — whichever is higher)
        relevance_bonus = 0
        combined_relevance = ai_relevance_score + climate_score
        if combined_relevance >= 4:
            relevance_bonus = 2
            if ai_relevance_score >= climate_score:
                signals.append(f"High AI score: {ai_relevance_score} (+2)")
            else:
                signals.append(f"High climate score: {climate_score} (+2)")
        elif combined_relevance >= 2:
            relevance_bonus = 1
            if ai_relevance_score >= climate_score:
                signals.append(f"Medium AI score: {ai_relevance_score} (+1)")
            else:
                signals.append(f"Medium climate score: {climate_score} (+1)")

        # Calculate total
        total_score = (
            legal_form_score + location_score + name_pattern_score + purpose_score + capital_score + relevance_bonus
        )

        return StartupScore(
            total_score=total_score,
            is_likely_startup=total_score >= self.STARTUP_THRESHOLD,
            legal_form_score=legal_form_score,
            location_score=location_score,
            name_pattern_score=name_pattern_score,
            purpose_score=purpose_score,
            capital_score=capital_score,
            ai_relevance_bonus=relevance_bonus,
            signals=signals,
            negative_signals=negative_signals,
        )

    def classify(
        self,
        score: StartupScore,
        ai_relevance_score: int = 0,
        climate_score: int = 0,
        tech_categories: Optional[List[str]] = None,
    ) -> str:
        """
        Classify company based on startup score and relevance signals.

        Returns:
            'startup' - high-growth potential tech/climate startup
            'scaleup' - tech-adjacent company with growth signals
            'established' - traditional SME or mature company

        Classification logic:
        - 'startup': strong structural + domain signals. Either:
          (a) total >= 5 and has some domain relevance (AI, climate, or tech category)
          (b) total >= 3 and has strong domain relevance (score >= 3) AND tech categories
        - 'scaleup': moderate signals — has tech/growth signals but not enough
          for full startup classification.
        - 'established': low score — traditional SME patterns dominate.
        """
        combined_relevance = ai_relevance_score + climate_score
        has_tech_categories = bool(tech_categories)

        # Path A: strong structural signals + some domain relevance
        if score.total_score >= 5 and (combined_relevance >= 1 or has_tech_categories):
            return "startup"

        # Path B: moderate structural but strong domain signals with tech categories
        # This catches climate/fintech/healthtech startups in non-hub cities
        if score.total_score >= 3 and combined_relevance >= 3 and has_tech_categories:
            return "startup"

        # Scaleup: moderate signals — tech-adjacent or growing
        if score.total_score >= 2:
            return "scaleup"

        # Also scaleup if has tech categories but low structural score
        if has_tech_categories and score.total_score >= 0:
            return "scaleup"

        # Established: traditional SME
        return "established"


def score_companies_batch(
    companies: List[Dict],
    scorer: Optional[StartupScorer] = None,
) -> List[Tuple[Dict, StartupScore, str]]:
    """
    Score a batch of companies for startup likelihood.

    Args:
        companies: List of company dicts with keys: name, legal_form, city,
                   ai_robotics_score, climate_score, purpose, capital_amount,
                   tech_categories
        scorer: Optional pre-initialized scorer

    Returns:
        List of (company, score, classification) tuples
    """
    if scorer is None:
        scorer = StartupScorer()

    results = []
    for company in companies:
        ai_score = company.get("ai_robotics_score", 0)
        clim_score = company.get("climate_score", 0)
        tech_cats = company.get("tech_categories")
        if isinstance(tech_cats, str):
            import json

            try:
                tech_cats = json.loads(tech_cats)
            except (json.JSONDecodeError, TypeError):
                tech_cats = None

        score = scorer.score_company(
            name=company.get("name", ""),
            legal_form=company.get("legal_form"),
            city=company.get("city"),
            ai_relevance_score=ai_score,
            climate_score=clim_score,
            purpose=company.get("purpose"),
            capital_amount=company.get("capital_amount"),
            tech_categories=tech_cats,
        )
        classification = scorer.classify(
            score,
            ai_relevance_score=ai_score,
            climate_score=clim_score,
            tech_categories=tech_cats,
        )
        results.append((company, score, classification))

    return results


# Quick test function
if __name__ == "__main__":
    scorer = StartupScorer()

    # Test cases: (name, legal_form, city, ai_score, climate_score, purpose, capital, tech_cats)
    test_cases = [
        # === Clear startups (AI) ===
        ("KI Labs UG", "UG (haftungsbeschränkt)", "Berlin", 4, 0,
         "Entwicklung von Software im Bereich künstliche Intelligenz", 1000, ["general_ai"]),
        ("DeepTech AI GmbH", "GmbH", "München", 5, 0,
         "Maschinelles Lernen und Datenanalyse", 25000, ["ml_analytics"]),
        # === Clear startups (Climate — no AI score) ===
        ("GreenHydrogen Solutions UG", "UG", "Berlin", 0, 4,
         "Herstellung und Vertrieb von Wasserstoff-Elektrolyseuren", 1000, ["climate_tech"]),
        ("SolarTech Cleantech GmbH", "GmbH", "München", 0, 3,
         "Entwicklung von Solarenergieanlagen und Energiespeichern", 100000, ["climate_tech"]),
        # === Startups (Fintech / Healthtech — no AI or climate) ===
        ("FinWave Digital Banking UG", "UG", "Berlin", 0, 0,
         "Digitale Finanzdienstleistungen und Open Banking API-Lösungen", 2000, ["fintech"]),
        ("CyberShield Security GmbH", "GmbH", "Frankfurt am Main", 0, 0,
         "IT-Sicherheit, Penetrationstests und Cybersecurity-Beratung", 25000, ["cybersecurity"]),
        ("DeepScan Medical Imaging GmbH", "GmbH", "Heidelberg", 0, 0,
         "Medizinische Bildanalyse mittels Deep Learning und KI-gestützter Diagnostik", 150000, ["healthtech"]),
        # === Scaleups ===
        ("EdgeAI Semiconductor GmbH", "GmbH", "Aachen", 2, 0,
         "Entwicklung von KI-Chips und Edge-Computing-Hardware", 2000000, ["deeptech"]),
        # === Established SMEs ===
        ("Müller Verwaltungs GmbH", "GmbH", "Passau", 0, 0,
         "Verwaltung eigenen Vermögens", 25000, None),
        ("Bäckerei Hoffmann GmbH", "GmbH", "Nürnberg", 0, 0,
         "Herstellung und Vertrieb von Backwaren aller Art", 50000, None),
        ("Schmidt Immobilien Verwaltung GmbH", "GmbH", "Essen", 0, 0,
         "Verwaltung und Vermietung von Immobilien", 25000, None),
    ]

    print("Startup Scoring Test Results")
    print("=" * 80)

    for name, legal_form, city, ai_score, clim_score, purpose, capital, tech_cats in test_cases:
        score = scorer.score_company(
            name, legal_form, city, ai_score, clim_score,
            purpose=purpose, capital_amount=capital, tech_categories=tech_cats,
        )
        classification = scorer.classify(
            score, ai_relevance_score=ai_score, climate_score=clim_score,
            tech_categories=tech_cats,
        )

        print(f"\n{name}")
        print(f"  Purpose: {purpose[:60]}...")
        print(f"  Legal: {legal_form}, City: {city}, Capital: {capital}, AI: {ai_score}, Climate: {clim_score}")
        print(f"  Total Score: {score.total_score} -> {classification.upper()}")
        print(
            f"  Breakdown: legal={score.legal_form_score}, location={score.location_score}, "
            f"name={score.name_pattern_score}, purpose={score.purpose_score}, "
            f"capital={score.capital_score}, relevance_bonus={score.ai_relevance_bonus}"
        )
        if score.signals:
            print(f"  + Signals: {', '.join(score.signals)}")
        if score.negative_signals:
            print(f"  - Negative: {', '.join(score.negative_signals)}")
