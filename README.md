# Bambu Lab Filament Scraper

Scrapes filament products and prices from the [Bambu Lab EU Store](https://eu.store.bambulab.com/nl/collections/bambu-lab-3d-printer-filament).

## Features

- Extracts all filament products with names, prices, and URLs
- Saves data as JSON and optionally CSV
- Detects price changes between runs
- Designed for cron job automation
- No API key required

## Requirements

- Python 3.10+
- Google Chrome or Chromium (for headless browsing)

## Installation

### 1. Clone or download this folder

```bash
git clone <repo-url> bambulab-scraper
cd bambulab-scraper
```

### 2. Create virtual environment (recommended)

```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Setup Crawl4AI (first time only)

```bash
crawl4ai-setup
```

This downloads the required browser binaries (~300MB).

## Usage

### Basic usage

```bash
python scraper.py
```

Output: `data/bambulab_filaments.json`

### With CSV export

```bash
python scraper.py --csv
```

Output: `data/bambulab_filaments.json` + `data/bambulab_filaments.csv`

### Custom output file

```bash
python scraper.py --output /path/to/output.json
```

### Quiet mode (for cron)

```bash
python scraper.py --quiet --csv
```

## Output Format

### JSON

```json
{
  "scraped_at": "2026-02-03T22:00:00.000000",
  "source_url": "https://eu.store.bambulab.com/nl/collections/bambu-lab-3d-printer-filament",
  "product_count": 35,
  "products": [
    {
      "name": "PLA Basic",
      "handle": "pla-basic-filament",
      "price": "€11,50",
      "price_eur": 11.5,
      "url": "https://eu.store.bambulab.com/nl/products/pla-basic-filament",
      "in_stock": true
    }
  ]
}
```

### CSV

| name | handle | price | price_eur | url | in_stock |
|------|--------|-------|-----------|-----|----------|
| PLA Basic | pla-basic-filament | €11,50 | 11.5 | https://... | True |

## Cron Job Setup

### Linux/Mac

Edit crontab:
```bash
crontab -e
```

Add line (runs daily at 6:00 AM):
```cron
0 6 * * * cd /path/to/bambulab-scraper && /path/to/venv/bin/python scraper.py --csv --quiet >> /var/log/bambu-scraper.log 2>&1
```

### With virtual environment

```cron
0 6 * * * cd /home/user/bambulab-scraper && ./venv/bin/python scraper.py --csv --quiet
```

### Check logs

```bash
tail -f /var/log/bambu-scraper.log
```

## Discord Notifications

The scraper can send Discord notifications for:
- 🆕 **New products** added to the store
- 🗑️ **Removed products** no longer available
- ✅ **Back in stock** items
- ⚠️ **Out of stock** items
- 💰 **Price changes** (drops and increases)

### Setup

On first run, a `config.json` file is created:

```json
{
  "discord_webhooks": {
    "stock_alerts": [],
    "new_items": [],
    "removed_items": [],
    "price_changes": [],
    "all": []
  },
  "notify_on": {
    "out_of_stock": true,
    "in_stock": true,
    "new_items": true,
    "removed_items": true,
    "price_changes": true
  }
}
```

### Adding Webhooks

1. Create a Discord webhook in your server (Server Settings → Integrations → Webhooks)
2. Copy the webhook URL
3. Add it to the appropriate category in `config.json`:

```json
{
  "discord_webhooks": {
    "stock_alerts": [
      "https://discord.com/api/webhooks/123/abc"
    ],
    "all": [
      "https://discord.com/api/webhooks/456/xyz"
    ]
  }
}
```

- **stock_alerts**: Receives out-of-stock and back-in-stock notifications
- **new_items**: Receives new product notifications
- **removed_items**: Receives removed product notifications
- **price_changes**: Receives price change notifications
- **all**: Receives ALL notifications (useful for a general channel)

You can add multiple webhooks per category.

### Test Your Webhook

```bash
python scraper.py --test-webhook "https://discord.com/api/webhooks/your/webhook"
```

### Disable Notifications

```bash
python scraper.py --no-notify
```

Or set specific notifications to `false` in `config.json`:

```json
{
  "notify_on": {
    "out_of_stock": false,
    "price_changes": false
  }
}
```

## Price Change Detection

The scraper automatically compares with the previous run and reports price changes:

```
*** PRICE CHANGES DETECTED ***
  PLA Basic: €12.50 -> €11.50 (-8.0%)
  PETG HF: €13.00 -> €11.50 (-11.5%)
```

## Troubleshooting

### "crawl4ai not installed"

```bash
pip install crawl4ai
crawl4ai-setup
```

### Browser errors

Make sure Chrome/Chromium is installed:

```bash
# Ubuntu/Debian
sudo apt install chromium-browser

# Mac
brew install --cask chromium

# Or let crawl4ai install it
crawl4ai-setup
```

### Timeout errors

The store uses heavy JavaScript. If scraping fails, try increasing timeout in `scraper.py`:

```python
page_timeout=120000,  # 2 minutes
```

## Extending

### Add more collections

Edit `CONFIG` in `scraper.py`:

```python
CONFIG = {
    "collections": [
        "bambu-lab-3d-printer-filament",
        "accessories",
        "3d-printer",
    ],
    ...
}
```

### Webhook notifications

Add to the end of `main()`:

```python
if changes:
    import requests
    requests.post("https://your-webhook.com", json={"changes": changes})
```

## License

MIT - Feel free to use and modify.

## Author

HageneeZ - 2026
