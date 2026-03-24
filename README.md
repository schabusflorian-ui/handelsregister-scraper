# Handelsregister Startup Discovery Platform

A data platform that monitors the German commercial register (Handelsregister) to discover AI, robotics, and climate-tech startups. It continuously ingests data from multiple sources, scores companies on relevance, and serves an interactive web dashboard for browsing, filtering, and exporting results.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Scheduler                            │
│  (APScheduler — 15 jobs on cron triggers)                   │
└──────┬──────────────┬───────────────┬───────────────────────┘
       │              │               │
       ▼              ▼               ▼
┌─────────────┐ ┌──────────┐ ┌──────────────┐
│   Sources   │ │Processing│ │    Export     │
│ BundesAPI   │ │ Scoring  │ │  CSV / JSON  │
│ LinkedIn    │ │ Filtering│ └──────────────┘
│ DuckDuckGo  │ │ Matching │
│ RSS / News  │ └────┬─────┘
│ Websites    │      │
└──────┬──────┘      │
       │             │
       ▼             ▼
┌─────────────────────────┐     ┌──────────────────────┐
│     Persistence         │────▶│      Web UI          │
│  (SQLite + dataclasses) │     │  (FastAPI + Jinja2)  │
└─────────────────────────┘     └──────────────────────┘
```

## Features

- **Multi-source discovery** — BundesAPI, OffeneRegister bulk data, DuckDuckGo/Brave/Serper search, LinkedIn snippet extraction, RSS news feeds, VC portfolio scraping
- **Intelligent scoring** — AI/robotics relevance, climate-tech relevance, startup likelihood, brand name analysis, investor detection
- **Stealth founder tracking** — discover founders building in stealth via LinkedIn search snippets, track emergence into public companies
- **Web dashboard** — browse, filter (15+ dimensions), sort, tag, and export companies with HTMX-powered UI
- **Automated scheduling** — 15 background jobs on configurable cron triggers with rate limiting
- **Capital event detection** — monitor official publications for funding rounds and capital increases
- **Export** — CSV, JSON, and database download

## Quick Start

### Docker (recommended)

```bash
cp .env.example .env   # Edit with your settings
docker compose up --build
```

The dashboard will be available at `http://localhost:8000`.

### Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start the web UI with hot-reload
make dev
```

### Running Tests

```bash
make test        # Run test suite
make lint        # Check code with ruff
make format      # Auto-format code
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `handelsregister.db` | SQLite database file path |
| `PORT` | `8000` | Web UI port |
| `HANDELSREGISTER_API_KEY` | — | API key for handelsregister.ai (optional) |
| `DISCOVERY_INTERVAL_HOURS` | `2` | Hours between discovery job runs |
| `SLACK_WEBHOOK_URL` | — | Slack webhook for alerts (optional) |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

See `.env.example` for the full list.

### Keyword Configuration

Search keywords and investor lists are configured in `config/`:
- `config/keywords.yaml` — AI, robotics, climate-tech, and deep-tech keywords (German + English)
- `config/investors.yaml` — known VC/PE/angel investor names and aliases

## Project Structure

```
├── main.py                    # CLI entry point (bulk-load, scan, export)
├── config.py                  # Global configuration and keyword lists
├── persistence/               # SQLite database layer
│   └── database.py            #   Company, Officer, CapitalEvent models
├── sources/                   # External data integrations
│   ├── bundesapi.py           #   Official Handelsregister API client
│   ├── google_search.py       #   Multi-engine search (DDG, Brave, Serper)
│   ├── linkedin_scraper.py    #   LinkedIn profile extraction from snippets
│   ├── news_monitor.py        #   RSS feed monitoring
│   ├── website_finder.py      #   Domain guessing and validation
│   └── ...
├── processing/                # Business logic
│   ├── filters.py             #   AI/robotics/climate keyword scoring
│   ├── startup_scorer.py      #   Startup likelihood heuristics
│   ├── investor_matcher.py    #   Fuzzy investor name matching
│   └── ...
├── scheduler/                 # Job orchestration
│   ├── scheduler.py           #   APScheduler with 15 job types
│   ├── rate_limiter.py        #   Token-bucket rate limiter
│   └── jobs/                  #   Individual job implementations
├── web/                       # FastAPI web application
│   ├── app.py                 #   App factory and router registration
│   ├── dependencies.py        #   Shared state (DB, templates)
│   ├── routers/               #   Route modules (companies, founders, ...)
│   ├── templates/             #   Jinja2 templates (Tailwind + HTMX)
│   └── static/                #   JavaScript (app.js)
├── export/                    # CSV/JSON export
├── scripts/                   # Utility and migration scripts
├── tests/                     # Unit tests (pytest)
├── config/                    # YAML keyword and investor configs
├── deployment/                # start.sh for Docker/Railway
├── Dockerfile                 # Production image
├── docker-compose.yml         # Local development stack
└── pyproject.toml             # Project metadata and tool config
```

## Deployment

The application is deployed on [Railway](https://railway.app) with:
- A single container running both the web UI and scheduler (`deployment/start.sh`)
- A persistent volume for the SQLite database (`/data`)
- Health check on `/health`

See `railway.toml` and `Dockerfile` for the full configuration.

The stealth scraper runs locally (not in Docker) because search engines block cloud IPs. Use `scripts/run_stealth.py` for continuous stealth founder discovery.

## Development

```bash
# Install in editable mode with dev dependencies
make install

# Run the web UI with hot-reload
make dev

# Run all tests
make test

# Lint and format
make lint
make format

# Start Docker stack
make docker
```

## License

MIT — see [LICENSE](LICENSE).
