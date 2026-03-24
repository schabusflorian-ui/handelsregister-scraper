"""
Sample test data for Handelsregister scraper tests.

Contains true positives (companies that should match AI/robotics filter)
and false positives (companies that should NOT match).
"""

# True positives: Companies that SHOULD be identified as AI/robotics
# Format: (name, expected_passes, expected_min_score, expected_categories)
# NOTE: Scores are based on actual filter behavior:
# - Each keyword match: +1 point
# - High-signal keyword: +1 bonus point
# - Standalone AI: +2 points
# "KI" alone was removed from standalone patterns due to false positives
TRUE_POSITIVES = [
    # Core AI companies (standalone AI gets +2 bonus)
    ("DeepMind AI Solutions GmbH", True, 2, ["general_ai"]),
    ("Artificial Intelligence Systems AG", True, 2, ["general_ai"]),
    ("Künstliche Intelligenz Labs GmbH", True, 2, ["general_ai"]),
    # Robotics companies
    ("Robotik Automation Systems UG", True, 2, ["robotics"]),
    ("Industrieroboter Technik GmbH", True, 1, ["robotics"]),
    ("Cobot Solutions GmbH", True, 1, ["robotics"]),
    ("Drone Services Germany UG", True, 1, ["robotics"]),
    # Machine Learning / Deep Learning (high-signal keywords get bonus)
    ("Machine Learning Analytics GmbH", True, 2, ["ml_analytics"]),
    ("Deep Learning Research AG", True, 2, ["ml_analytics"]),
    ("Neural Network Solutions UG", True, 2, ["ml_analytics"]),
    # Computer Vision (high-signal gets bonus)
    ("Computer Vision Tech GmbH", True, 2, ["computer_vision"]),
    ("Bildverarbeitung Systems AG", True, 2, ["computer_vision"]),
    ("Bilderkennung AI UG", True, 3, ["computer_vision"]),
    # Language AI
    # NOTE: "NLP Solutions GmbH" removed — nlp keyword dropped (Neuro-Linguistic Programming FPs)
    ("Chatbot Development AG", True, 2, ["nlp"]),
    ("Sprachverarbeitung Systeme UG", True, 1, ["nlp"]),
    # Autonomous systems (single keyword = score varies by high-signal status)
    ("Autonomes Fahren GmbH", True, 1, ["autonomous_systems"]),
    # Industry 4.0 (single keyword = 1 point unless high-signal)
    ("Industrie 4.0 Solutions GmbH", True, 1, ["industry_40"]),
    ("Smart Factory Systems AG", True, 1, ["industry_40"]),
    ("Digital Twin Analytics UG", True, 1, ["industry_40"]),
    # Generative AI (high-signal gets bonus)
    ("Generative AI Studio GmbH", True, 3, ["generative_ai"]),
    # Compound/Multiple keywords
    ("AI Robotics Machine Learning GmbH", True, 4, ["robotics", "ml_analytics"]),
]

# False positives: Companies that should NOT match (or have very low score)
# Format: (name, should_pass, reason)
FALSE_POSITIVES = [
    # KI prefix false positives (person names)
    ("Kai-Uwe Consulting GmbH", False, "KI in person name 'Kai'"),
    ("Kai Schmidt Immobilien GmbH", False, "KI in person name 'Kai'"),
    ("Kira Modedesign UG", False, "KI in name 'Kira'"),
    # AI substring false positives
    ("HAIR Salon Berlin GmbH", False, "AI in 'HAIR'"),
    ("FAIR Trade Import GmbH", False, "AI in 'FAIR'"),
    ("Thailand Import Export AG", False, "AI in 'Thailand'"),
    ("MAIN Street Retail GmbH", False, "AI in 'MAIN'"),
    # ML false positives (company initials)
    ("ML Schiffsinvest GmbH & Co. KG", False, "ML as company initials"),
    ("M.L. Consulting GmbH", False, "ML as person initials"),
    # Smart false positives (generic usage)
    ("Smart Repair Autoglas GmbH", False, "'Smart' in car repair context"),
    ("Smart Home Elektro GmbH", False, "'Smart' generic usage"),
    ("SmartPhone Reparatur Berlin UG", False, "'Smart' in smartphone"),
    # Hyphenated KI false positives
    ("Hap-Ki-Do Sportverein e.V.", False, "KI in martial arts name"),
    ("Mu-Ki-Va Familienservice GmbH", False, "KI in child service name"),
    ("Ki-Ka Kindermode GmbH", False, "KI in children's brand"),
    # Generic business names
    ("Müller Verwaltungs GmbH", False, "Generic administration company"),
    ("Schmidt & Partner Steuerberatung", False, "Tax consulting"),
    ("Berlin Immobilien Management AG", False, "Real estate"),
    ("Autohaus Premium GmbH", False, "Car dealership"),
    ("Gastro Service Deutschland UG", False, "Catering service"),
    # Substring matches that shouldn't count
    ("Automatik Getriebe Service GmbH", False, "'Automat' in car transmission"),
]

