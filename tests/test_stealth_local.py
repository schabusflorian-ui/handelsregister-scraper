#!/usr/bin/env python3
"""
Test script for stealth founder discovery.
Run this locally on a machine with normal internet access.

Usage:
    python test_stealth_local.py

Requirements:
    pip install cloudscraper beautifulsoup4 requests
"""

import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)

logger = logging.getLogger(__name__)


def test_duckduckgo_search():
    """Test DuckDuckGo search for LinkedIn profiles."""
    print("\n" + "=" * 60)
    print("TEST 1: DuckDuckGo Search")
    print("=" * 60)

    from sources.google_search import DuckDuckGoSearchScraper

    scraper = DuckDuckGoSearchScraper(
        delay_range=(3, 6),
        use_cloudscraper=True,
    )

    # Run a single query
    results = scraper.search_query('linkedin.com/in stealth founder berlin')

    print(f"\nFound {len(results)} LinkedIn profiles:")
    for r in results[:5]:
        print(f"  - {r.title}")
        print(f"    URL: {r.url}")

    return results


def test_linkedin_scraping(urls: list):
    """Test LinkedIn profile scraping."""
    print("\n" + "=" * 60)
    print("TEST 2: LinkedIn Profile Scraping")
    print("=" * 60)

    if not urls:
        print("No URLs to test. Using sample URL...")
        urls = ['https://www.linkedin.com/in/satyanadella']

    from sources.linkedin_scraper import LinkedInProfileScraper

    scraper = LinkedInProfileScraper(
        delay_range=(3, 6),
        use_cloudscraper=True,
    )

    profiles = []
    for url in urls[:3]:  # Test first 3
        print(f"\nScraping: {url}")
        profile = scraper.scrape_profile(url)

        if profile:
            print(f"  Name: {profile.name}")
            print(f"  Headline: {profile.headline}")
            print(f"  Location: {profile.location}")
            print(f"  Signals: {profile.stealth_signals}")
            print(f"  Confidence: {profile.confidence_score:.2f}")
            profiles.append(profile)
        else:
            print("  -> Failed to scrape")

    return profiles


def test_full_discovery():
    """Test full stealth founder discovery pipeline."""
    print("\n" + "=" * 60)
    print("TEST 3: Full Discovery Pipeline")
    print("=" * 60)

    from persistence.database import Database
    from scheduler.jobs.stealth_founder_job import StealthFounderJob

    db = Database('handelsregister.db')
    try:
        job = StealthFounderJob(
            db=db,
            max_queries=2,
            max_profiles_to_scrape=5,
            min_confidence=0.1,
            google_delay=(5, 10),
            linkedin_delay=(3, 6),
        )

        stats = job.run()

        print("\nResults:")
        for k, v in stats.items():
            print(f"  {k}: {v}")

        # Show found founders
        founders = job.get_top_founders(limit=10)
        if founders:
            print("\nTop founders found:")
            for f in founders:
                print(f"\n  {f['name']} (conf={f['confidence']:.2f})")
                print(f"    {f['headline']}")
                print(f"    {f['url']}")

        return stats

    finally:
        db.close()


def test_manual_import():
    """Test manual URL import (bypasses search)."""
    print("\n" + "=" * 60)
    print("TEST 4: Manual URL Import")
    print("=" * 60)

    # Add your own LinkedIn URLs here
    sample_urls = [
        # 'https://www.linkedin.com/in/your-target-profile',
    ]

    if not sample_urls:
        print("Add LinkedIn URLs to test manual import.")
        print("Edit this file and add URLs to sample_urls list.")
        return None

    from scheduler.jobs.stealth_founder_job import import_and_scrape_urls

    stats = import_and_scrape_urls(
        urls=sample_urls,
        db_path='handelsregister.db',
        min_confidence=0.1,
    )

    print("\nResults:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    return stats


if __name__ == '__main__':
    print("=" * 60)
    print("STEALTH FOUNDER DISCOVERY - LOCAL TEST")
    print("=" * 60)

    # Test 1: DuckDuckGo Search
    try:
        search_results = test_duckduckgo_search()
    except Exception as e:
        print(f"Search test failed: {e}")
        search_results = []

    # Test 2: LinkedIn Scraping
    try:
        urls = [r.url for r in search_results] if search_results else []
        profiles = test_linkedin_scraping(urls)
    except Exception as e:
        print(f"Scraping test failed: {e}")
        profiles = []

    # Test 3: Full Pipeline (optional - uncomment to run)
    # try:
    #     stats = test_full_discovery()
    # except Exception as e:
    #     print(f"Full discovery test failed: {e}")

    print("\n" + "=" * 60)
    print("TESTS COMPLETE")
    print("=" * 60)
    print(f"Search results: {len(search_results)}")
    print(f"Profiles scraped: {len(profiles)}")
