# Bambu Lab Scraper - Docker Image
FROM python:3.11-slim

# Install Chromium and dependencies
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    wget \
    gnupg \
    cron \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Set environment for crawl4ai to use system Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/bin
ENV CRAWL4AI_BROWSER_PATH=/usr/bin/chromium

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install playwright and download browser
RUN pip install playwright && \
    playwright install chromium && \
    playwright install-deps

# Copy app files
COPY scraper.py .
COPY config.example.json .

# Create data directory
RUN mkdir -p /app/data

# Default: run once
CMD ["python", "scraper.py", "--csv"]
