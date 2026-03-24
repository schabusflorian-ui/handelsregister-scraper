"""
Handelsregister Scraper POC - AI/Robotics Startup Tracker

This scraper finds new AI and robotics startups in the German Handelsregister by:
1. Searching for companies with AI/robotics keywords in their business purpose
2. Filtering for recent incorporations
3. Detecting capital raises from publications
4. Extracting relevant data (shareholders, capital, management)
"""

import os
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import csv

# Note: Install with: pip install handelsregister
try:
    from handelsregister import Handelsregister, Company
except ImportError:
    print("Please install: pip install handelsregister")
    exit(1)


class HandelsregisterAIScraper:
    """Scraper for finding AI/robotics startups in the German Handelsregister"""
    
    # Keywords to identify AI and robotics companies (in German)
    AI_ROBOTICS_KEYWORDS = [
        "künstliche intelligenz",
        "artificial intelligence",
        "AI",
        "KI",
        "machine learning",
        "maschinelles lernen",
        "deep learning",
        "neural",
        "robotik",
        "robotics",
        "roboter",
        "automation",
        "autonome systeme",
        "autonomous",
        "computer vision",
        "bildverarbeitung",
        "natural language processing",
        "sprachverarbeitung",
        "chatbot",
        "predictive analytics",
    ]
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the scraper
        
        Args:
            api_key: handelsregister.ai API key (or set HANDELSREGISTER_API_KEY env var)
        """
        self.client = Handelsregister(api_key=api_key)
        self.results = []
        
    def search_by_keyword(self, keyword: str, max_results: int = 50) -> List[Dict]:
        """
        Search for companies by keyword in business purpose
        
        Args:
            keyword: Search term
            max_results: Maximum number of results to return
            
        Returns:
            List of company dictionaries
        """
        print(f"Searching for companies with keyword: '{keyword}'...")
        
        try:
            # Search with the keyword
            # Note: The actual search implementation depends on handelsregister.ai's search endpoint
            # For now, we'll use fetch_organization with the query parameter
            results = self.client.search_organizations(
                q=keyword,
                limit=max_results
            )
            
            print(f"  Found {len(results.get('results', []))} results")
            return results.get('results', [])
            
        except Exception as e:
            print(f"  Error searching for '{keyword}': {str(e)}")
            return []
    
    def fetch_company_details(self, company_name: str, location: str = "") -> Optional[Dict]:
        """
        Fetch detailed company information including publications
        
        Args:
            company_name: Name of the company
            location: Optional location filter
            
        Returns:
            Dictionary with company details or None if not found
        """
        try:
            query = f"{company_name} {location}".strip()
            
            # Fetch with all relevant features
            company_data = self.client.fetch_organization(
                q=query,
                features=[
                    "related_persons",              # Management/shareholders
                    "publications",                 # For capital raises/announcements
                    "financial_kpi",               # Financial data
                    "balance_sheet_accounts",      # Balance sheet
                ],
                ai_search="on"  # Use AI for better matching
            )
            
            return company_data
            
        except Exception as e:
            print(f"  Error fetching details for '{company_name}': {str(e)}")
            return None
    
    def extract_capital_raises(self, publications: List[Dict]) -> List[Dict]:
        """
        Extract capital raise events from publications
        
        Args:
            publications: List of publication dictionaries
            
        Returns:
            List of capital raise events
        """
        capital_raises = []
        
        # Keywords indicating capital raises
        capital_keywords = [
            "kapitalerhöhung",
            "capital increase",
            "stammkapital",
            "share capital",
            "gesellschafterbeschluss",
        ]
        
        for pub in publications:
            pub_text = pub.get('text', '').lower()
            
            # Check if publication mentions capital raise
            if any(keyword in pub_text for keyword in capital_keywords):
                capital_raises.append({
                    'date': pub.get('date'),
                    'type': pub.get('type'),
                    'text': pub.get('text'),
                    'source': pub.get('source'),
                })
        
        return capital_raises
    
    def is_recent_incorporation(self, registration_date: str, months: int = 24) -> bool:
        """
        Check if company was incorporated recently
        
        Args:
            registration_date: Registration date string (ISO format)
            months: Number of months to consider as "recent"
            
        Returns:
            True if incorporated within the last N months
        """
        if not registration_date:
            return False
        
        try:
            reg_date = datetime.fromisoformat(registration_date.replace('Z', '+00:00'))
            cutoff_date = datetime.now() - timedelta(days=months * 30)
            return reg_date >= cutoff_date
        except:
            return False
    
    def analyze_company(self, company_data: Dict) -> Dict:
        """
        Analyze company data and extract relevant information
        
        Args:
            company_data: Raw company data from API
            
        Returns:
            Structured analysis
        """
        analysis = {
            'name': company_data.get('name'),
            'entity_id': company_data.get('entity_id'),
            'status': company_data.get('status'),
            'purpose': company_data.get('purpose'),
            'registration': company_data.get('registration', {}),
            'registration_date': company_data.get('registration', {}).get('date'),
            'address': company_data.get('address'),
            'website': company_data.get('website'),
            'capital': {},
            'management': [],
            'capital_raises': [],
            'is_recent': False,
            'ai_robotics_score': 0,
        }
        
        # Extract capital information
        if 'capital' in company_data:
            analysis['capital'] = company_data['capital']
        
        # Extract management/shareholders
        related_persons = company_data.get('related_persons', [])
        for person in related_persons:
            if person.get('is_current'):
                analysis['management'].append({
                    'name': person.get('name'),
                    'role': person.get('role', {}).get('de', {}).get('long', 'Unknown'),
                    'start_date': person.get('start_date'),
                })
        
        # Extract capital raises from publications
        publications = company_data.get('publications', [])
        if publications:
            analysis['capital_raises'] = self.extract_capital_raises(publications)
        
        # Check if recent incorporation
        analysis['is_recent'] = self.is_recent_incorporation(analysis['registration_date'])
        
        # Calculate AI/robotics relevance score
        purpose_text = (analysis['purpose'] or '').lower()
        analysis['ai_robotics_score'] = sum(
            1 for keyword in self.AI_ROBOTICS_KEYWORDS 
            if keyword.lower() in purpose_text
        )
        
        return analysis
    
    def scrape_ai_robotics_startups(
        self,
        keywords: Optional[List[str]] = None,
        recent_months: int = 24,
        min_relevance_score: int = 1,
    ) -> List[Dict]:
        """
        Main scraping method to find AI/robotics startups
        
        Args:
            keywords: List of keywords to search (uses defaults if None)
            recent_months: Only include companies incorporated in last N months
            min_relevance_score: Minimum AI/robotics relevance score
            
        Returns:
            List of analyzed company dictionaries
        """
        if keywords is None:
            # Use a subset of highly relevant keywords to avoid too many API calls
            keywords = [
                "künstliche intelligenz",
                "artificial intelligence", 
                "robotik",
                "machine learning",
                "autonomous",
            ]
        
        results = []
        seen_entities = set()
        
        for keyword in keywords:
            # Search for companies
            companies = self.search_by_keyword(keyword, max_results=20)
            
            for company_summary in companies:
                entity_id = company_summary.get('entity_id')
                
                # Skip if already processed
                if entity_id in seen_entities:
                    continue
                seen_entities.add(entity_id)
                
                # Fetch full details
                company_name = company_summary.get('name', '')
                print(f"\nFetching details for: {company_name}")
                
                company_data = self.fetch_company_details(company_name)
                
                if not company_data:
                    continue
                
                # Analyze the company
                analysis = self.analyze_company(company_data)
                
                # Apply filters
                if analysis['ai_robotics_score'] < min_relevance_score:
                    print(f"  Skipped: Low relevance score ({analysis['ai_robotics_score']})")
                    continue
                
                if recent_months and not analysis['is_recent']:
                    print(f"  Skipped: Not a recent incorporation")
                    continue
                
                print(f"  ✓ Match! Score: {analysis['ai_robotics_score']}, Capital raises: {len(analysis['capital_raises'])}")
                results.append(analysis)
                
                # Rate limiting - be nice to the API
                time.sleep(1)
        
        return results
    
    def export_to_csv(self, results: List[Dict], filename: str):
        """Export results to CSV file"""
        if not results:
            print("No results to export")
            return
        
        with open(filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'name', 'entity_id', 'status', 'registration_date', 'purpose',
                'capital_amount', 'capital_currency', 'management_count',
                'capital_raises_count', 'ai_robotics_score', 'website',
                'city', 'address_full'
            ])
            
            writer.writeheader()
            
            for result in results:
                writer.writerow({
                    'name': result['name'],
                    'entity_id': result['entity_id'],
                    'status': result['status'],
                    'registration_date': result['registration_date'],
                    'purpose': (result['purpose'] or '')[:200],  # Truncate
                    'capital_amount': result['capital'].get('amount', ''),
                    'capital_currency': result['capital'].get('currency', ''),
                    'management_count': len(result['management']),
                    'capital_raises_count': len(result['capital_raises']),
                    'ai_robotics_score': result['ai_robotics_score'],
                    'website': result.get('website', ''),
                    'city': result.get('address', {}).get('city', ''),
                    'address_full': self._format_address(result.get('address', {})),
                })
        
        print(f"\n✓ Exported {len(results)} results to {filename}")
    
    def _format_address(self, address: Dict) -> str:
        """Format address dictionary to string"""
        parts = [
            address.get('street', ''),
            address.get('house_number', ''),
            address.get('zip_code', ''),
            address.get('city', ''),
        ]
        return ', '.join(filter(None, parts))
    
    def export_to_json(self, results: List[Dict], filename: str):
        """Export results to JSON file"""
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        
        print(f"✓ Exported {len(results)} results to {filename}")


def main():
    """Main execution function"""
    
    # Check for API key
    api_key = os.getenv('HANDELSREGISTER_API_KEY')
    if not api_key:
        print("ERROR: Please set HANDELSREGISTER_API_KEY environment variable")
        print("Get your API key from: https://handelsregister.ai")
        return
    
    # Initialize scraper
    scraper = HandelsregisterAIScraper(api_key=api_key)
    
    print("=" * 70)
    print("Handelsregister AI/Robotics Startup Scraper - POC")
    print("=" * 70)
    print()
    
    # Run the scraper
    results = scraper.scrape_ai_robotics_startups(
        keywords=[
            "künstliche intelligenz",
            "robotik",
            "machine learning",
        ],
        recent_months=24,  # Last 2 years
        min_relevance_score=1,  # At least 1 keyword match
    )
    
    print("\n" + "=" * 70)
    print(f"Found {len(results)} AI/Robotics startups")
    print("=" * 70)
    
    # Display summary
    for i, company in enumerate(results, 1):
        print(f"\n{i}. {company['name']}")
        print(f"   Status: {company['status']}")
        print(f"   Incorporated: {company['registration_date']}")
        print(f"   Purpose: {(company['purpose'] or '')[:100]}...")
        print(f"   Capital: {company['capital'].get('amount', 'N/A')} {company['capital'].get('currency', '')}")
        print(f"   Management: {len(company['management'])} person(s)")
        print(f"   Capital Raises: {len(company['capital_raises'])}")
        print(f"   Relevance Score: {company['ai_robotics_score']}")
    
    # Export results
    if results:
        scraper.export_to_csv(results, 'ai_robotics_startups.csv')
        scraper.export_to_json(results, 'ai_robotics_startups.json')
        
        print("\n✓ Scraping complete!")
        print(f"  - CSV: ai_robotics_startups.csv")
        print(f"  - JSON: ai_robotics_startups.json")


if __name__ == "__main__":
    main()
