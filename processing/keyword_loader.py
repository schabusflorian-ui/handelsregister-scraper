"""
Keyword Loader - Load keywords from YAML configuration.

Provides a flexible way to manage keywords for the Handelsregister scraper
without hardcoding them in the source files.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)

# Default config path relative to project root
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config" / "keywords.yaml"


@dataclass
class KeywordCategory:
    """A category of keywords with metadata."""

    name: str
    description: str
    priority: int
    keywords: List[str]


@dataclass
class KeywordConfig:
    """Complete keyword configuration."""

    categories: Dict[str, KeywordCategory]
    exclusions: Dict[str, List[str]]
    search_config: Dict

    def get_keywords_by_priority(self, max_priority: int = 3) -> List[str]:
        """Get all keywords up to the specified priority level."""
        keywords = []
        for cat in self.categories.values():
            if cat.priority <= max_priority:
                keywords.extend(cat.keywords)
        return list(set(keywords))  # Remove duplicates

    def get_high_priority_keywords(self) -> List[str]:
        """Get priority 1 keywords."""
        return self.get_keywords_by_priority(1)

    def get_medium_priority_keywords(self) -> List[str]:
        """Get priority 1 and 2 keywords."""
        return self.get_keywords_by_priority(2)

    def get_all_keywords(self) -> List[str]:
        """Get all keywords regardless of priority."""
        return self.get_keywords_by_priority(3)

    def get_exclusion_patterns(self) -> Set[str]:
        """Get all exclusion patterns as a set."""
        patterns = set()
        for exclusion_list in self.exclusions.values():
            patterns.update(exclusion_list)
        return patterns

    def get_climate_tech_keywords(self) -> List[str]:
        """Get keywords from climate tech categories."""
        keywords = []
        climate_categories = [
            "climate_energy",
            "climate_grid",
            "climate_hydrogen",
            "climate_carbon",
            "climate_cleantech",
            "climate_mobility",
        ]
        for cat_name in climate_categories:
            if cat_name in self.categories:
                keywords.extend(self.categories[cat_name].keywords)
        return list(set(keywords))

    def get_ai_keywords(self) -> List[str]:
        """Get keywords from AI/ML categories."""
        keywords = []
        ai_categories = ["ai_core", "ai_applications", "ai_business"]
        for cat_name in ai_categories:
            if cat_name in self.categories:
                keywords.extend(self.categories[cat_name].keywords)
        return list(set(keywords))

    def get_robotics_keywords(self) -> List[str]:
        """Get keywords from robotics categories."""
        keywords = []
        robotics_categories = ["robotics_core", "robotics_industrial", "robotics_specific"]
        for cat_name in robotics_categories:
            if cat_name in self.categories:
                keywords.extend(self.categories[cat_name].keywords)
        return list(set(keywords))


def load_keywords(config_path: Optional[Path] = None) -> KeywordConfig:
    """
    Load keyword configuration from YAML file.

    Args:
        config_path: Path to YAML config file (uses default if not provided)

    Returns:
        KeywordConfig object with all keywords and settings
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    if not config_path.exists():
        logger.warning("Keywords config not found at %s, using defaults", config_path)
        return _get_default_config()

    try:
        with open(config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        categories = {}
        for cat_name, cat_data in data.get("categories", {}).items():
            categories[cat_name] = KeywordCategory(
                name=cat_name,
                description=cat_data.get("description", ""),
                priority=cat_data.get("priority", 2),
                keywords=cat_data.get("keywords", []),
            )

        return KeywordConfig(
            categories=categories,
            exclusions=data.get("exclusions", {}),
            search_config=data.get("search_config", {}),
        )

    except Exception as e:
        logger.error("Error loading keywords config: %s", e)
        return _get_default_config()


def _get_default_config() -> KeywordConfig:
    """Return default keyword configuration (fallback)."""
    return KeywordConfig(
        categories={
            "ai_core": KeywordCategory(
                name="ai_core",
                description="Core AI terms",
                priority=1,
                keywords=[
                    "künstliche intelligenz",
                    "artificial intelligence",
                    "machine learning",
                    "deep learning",
                    "robotik",
                    "robotics",
                ],
            ),
        },
        exclusions={
            "false_positives": ["hap-ki-do", "reiki"],
            "traditional_business": ["verwaltung", "immobilien"],
        },
        search_config={
            "max_results_per_query": 100,
            "search_all_states": True,
        },
    )


def get_keywords_for_discovery() -> List[str]:
    """Get keywords suitable for the discovery job."""
    config = load_keywords()
    return config.get_medium_priority_keywords()


def get_keywords_for_backfill() -> List[str]:
    """Get all keywords for comprehensive backfill."""
    config = load_keywords()
    return config.get_all_keywords()


def get_climate_tech_keywords() -> List[str]:
    """Get climate tech keywords."""
    config = load_keywords()
    return config.get_climate_tech_keywords()


# CLI helper
def print_keyword_summary():
    """Print summary of loaded keywords."""
    config = load_keywords()

    print("Keyword Configuration Summary")
    print("=" * 60)

    total_keywords = 0
    for cat_name, category in sorted(config.categories.items()):
        count = len(category.keywords)
        total_keywords += count
        priority_label = ["", "HIGH", "MEDIUM", "LOW"][category.priority]
        print(f"  {cat_name}: {count} keywords [{priority_label}]")

    print(f"\nTotal unique keywords: {len(config.get_all_keywords())}")
    print(f"High priority: {len(config.get_high_priority_keywords())}")
    print(f"Climate tech: {len(config.get_climate_tech_keywords())}")
    print(f"AI/ML: {len(config.get_ai_keywords())}")
    print(f"Robotics: {len(config.get_robotics_keywords())}")


if __name__ == "__main__":
    print_keyword_summary()
