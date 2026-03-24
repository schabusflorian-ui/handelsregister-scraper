"""
Configuration file for Handelsregister Scraper

Edit these values to customize the scraper behavior
"""

# =============================================================================
# API CONFIGURATION
# =============================================================================

# Your handelsregister.ai API key (alternatively set HANDELSREGISTER_API_KEY env var)
API_KEY = None  # Set to your key or use environment variable

# Rate limiting: seconds to wait between API calls
RATE_LIMIT_DELAY = 1.0

# Request timeout in seconds
REQUEST_TIMEOUT = 60


# =============================================================================
# SEARCH PARAMETERS
# =============================================================================

# Keywords to search for (German and English)
# Add or remove keywords based on your focus area
SEARCH_KEYWORDS = [
    # AI Keywords
    "künstliche intelligenz",
    "artificial intelligence",
    "AI",
    "KI",
    "machine learning",
    "maschinelles lernen",
    "deep learning",
    "neural network",
    # Robotics Keywords
    "robotik",
    "robotics",
    "roboter",
    "autonomous",
    "autonome systeme",
    # Specific Technologies
    "computer vision",
    "natural language processing",
    "NLP",
    "chatbot",
    "automation",
    "predictive analytics",
    # Climate Tech / Cleantech
    "cleantech",
    "greentech",
    "wasserstoff",
    "hydrogen",
    "brennstoffzelle",
    "photovoltaik",
    "solarenergie",
    "windenergie",
    "energiespeicher",
    "elektromobilität",
    "ladeinfrastruktur",
    "dekarbonisierung",
    "kreislaufwirtschaft",
    "nachhaltigkeit",
    "klimaneutral",
    "wärmepumpe",
    "geothermie",
    "agritech",
    "carbon capture",
    # Deeptech / Research Spinoffs
    "quantum",
    "quanten",
    "photonics",
    "photonik",
    "nanotechnologie",
    "synthetische biologie",
    "genomics",
    "neue materialien",
]

# Maximum results to fetch per keyword
MAX_RESULTS_PER_KEYWORD = 20


# =============================================================================
# FILTERING PARAMETERS
# =============================================================================

# Only include companies incorporated in the last N months
# Set to None to include all companies regardless of age
RECENT_MONTHS = 24

# Minimum AI/robotics relevance score (number of keyword matches in business purpose)
# Higher score = more relevant to AI/robotics
MIN_RELEVANCE_SCORE = 1

# Filter by status
ALLOWED_STATUSES = ["ACTIVE"]  # Can include: 'ACTIVE', 'LIQUIDATION', 'DELETED', etc.

# Geographic filters (leave empty to include all locations)
# Example: ['München', 'Berlin', 'Hamburg']
CITIES_FILTER = []

# Minimum capital amount (in EUR) - set to 0 or None for no minimum
MIN_CAPITAL_AMOUNT = 0


# =============================================================================
# DATA FEATURES
# =============================================================================

# Which data features to fetch from the API
# More features = more credits consumed per request
# Available features:
# - 'related_persons': Management and shareholders
# - 'publications': Official announcements (for capital raises)
# - 'financial_kpi': Financial metrics (revenue, employees, etc.)
# - 'balance_sheet_accounts': Detailed balance sheet
# - 'profit_and_loss_account': P&L statement
# - 'insolvency_publications': Insolvency notices
# - 'annual_financial_statements': Annual reports

FEATURES = [
    "related_persons",
    "publications",
    "financial_kpi",
]

# Use AI-powered search for better entity matching
USE_AI_SEARCH = True


# =============================================================================
# CAPITAL RAISE DETECTION
# =============================================================================

# Keywords to identify capital raises in publications (German)
CAPITAL_RAISE_KEYWORDS = [
    "kapitalerhöhung",
    "capital increase",
    "stammkapital erhöht",
    "share capital",
    "gesellschafterbeschluss",
    "einzahlung",
    "funding",
    "finanzierung",
    "investment",
]


# =============================================================================
# OUTPUT CONFIGURATION
# =============================================================================

# Output file names
CSV_OUTPUT_FILE = "ai_robotics_startups.csv"
JSON_OUTPUT_FILE = "ai_robotics_startups.json"

