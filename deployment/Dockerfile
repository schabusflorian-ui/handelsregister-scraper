# Handelsregister Scraper - Docker Image
# Hybrid deployment: Web UI + Scheduler (no stealth scraper)
# For deployment on Railway or similar platforms

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install uvicorn and additional web dependencies
RUN pip install --no-cache-dir uvicorn[standard] python-multipart aiofiles

# Copy application code
COPY . .

# Make start script executable
RUN chmod +x deployment/start.sh

# Create data directory for SQLite database
RUN mkdir -p /data

# Environment variables
ENV DATABASE_PATH=/data/handelsregister.db
ENV DB_PATH=/data/handelsregister.db
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Expose web UI port
EXPOSE 8000

# Health check via dedicated endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Run both web UI and scheduler
CMD ["./deployment/start.sh"]
