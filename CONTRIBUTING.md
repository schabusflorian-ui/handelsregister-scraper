# Contributing

## Setup

```bash
git clone <repo-url>
cd handelsregister-scraper

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Development Workflow

1. Create a feature branch from `main`
2. Make your changes
3. Run the checks:
   ```bash
   make lint      # ruff check
   make format    # ruff format
   make test      # pytest
   ```
4. Commit with a clear message describing the change
5. Open a pull request against `main`

## Code Style

- Formatting and linting are handled by [ruff](https://docs.astral.sh/ruff/) (configured in `pyproject.toml`)
- Run `make format` before committing to auto-fix style issues
- Keep functions focused — prefer small, well-named functions over long procedural blocks

## Running Locally

```bash
# Web UI with hot-reload
make dev

# Full Docker stack (web + scheduler)
make docker
```

## Tests

Tests live in `tests/unit/` and use pytest. Run with:

```bash
make test
```

## Project Layout

See the [README](README.md#project-structure) for a full directory map.
