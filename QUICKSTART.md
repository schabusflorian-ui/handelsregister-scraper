# Handelsregister Scraper POC - Quick Start Guide

## What Was Built

A complete proof-of-concept scraper for finding AI and robotics startups in the German Handelsregister. The POC includes:

### Core Files

1. **`handelsregister_scraper.py`** - Main scraper implementation
   - Search by AI/robotics keywords
   - Filter by incorporation date and relevance
   - Extract company details, management, and capital raises
   - Export to CSV and JSON

2. **`advanced_monitoring.py`** - Database integration example
   - SQLite database for change tracking
   - Monitor new companies and capital raises over time
   - Generate reports and statistics
   - Foundation for automated monitoring system

3. **`config.py`** - Configuration file
   - Customize keywords, filters, and parameters
   - No code changes needed for different searches
   - Examples for specialized configurations

4. **`requirements.txt`** - Python dependencies
5. **`.env.example`** - Environment variable template
6. **`README.md`** - Comprehensive documentation

## Immediate Next Steps

### 1. Get Started (5 minutes)

```bash
# Install dependencies
pip install -r requirements.txt

# Get API key from handelsregister.ai
# Sign up at: https://handelsregister.ai

# Set your API key
export HANDELSREGISTER_API_KEY="your_api_key_here"

# Run the basic scraper
python handelsregister_scraper.py
```

### 2. Review Results

The scraper will create:
- `ai_robotics_startups.csv` - Spreadsheet view
- `ai_robotics_startups.json` - Structured data

### 3. Customize Your Search

Edit `config.py` to:
- Add/remove keywords
- Focus on specific cities (Munich, Berlin, etc.)
- Adjust time window (last 6 months, 12 months, etc.)
- Set minimum capital requirements
- Filter by technology category

### 4. Run Advanced Monitoring

```bash
# Set up database tracking
python advanced_monitoring.py

# This will:
# - Create startups.db SQLite database
# - Track changes over time
# - Generate summary reports
```

## Key Features Explained

### Keyword Search
The scraper searches for companies whose business purpose contains AI/robotics keywords:
- German: "künstliche intelligenz", "robotik", "maschinelles lernen"
- English: "artificial intelligence", "robotics", "machine learning"

### Capital Raise Detection
Analyzes official publications (Bekanntmachungen) for events like:
- Kapitalerhöhung (capital increase)
- Stammkapital changes
- Gesellschafterbeschluss (shareholder resolutions)

### Relevance Scoring
Each company gets a score based on how many keywords appear in their business purpose.
Higher score = more relevant to AI/robotics.

### Recent Incorporations
Filters for companies registered in the last N months to find new startups.

## API Usage & Costs

**Free Tier**: Get started with limited credits
**Paid Plans**: From €19/month for regular usage

**Estimated costs for this POC**:
- Searching 3 keywords × 20 results = ~120 credits
- Fetching 60 companies with features = ~600 credits
- **Total: ~700 credits** (one-time for POC run)

## What Data You'll Get

For each company:
- **Basic Info**: Name, registration number, status, incorporation date
- **Business Details**: Purpose/description, legal form, registered address
- **Financial**: Share capital amount and currency
- **Management**: Names and roles of Geschäftsführer/board members
- **Capital Raises**: Detected funding events from publications
- **Contact**: Website, address, city

## Limitations to Know

1. **Search Method**: Currently uses keyword-based search. Some companies might be missed if they don't use standard AI/robotics terminology.

2. **Publication Parsing**: Capital raise detection uses keyword matching. Manual verification recommended for investment decisions.

3. **Update Frequency**: handelsregister.ai updates daily. There may be 1-2 day delay from official register changes.

4. **Coverage**: Only covers German Handelsregister. Doesn't include:
   - Foreign companies
   - Sole proprietorships (Einzelunternehmen)
   - Partnerships without register requirement

## Production Recommendations

### For Regular Monitoring
1. Set up daily/weekly cron job
2. Use `advanced_monitoring.py` for change tracking
3. Implement email/Slack notifications
4. Store results in PostgreSQL for better querying

### For Investment Sourcing
1. Cross-reference with Crunchbase/PitchBook
2. Add web scraping for company websites
3. Track LinkedIn company pages
4. Monitor news mentions
5. Enrich with founder backgrounds

### For Market Research
1. Download full dataset from OffeneRegister.de
2. Analyze trends over time
3. Geographic clustering analysis
4. Technology category classification
5. Combine with Bundesanzeiger financial data

## Troubleshooting

**"No results found"**
- Try broader keywords
- Increase `RECENT_MONTHS` in config
- Lower `MIN_RELEVANCE_SCORE`

**"API key error"**
- Verify key is correct
- Check you have credits available
- Ensure environment variable is set

**"Rate limit exceeded"**
- Increase `RATE_LIMIT_DELAY` in config
- Reduce `MAX_RESULTS_PER_KEYWORD`

## Alternative Approaches

### Free/Open Source Options
1. **OffeneRegister.de**: Download full dataset, parse locally
   - Pros: Free, complete historical data
   - Cons: Requires more setup, less structured

2. **bundesAPI/handelsregister**: Direct scraping of official portal
   - Pros: Free, official source
   - Cons: 60 requests/hour limit, less structured

### Commercial Alternatives
1. **North Data**: Commercial aggregator with APIs
2. **Implisense**: Company data platform
3. **Dealfront/Echobot**: B2B data platform with Handelsregister access

## Support Resources

- **handelsregister.ai Docs**: https://handelsregister.ai/en/documentation
- **API SDK**: https://github.com/Handelsregister-AI/handelsregister
- **Official Register**: https://www.handelsregister.de

## Next Development Steps

### Week 1-2: Foundation
- [ ] Test POC with your API key
- [ ] Customize keywords for your focus area
- [ ] Review initial results quality
- [ ] Decide on production architecture

### Week 3-4: Enhancement
- [ ] Set up database (PostgreSQL recommended)
- [ ] Implement change tracking
- [ ] Add notification system (email/Slack)
- [ ] Create simple web dashboard

### Month 2: Production
- [ ] Automate with cron jobs
- [ ] Implement data enrichment pipeline
- [ ] Add quality scoring system
- [ ] Build reporting templates

### Month 3+: Scaling
- [ ] Integrate additional data sources
- [ ] Machine learning for classification
- [ ] API for internal tools
- [ ] Export to CRM/deal flow tools

## Example Use Cases

### Venture Capital
"Find all AI startups in Munich incorporated in last 12 months with >€100k capital"
```python
CITIES_FILTER = ['München']
RECENT_MONTHS = 12
MIN_CAPITAL_AMOUNT = 100_000
```

### Corporate Innovation
"Track robotics companies for potential partnerships"
```python
SEARCH_KEYWORDS = ['robotik', 'autonomous', 'automation']
FEATURES.append('related_persons')  # Get decision makers
```

### Market Research
"Analyze AI startup landscape in Germany"
- Download full dataset from OffeneRegister.de
- Run trend analysis on incorporation rates
- Geographic clustering
- Technology category breakdown

## Success Metrics

Track these KPIs to measure scraper effectiveness:
- **Precision**: % of results that are truly AI/robotics companies
- **Recall**: % of actual AI/robotics companies found
- **Freshness**: Time lag from incorporation to detection
- **Coverage**: % of target markets/geographies covered

## Questions?

For API issues → Contact handelsregister.ai support
For code questions → Review README.md and code comments
For enhancements → Modify config.py and extend scraper classes

---

**You're ready to start! Run `python handelsregister_scraper.py` to begin. 🚀**
