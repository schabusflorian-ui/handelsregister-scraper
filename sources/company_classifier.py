"""
Company Classifier - Categorize companies by sector/vertical.

Uses keyword matching and heuristics to classify companies
into sectors based on name, description, and other metadata.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Sector definitions with keywords
SECTORS = {
    "fintech": {
        "keywords": [
            "fintech",
            "payment",
            "banking",
            "bank",
            "finance",
            "financial",
            "insurance",
            "insurtech",
            "lending",
            "credit",
            "loan",
            "invest",
            "trading",
            "crypto",
            "blockchain",
            "wallet",
            "neobank",
            "regtech",
            "wealthtech",
            "paytech",
            "money",
            "capital",
            "fund",
            "asset",
        ],
        "companies": ["n26", "klarna", "revolut", "wise", "stripe", "bitpanda"],
        "description": "Financial technology and services",
    },
    "saas": {
        "keywords": [
            "saas",
            "software",
            "platform",
            "cloud",
            "automation",
            "workflow",
            "productivity",
            "collaboration",
            "enterprise",
            "b2b",
            "crm",
            "erp",
            "analytics",
            "dashboard",
            "api",
            "integration",
            "tool",
            "solution",
        ],
        "companies": ["personio", "celonis", "contentful", "notion"],
        "description": "Software as a Service products",
    },
    "ecommerce": {
        "keywords": [
            "ecommerce",
            "e-commerce",
            "shop",
            "store",
            "retail",
            "marketplace",
            "commerce",
            "shopping",
            "d2c",
            "direct to consumer",
            "brand",
            "fashion",
            "apparel",
            "beauty",
            "consumer",
            "cpg",
        ],
        "companies": ["zalando", "about you", "mytheresa"],
        "description": "Online retail and marketplaces",
    },
    "healthtech": {
        "keywords": [
            "health",
            "healthcare",
            "medical",
            "medicine",
            "pharma",
            "biotech",
            "digital health",
            "telehealth",
            "telemedicine",
            "wellness",
            "fitness",
            "mental health",
            "therapy",
            "clinic",
            "hospital",
            "patient",
            "doctor",
            "diagnostics",
            "drug",
            "clinical",
            "life science",
        ],
        "companies": ["doctolib", "kry", "ada health"],
        "description": "Healthcare and medical technology",
    },
    "proptech": {
        "keywords": [
            "proptech",
            "real estate",
            "property",
            "housing",
            "home",
            "rent",
            "rental",
            "immobilien",
            "wohnung",
            "building",
            "construction",
            "architecture",
            "facility",
            "smart home",
            "iot home",
        ],
        "companies": ["mcmakler", "homeday", "immoscout"],
        "description": "Real estate technology",
    },
    "mobility": {
        "keywords": [
            "mobility",
            "transport",
            "transportation",
            "automotive",
            "car",
            "vehicle",
            "ev",
            "electric vehicle",
            "scooter",
            "bike",
            "bicycle",
            "ride",
            "sharing",
            "fleet",
            "logistics",
            "delivery",
            "last mile",
            "shipping",
            "freight",
            "trucking",
            "aviation",
            "drone",
        ],
        "companies": ["flixbus", "tier", "lime", "getaround"],
        "description": "Transportation and mobility services",
    },
    "foodtech": {
        "keywords": [
            "food",
            "foodtech",
            "delivery",
            "restaurant",
            "meal",
            "grocery",
            "kitchen",
            "cooking",
            "recipe",
            "nutrition",
            "diet",
            "agtech",
            "agriculture",
            "farming",
            "sustainable food",
            "plant-based",
            "alternative protein",
            "vertical farming",
        ],
        "companies": ["delivery hero", "gorillas", "flink", "hellofresh"],
        "description": "Food delivery and food technology",
    },
    "edtech": {
        "keywords": [
            "education",
            "edtech",
            "learning",
            "training",
            "course",
            "school",
            "university",
            "student",
            "teacher",
            "tutor",
            "skill",
            "upskill",
            "reskill",
            "bootcamp",
            "certification",
            "e-learning",
            "mooc",
        ],
        "companies": ["coursera", "duolingo", "babbel"],
        "description": "Education technology",
    },
    "hrtech": {
        "keywords": [
            "hr",
            "human resources",
            "hiring",
            "recruiting",
            "recruitment",
            "talent",
            "workforce",
            "employee",
            "payroll",
            "benefits",
            "culture",
            "engagement",
            "performance",
            "onboarding",
            "staffing",
            "job",
        ],
        "companies": ["personio", "workday", "greenhouse"],
        "description": "Human resources technology",
    },
    "ai_ml": {
        "keywords": [
            "ai",
            "artificial intelligence",
            "machine learning",
            "ml",
            "deep learning",
            "neural",
            "nlp",
            "natural language",
            "computer vision",
            "robotics",
            "automation",
            "intelligent",
            "predictive",
            "generative",
            "llm",
            "gpt",
            "chatbot",
            "voice",
            "speech",
        ],
        "companies": ["deepmind", "openai", "anthropic", "aleph alpha"],
        "description": "Artificial Intelligence and Machine Learning",
    },
    "cybersecurity": {
        "keywords": [
            "security",
            "cybersecurity",
            "cyber",
            "infosec",
            "privacy",
            "encryption",
            "identity",
            "authentication",
            "fraud",
            "compliance",
            "risk",
            "threat",
            "vulnerability",
            "penetration",
            "soc",
            "siem",
        ],
        "companies": ["snyk", "crowdstrike", "sentinelone"],
        "description": "Cybersecurity and data protection",
    },
    "cleantech": {
        "keywords": [
            "clean",
            "cleantech",
            "climate",
            "sustainability",
            "sustainable",
            "green",
            "renewable",
            "energy",
            "solar",
            "wind",
            "battery",
            "carbon",
            "emission",
            "environment",
            "recycling",
            "circular",
            "waste",
            "water",
            "hydrogen",
            "ev charging",
        ],
        "companies": ["northvolt", "lilium", "enpal"],
        "description": "Clean technology and sustainability",
    },
    "gaming": {
        "keywords": [
            "gaming",
            "game",
            "esports",
            "metaverse",
            "virtual reality",
            "vr",
            "augmented reality",
            "ar",
            "entertainment",
            "streaming",
            "content",
            "creator",
            "media",
            "video",
            "music",
            "podcast",
        ],
        "companies": ["wooga", "innogames", "goodgame"],
        "description": "Gaming and entertainment",
    },
    "legaltech": {
        "keywords": [
            "legal",
            "legaltech",
            "law",
            "lawyer",
            "contract",
            "compliance",
            "regulatory",
            "notary",
            "dispute",
            "litigation",
            "ip",
            "patent",
        ],
        "companies": ["lexoffice", "lawgeex"],
        "description": "Legal technology",
    },
    "marketingtech": {
        "keywords": [
            "marketing",
            "martech",
            "advertising",
            "adtech",
            "seo",
            "sem",
            "social media",
            "influencer",
            "content marketing",
            "email",
            "crm",
            "customer",
            "engagement",
            "loyalty",
            "personalization",
        ],
        "companies": ["emarsys", "adjust", "braze"],
        "description": "Marketing technology",
    },
    "devtools": {
        "keywords": [
            "developer",
            "devtools",
            "infrastructure",
            "devops",
            "ci/cd",
            "monitoring",
            "observability",
            "testing",
            "code",
            "git",
            "api",
            "database",
            "serverless",
            "container",
            "kubernetes",
            "open source",
        ],
        "companies": ["gitlab", "datadog", "hashicorp"],
        "description": "Developer tools and infrastructure",
    },
}

# B2B vs B2C indicators
B2B_KEYWORDS = [
    "enterprise",
    "b2b",
    "business",
    "corporate",
    "saas",
    "platform",
    "solution",
    "service provider",
    "consultant",
    "agency",
]

B2C_KEYWORDS = [
    "consumer",
    "b2c",
    "app",
    "personal",
    "individual",
    "user",
    "customer",
    "retail",
    "shop",
    "marketplace",
]

# Stage indicators from company names/descriptions
EARLY_STAGE_INDICATORS = [
    "stealth",
    "beta",
    "alpha",
    "launching",
    "pre-launch",
    "coming soon",
    "mvp",
    "prototype",
    "early stage",
    "seed",
    "pre-seed",
]


@dataclass
class CompanyCategory:
    """Category classification for a company."""

    primary_sector: Optional[str] = None
    secondary_sectors: List[str] = field(default_factory=list)
    sector_confidence: float = 0.0
    is_b2b: bool = False
    is_b2c: bool = False
    business_model: Optional[str] = None  # 'b2b', 'b2c', 'b2b2c', 'marketplace'
    stage_signals: List[str] = field(default_factory=list)
    keywords_matched: Dict[str, List[str]] = field(default_factory=dict)


def classify_company(
    name: str,
    description: Optional[str] = None,
    website: Optional[str] = None,
    source: Optional[str] = None,
) -> CompanyCategory:
    """
    Classify a company into sectors based on available data.

    Args:
        name: Company name
        description: Company description/tagline
        website: Company website URL
        source: Source of the data (e.g., 'Speedinvest', 'YC')

    Returns:
        CompanyCategory with classification results
    """
    category = CompanyCategory()

    # Combine all text for analysis
    text_parts = [name.lower()]
    if description:
        text_parts.append(description.lower())
    if website:
        text_parts.append(website.lower())

    combined_text = " ".join(text_parts)

    # Score each sector
    sector_scores: Dict[str, Tuple[int, List[str]]] = {}

    for sector, config in SECTORS.items():
        score = 0
        matched = []

        # Check keywords
        for keyword in config["keywords"]:
            if keyword in combined_text:
                score += 1
                matched.append(keyword)

        # Check known companies
        for company in config.get("companies", []):
            if company in combined_text:
                score += 3  # Strong signal
                matched.append(f"company:{company}")

        if score > 0:
            sector_scores[sector] = (score, matched)

    # Determine primary and secondary sectors
    if sector_scores:
        sorted_sectors = sorted(sector_scores.items(), key=lambda x: x[1][0], reverse=True)

        # Primary sector
        category.primary_sector = sorted_sectors[0][0]
        category.keywords_matched[sorted_sectors[0][0]] = sorted_sectors[0][1][1]

        # Calculate confidence (normalized by number of keywords)
        max_score = sorted_sectors[0][1][0]
        category.sector_confidence = min(1.0, max_score / 5.0)  # 5+ matches = 100%

        # Secondary sectors (score >= 2)
        for sector, (score, matched) in sorted_sectors[1:]:
            if score >= 2:
                category.secondary_sectors.append(sector)
                category.keywords_matched[sector] = matched

    # B2B vs B2C classification
    b2b_score = sum(1 for k in B2B_KEYWORDS if k in combined_text)
    b2c_score = sum(1 for k in B2C_KEYWORDS if k in combined_text)

    category.is_b2b = b2b_score >= 2
    category.is_b2c = b2c_score >= 2

    if category.is_b2b and category.is_b2c:
        category.business_model = "b2b2c"
    elif category.is_b2b:
        category.business_model = "b2b"
    elif category.is_b2c:
        category.business_model = "b2c"

    # Check for early stage signals
    for signal in EARLY_STAGE_INDICATORS:
        if signal in combined_text:
            category.stage_signals.append(signal)

    return category


def classify_companies_batch(companies: List[Dict]) -> List[Tuple[Dict, CompanyCategory]]:
    """
    Classify a batch of companies.

    Args:
        companies: List of company dicts with 'name', 'description', etc.

    Returns:
        List of (company, category) tuples
    """
    results = []
    for company in companies:
        category = classify_company(
            name=company.get("name", ""),
            description=company.get("description"),
            website=company.get("website"),
            source=company.get("source"),
        )
        results.append((company, category))
    return results


def update_company_categories(db, batch_size: int = 100):
    """
    Update category tags for all portfolio companies in database.

    Args:
        db: Database connection
        batch_size: Number of companies to process at once
    """
    cursor = db.conn.cursor()

    # Add category columns if they don't exist
    try:
        cursor.execute("ALTER TABLE portfolio_companies ADD COLUMN primary_sector TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE portfolio_companies ADD COLUMN secondary_sectors TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE portfolio_companies ADD COLUMN business_model TEXT")
    except:
        pass
    try:
        cursor.execute("ALTER TABLE portfolio_companies ADD COLUMN sector_confidence REAL")
    except:
        pass

    # Get all companies
    cursor.execute("SELECT id, name, description, website, source FROM portfolio_companies")
    companies = cursor.fetchall()

    updated = 0
    for id, name, description, website, source in companies:
        category = classify_company(name, description, website, source)

        cursor.execute(
            """
            UPDATE portfolio_companies
            SET primary_sector = ?,
                secondary_sectors = ?,
                business_model = ?,
                sector_confidence = ?
            WHERE id = ?
        """,
            (
                category.primary_sector,
                ",".join(category.secondary_sectors) if category.secondary_sectors else None,
                category.business_model,
                category.sector_confidence,
                id,
            ),
        )
        updated += 1

    db.conn.commit()
    logger.info(f"Updated categories for {updated} companies")
    return updated


def get_sector_stats(db) -> Dict[str, int]:
    """Get count of companies per sector."""
    cursor = db.conn.cursor()
    cursor.execute("""
        SELECT primary_sector, COUNT(*)
        FROM portfolio_companies
        WHERE primary_sector IS NOT NULL
        GROUP BY primary_sector
        ORDER BY COUNT(*) DESC
    """)
    return dict(cursor.fetchall())


def search_by_sector(db, sector: str, limit: int = 50) -> List[Dict]:
    """Search for companies in a specific sector."""
    cursor = db.conn.cursor()
    cursor.execute(
        """
        SELECT name, description, website, source, primary_sector, business_model
        FROM portfolio_companies
        WHERE primary_sector = ? OR secondary_sectors LIKE ?
        ORDER BY sector_confidence DESC
        LIMIT ?
    """,
        (sector, f"%{sector}%", limit),
    )

    columns = ["name", "description", "website", "source", "primary_sector", "business_model"]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


if __name__ == "__main__":
    from persistence.database import Database

    logging.basicConfig(level=logging.INFO)

    db = Database("handelsregister.db")

    print("Updating company categories...")
    updated = update_company_categories(db)
    print(f"Updated {updated} companies\n")

    print("Sector distribution:")
    stats = get_sector_stats(db)
    for sector, count in stats.items():
        print(f"  {sector}: {count}")

    db.close()
