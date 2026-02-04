# Handelsregister Scraper - Docker Image
# For deployment on Railway or similar platforms

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory for SQLite database
RUN mkdir -p /data

# Environment variables
ENV DATABASE_PATH=/data/handelsregister.db
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "from persistence.database import Database; d = Database('$DATABASE_PATH'); print('OK'); d.close()" || exit 1

# Default command: run the scheduler
CMD ["python3", "-m", "scheduler.main", "--db", "/data/handelsregister.db"]