# CSV field order and selection
CSV_FIELDS = [
    "name",
    "entity_id",
    "status",
    "registration_date",
    "purpose",
    "capital_amount",
    "capital_currency",
    "management_count",
    "capital_raises_count",
    "ai_robotics_score",
    "website",
    "city",
    "address_full",
]

# Maximum length for text fields in CSV (to avoid huge cells)
CSV_TEXT_TRUNCATE_LENGTH = 200


# =============================================================================
# MONITORING & NOTIFICATIONS
# =============================================================================

# Database file for change tracking (used in advanced_monitoring.py)
DATABASE_FILE = "startups.db"

# Email notifications (placeholder - implement your own)
EMAIL_NOTIFICATIONS = False
EMAIL_TO = "your-email@example.com"
EMAIL_FROM = "scraper@example.com"

# Slack notifications (placeholder - implement your own)
SLACK_NOTIFICATIONS = False
SLACK_WEBHOOK_URL = None


# =============================================================================
# ADVANCED FILTERS
# =============================================================================


def custom_filter(company_analysis: dict) -> bool:
    """
    Custom filter function for advanced filtering logic

    Args:
        company_analysis: Dictionary with analyzed company data

    Returns:
        True to include the company, False to skip it

    Example custom filters:
    - Only companies with websites
    - Only GmbH or AG legal forms
    - Only companies with specific keywords in exact positions
    - Only companies with multiple management members
    """

    # Example: Only include companies with a website
    # if not company_analysis.get('website'):
    #     return False

    # Example: Only include GmbH companies
    # legal_form = company_analysis.get('registration', {}).get('legal_form', '')
    # if 'GmbH' not in legal_form:
    #     return False

    # Example: Only include companies with at least 2 managers
    # if len(company_analysis.get('management', [])) < 2:
    #     return False

    # Default: accept all companies that pass basic filters
    return True


# =============================================================================
# TECHNOLOGY-SPECIFIC FILTERS
# =============================================================================

# Define specific technology categories for classification
TECH_CATEGORIES = {
    "computer_vision": [
        "computer vision",
        "bildverarbeitung",
        "image recognition",
        "objekterkennung",
        "visual ai",
    ],
    "nlp": [
        "natural language processing",
        "sprachverarbeitung",
        "nlp",
        "text analytics",
        "chatbot",
        "conversational ai",
    ],
    "robotics": [
        "robotik",
        "robotics",
        "autonomous",
        "drone",
        "cobots",
        "industrial automation",
    ],
    "predictive_analytics": [
        "predictive analytics",
        "forecasting",
        "prognose",
        "data science",
        "business intelligence",
    ],
}


def classify_company_tech(purpose: str) -> list:
    """
    Classify company by technology categories

    Args:
        purpose: Company business purpose text

    Returns:
        List of matching technology categories
    """
    if not purpose:
        return []

    purpose_lower = purpose.lower()
    matches = []

    for category, keywords in TECH_CATEGORIES.items():
        if any(keyword in purpose_lower for keyword in keywords):
            matches.append(category)

    return matches


# =============================================================================
# EXAMPLE SPECIALIZED CONFIGURATIONS
# =============================================================================

# Uncomment and modify one of these to quickly switch to a specialized search:

# # Focus on Computer Vision startups only
# SEARCH_KEYWORDS = [
#     "computer vision",
#     "bildverarbeitung",
#     "image recognition",
#     "visual ai",
#     "object detection",
# ]
# MIN_RELEVANCE_SCORE = 1

# # Focus on Munich/Berlin tech scene
# CITIES_FILTER = ['München', 'Berlin']
# RECENT_MONTHS = 12

# # Focus on well-funded startups
# MIN_CAPITAL_AMOUNT = 500_000  # €500k minimum
# FEATURES.append('financial_kpi')

# # Focus on recent incorporations only
# RECENT_MONTHS = 6
# MIN_RELEVANCE_SCORE = 2

# # Deep dive with all features (more expensive)
# FEATURES = [
#     'related_persons',
#     'publications',
#     'financial_kpi',
#     'balance_sheet_accounts',
#     'profit_and_loss_account',
# ]
