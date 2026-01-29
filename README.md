# Handelsregister AI/Robotics Startup Scraper - POC

A proof-of-concept scraper that identifies new AI and robotics startups in the German Handelsregister (Commercial Register) by monitoring:
- New company incorporations
- Capital raises and funding rounds
- Business purposes containing AI/robotics keywords
- Management changes and shareholder information

## Features

✅ **Keyword-based Discovery**: Searches for companies using AI/robotics keywords in German and English  
✅ **Recent Incorporations**: Filters for companies incorporated in the last N months  
✅ **Capital Raise Detection**: Identifies capital increases from official publications  
✅ **Detailed Company Data**: Extracts management, shareholders, financial KPIs, and more  
✅ **Export Capabilities**: Saves results to CSV and JSON formats  
✅ **Rate Limiting**: Respects API limits with built-in delays  

## Setup

### 1. Get an API Key

Sign up for a free account at [handelsregister.ai](https://handelsregister.ai) to get your API key.

The free tier includes:
- Limited credits for testing
- Access to all endpoints
- Real-time data from the official Handelsregister

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

This will install:
- `handelsregister` - Official Python SDK for handelsregister.ai
- `rich` - Enhanced CLI output (optional)
- `python-dateutil` - Date handling utilities

### 3. Set API Key

```bash
export HANDELSREGISTER_API_KEY="your_api_key_here"
```

Or add it to your `.bashrc`/`.zshrc`:
```bash
echo 'export HANDELSREGISTER_API_KEY="your_api_key_here"' >> ~/.bashrc
source ~/.bashrc
```

## Usage

### Basic Usage

Run the scraper with default settings:

```bash
python handelsregister_scraper.py
```

This will:
1. Search for companies with AI/robotics keywords
2. Filter for incorporations in the last 24 months
3. Analyze each company's purpose, capital, and publications
4. Export results to `ai_robotics_startups.csv` and `ai_robotics_startups.json`

### Customize the Search

Edit the script to modify search parameters:

```python
results = scraper.scrape_ai_robotics_startups(
    keywords=[
        "künstliche intelligenz",  # Add/remove keywords
        "robotik",
        "machine learning",
        "computer vision",
    ],
    recent_months=12,           # Only last 12 months
    min_relevance_score=2,      # Require at least 2 keyword matches
)
```

### Available Keywords

The scraper includes these German and English keywords:
- künstliche intelligenz, artificial intelligence, AI, KI
- machine learning, maschinelles lernen, deep learning
- neural, robotik, robotics, roboter
- automation, autonome systeme, autonomous
- computer vision, bildverarbeitung
- natural language processing, sprachverarbeitung
- chatbot, predictive analytics

## Output

### CSV Format
`ai_robotics_startups.csv` contains:
- Company name and entity ID
- Registration date and status
- Business purpose (truncated to 200 chars)
- Capital amount and currency
- Number of management members
- Number of capital raises detected
- AI/robotics relevance score
- Website and full address

### JSON Format
`ai_robotics_startups.json` contains the full structured data:
```json
{
  "name": "Example AI GmbH",
  "entity_id": "abc123...",
  "status": "ACTIVE",
  "purpose": "Entwicklung von KI-basierten Lösungen...",
  "registration_date": "2024-03-15",
  "capital": {
    "amount": 25000,
    "currency": "EUR"
  },
  "management": [
    {
      "name": "Max Mustermann",
      "role": "Geschäftsführer",
      "start_date": "2024-03-15"
    }
  ],
  "capital_raises": [
    {
      "date": "2024-09-20",
      "type": "Kapitalerhöhung",
      "text": "Beschluss über Erhöhung des Stammkapitals..."
    }
  ],
  "ai_robotics_score": 3,
  "is_recent": true
}
```

## Code Structure

### Main Components

**`HandelsregisterAIScraper`** - Core scraper class with methods:
- `search_by_keyword()` - Search companies by keyword
- `fetch_company_details()` - Get full company data with all features
- `extract_capital_raises()` - Parse publications for funding events
- `is_recent_incorporation()` - Filter by registration date
- `analyze_company()` - Extract and structure relevant data
- `scrape_ai_robotics_startups()` - Main orchestration method
- `export_to_csv()` / `export_to_json()` - Data export

### How It Works

1. **Keyword Search**: Iterates through AI/robotics keywords and searches the Handelsregister
2. **Deduplication**: Tracks entity IDs to avoid processing the same company twice
3. **Detailed Fetch**: For each match, fetches full data including:
   - `related_persons` - Management and shareholders
   - `publications` - Official announcements (capital raises, etc.)
   - `financial_kpi` - Financial metrics
   - `balance_sheet_accounts` - Balance sheet data
4. **Analysis**: Calculates relevance score and extracts key information
5. **Filtering**: Applies date and relevance filters
6. **Export**: Saves structured data to CSV and JSON

## API Costs

The handelsregister.ai API uses a credit-based system:

- **Search**: ~1-2 credits per search query
- **Fetch Organization** (with features): ~5-15 credits depending on features
- **Fetch Document**: ~5-10 credits per PDF

**Estimated cost for POC**:
- 3 keywords × 20 results = 60 searches ≈ 120 credits
- 60 unique companies × 10 credits each ≈ 600 credits
- **Total: ~700-800 credits**

Check current pricing at: https://handelsregister.ai/en/faq

## Limitations & Considerations

### Current Limitations

1. **Search API**: The `search_organizations` method may not be available in the current SDK version. If not available, you may need to:
   - Use the web interface to find companies first
   - Or use the `fetch_organization` method with broader queries

2. **Rate Limits**: The script includes 1-second delays between requests. Adjust if needed.

3. **Publication Parsing**: Capital raise detection relies on keyword matching in publications. Some events may be missed or false positives may occur.

4. **API Coverage**: handelsregister.ai updates daily but may have slight delays compared to real-time register changes.

### Legal & Compliance

- ✅ The data is publicly available from the official German Handelsregister
- ✅ handelsregister.ai is a legitimate commercial API provider
- ✅ Appropriate for research, due diligence, and business intelligence
- ⚠️ Respect rate limits and terms of service
- ⚠️ Personal data in the register is protected by GDPR - use appropriately

## Next Steps

### Production Enhancements

1. **Database Integration**
   - Store results in PostgreSQL/MySQL for historical tracking
   - Track changes over time (new entries, capital raises)
   - Build a change detection pipeline

2. **Automated Monitoring**
   - Set up daily/weekly cron jobs
   - Email alerts for new matches
   - Slack/Discord integration for notifications

3. **Advanced Filtering**
   - Geographic filters (focus on specific regions)
   - Industry/sector classification
   - Funding stage detection (seed, Series A, etc.)
   - Technology focus areas (NLP, computer vision, robotics, etc.)

4. **Data Enrichment**
   - Cross-reference with other sources (Crunchbase, LinkedIn)
   - Add web scraping for company websites
   - Social media presence analysis
   - News mentions and PR tracking

5. **API Optimization**
   - Implement caching to reduce API calls
   - Batch processing for large datasets
   - Retry logic with exponential backoff
   - Better error handling and logging

6. **Frontend Dashboard**
   - Build a simple web UI to view results
   - Filter and sort capabilities
   - Export custom reports
   - Visualization of trends (incorporations over time, funding amounts, etc.)

### Alternative Data Sources

For a more comprehensive solution, consider combining with:

- **OffeneRegister.de**: Free bulk downloads of full Handelsregister data
- **Bundesanzeiger**: Official gazette for corporate announcements
- **North Data**: Commercial company data aggregator
- **Crunchbase**: Startup funding and investor data
- **PitchBook**: Private market intelligence

## Example: Extending for Specific Use Cases

### Track Only Series A+ Funding

```python
def has_significant_funding(analysis: Dict) -> bool:
    """Filter for companies with capital > €1M"""
    capital = analysis.get('capital', {}).get('amount', 0)
    return capital > 1_000_000

# In scrape_ai_robotics_startups:
if not has_significant_funding(analysis):
    continue
```

### Focus on Munich/Berlin Tech Hubs

```python
TECH_HUBS = ['München', 'Berlin', 'Hamburg', 'Frankfurt']

def is_in_tech_hub(analysis: Dict) -> bool:
    city = analysis.get('address', {}).get('city', '')
    return city in TECH_HUBS
```

### Track Specific Technologies

```python
DEEP_TECH_KEYWORDS = [
    'quantum',
    'blockchain',
    'biotechnology',
    'nanotechnology',
    'photonics',
]
```

## Troubleshooting

### "Please set HANDELSREGISTER_API_KEY"
Make sure you've exported the environment variable:
```bash
export HANDELSREGISTER_API_KEY="your_key"
```

### "Please install: pip install handelsregister"
Run:
```bash
pip install -r requirements.txt
```

### "Error searching for 'keyword': ..."
- Check your API key is valid
- Verify you have credits available
- Check network connectivity
- Review API documentation for endpoint changes

### No results found
- Try broader keywords
- Increase `recent_months` parameter
- Lower `min_relevance_score`
- Check that the keywords match business purposes in the register

## Resources

- **handelsregister.ai API Docs**: https://handelsregister.ai/en/documentation
- **Python SDK GitHub**: https://github.com/Handelsregister-AI/handelsregister
- **Official Handelsregister**: https://www.handelsregister.de
- **Unternehmensregister**: https://www.unternehmensregister.de

## License

This POC is provided as-is for educational and research purposes. 

When using handelsregister.ai API, you must comply with their terms of service and the German legal framework for accessing public register data (§9 HGB).

## Support

For API-related issues, contact handelsregister.ai support.

For questions about this POC, create an issue or discussion.

---

**Happy Scraping! 🚀**