# Edge cases for testing boundary conditions
EDGE_CASES = [
    # Empty/None handling
    ("", 0, "Empty string"),
    (None, 0, "None value"),
    # Very long names
    ("A" * 500 + " AI GmbH", 2, "Very long name with AI"),
    # Special characters
    ("KI & Robotik GmbH", 2, "Ampersand in name"),
    ("AI/ML Solutions GmbH", 2, "Slash in name"),
    ("Künstliche Intelligenz (KI) GmbH", 2, "Parentheses in name"),
    # Unicode/Umlauts
    ("Künstliche Intelligenz GmbH", 2, "German umlaut ü"),
    ("Müller KI-Systeme GmbH", 1, "Umlaut with KI (but KI might be filtered)"),
    # Case variations
    ("ARTIFICIAL INTELLIGENCE GMBH", 2, "All uppercase"),
    ("artificial intelligence gmbh", 2, "All lowercase"),
    ("ArTiFiCiAl InTeLLiGeNcE GmbH", 2, "Mixed case"),
    # Whitespace variations
    ("  AI Solutions  GmbH  ", 2, "Extra whitespace"),
]

# Companies with known scores for regression testing
KNOWN_SCORES = [
    # Company name, expected minimum score, expected categories
    ("Künstliche Intelligenz Deep Learning GmbH", 4, ["general_ai", "ml_analytics"]),
    ("Robotik Computer Vision Systems AG", 4, ["robotics", "computer_vision"]),
    ("Simple Data Analytics UG", 1, []),  # Only matches 'analytics'
]

# Legal form extraction test cases
LEGAL_FORMS = [
    ("Test Company GmbH", "GmbH"),
    ("Example AG", "AG"),
    ("Startup UG (haftungsbeschränkt)", "UG (haftungsbeschränkt)"),
    ("Partner GmbH & Co. KG", "GmbH & Co. KG"),
    ("Verein e.V.", "e.V."),
    ("European SE", "SE"),
    ("No Legal Form Company", None),
]

# Sample companies for database tests
SAMPLE_COMPANIES_DB = [
    {
        "company_number": "HRB12345",
        "name": "AI Startup GmbH",
        "source": "bundesapi",
        "city": "Berlin",
        "ai_robotics_score": 5,
        "startup_score": 7,
        "startup_classification": "startup",
        "current_status": "active",
        "capital_amount": 25000.0,
    },
    {
        "company_number": "HRB12346",
        "name": "Robotics Tech AG",
        "source": "bundesapi",
        "city": "Munich",
        "ai_robotics_score": 4,
        "startup_score": 5,
        "startup_classification": "tech_company",
        "current_status": "active",
        "capital_amount": 100000.0,
    },
    {
        "company_number": "HRB12347",
        "name": "Traditional Consulting GmbH",
        "source": "bundesapi",
        "city": "Hamburg",
        "ai_robotics_score": 0,
        "startup_score": -2,
        "startup_classification": "traditional",
        "current_status": "active",
        "capital_amount": 50000.0,
    },
]
