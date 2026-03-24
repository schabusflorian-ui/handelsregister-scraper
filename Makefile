.PHONY: dev test lint format install docker clean

## Development

dev:  ## Start web UI with hot-reload
	uvicorn web.app:app --reload --port 8000

install:  ## Install project in editable mode with dev dependencies
	pip install -e ".[dev]"

## Quality

test:  ## Run test suite
	pytest tests/

lint:  ## Check code with ruff
	ruff check .

format:  ## Auto-format code with ruff
	ruff format .
	ruff check --fix .

## Docker

docker:  ## Build and start all services
	docker compose up --build

docker-stealth:  ## Build and start stealth scraper
	docker compose -f docker-compose.stealth.yml up --build

## Utilities

clean:  ## Remove Python artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
