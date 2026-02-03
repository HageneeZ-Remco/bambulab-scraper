#!/usr/bin/env python3
"""
Bambu Lab Filament Scraper
==========================
Scrapes filament products from the Bambu Lab EU store.
Sends Discord notifications for stock changes, new items, and removed items.

Requirements:
    pip install crawl4ai requests

Usage:
    python scraper.py                    # Scrape and save to JSON
    python scraper.py --csv              # Also export to CSV
    python scraper.py --output data.json # Custom output file
    python scraper.py --no-notify        # Skip Discord notifications

Cron Example (every hour):
    0 * * * * cd /path/to/scraper && /usr/bin/python3 scraper.py --csv >> /var/log/bambu-scraper.log 2>&1

Author: HageneeZ
Version: 2.0.0
"""

import asyncio
import json
import csv
import re
import os
import sys
import argparse
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from crawl4ai import AsyncWebCrawler
except ImportError:
    print("Error: crawl4ai not installed!")
    print("Install with: pip install crawl4ai")
    print("Then run: crawl4ai-setup")
    sys.exit(1)


# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    "base_url": "https://eu.store.bambulab.com",
    "collection_url": "https://eu.store.bambulab.com/nl/collections/bambu-lab-3d-printer-filament",
    "output_dir": "data",
    "output_file": "bambulab_filaments.json",
    "csv_file": "bambulab_filaments.csv",
    "config_file": "config.json",  # For webhooks
}

# Default Discord embed colors
COLORS = {
    "new_item": 0x00FF00,      # Green - new product added
    "removed_item": 0xFF0000,  # Red - product removed
    "in_stock": 0x00FF00,      # Green - back in stock
    "out_of_stock": 0xFF9900,  # Orange - out of stock
    "price_drop": 0x00FFFF,    # Cyan - price decreased
    "price_increase": 0xFF6600, # Orange - price increased
}


# =============================================================================
# DISCORD NOTIFICATIONS
# =============================================================================

def load_config(config_path: str) -> dict:
    """Load configuration from JSON file and environment variables."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        # Create default config
        config = {
            "discord_webhooks": {
                "stock_alerts": [],
                "new_items": [],
                "removed_items": [],
                "price_changes": [],
                "all": []  # Receives all notifications
            },
            "notify_on": {
                "out_of_stock": True,
                "in_stock": True,
                "new_items": True,
                "removed_items": True,
                "price_changes": True
            }
        }
        save_config(config, config_path)
        print(f"[CONFIG] Created default config: {config_path}")
        print("[CONFIG] Add your Discord webhook URLs to enable notifications!")
    except json.JSONDecodeError as e:
        print(f"[ERROR] Invalid config file: {e}")
        config = {"discord_webhooks": {}, "notify_on": {}}

    # Override with environment variables (for Pterodactyl/Docker)
    env_webhooks = {
        "DISCORD_WEBHOOK_ALL": "all",
        "DISCORD_WEBHOOK_STOCK": "stock_alerts",
        "DISCORD_WEBHOOK_NEW": "new_items",
        "DISCORD_WEBHOOK_REMOVED": "removed_items",
        "DISCORD_WEBHOOK_PRICE": "price_changes",
    }

    for env_var, config_key in env_webhooks.items():
        webhook_url = os.environ.get(env_var, "").strip()
        if webhook_url:
            if config_key not in config.get("discord_webhooks", {}):
                config.setdefault("discord_webhooks", {})[config_key] = []
            if webhook_url not in config["discord_webhooks"][config_key]:
                config["discord_webhooks"][config_key].append(webhook_url)
                print(f"[CONFIG] Added webhook from {env_var}")

    return config


def save_config(config: dict, config_path: str) -> None:
    """Save configuration to JSON file."""
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)


def send_discord_notification(
    webhooks: list[str],
    title: str,
    description: str,
    color: int,
    fields: list[dict] = None,
    thumbnail_url: str = None,
    url: str = None
) -> None:
    """
    Send notification to multiple Discord webhooks.

    Args:
        webhooks: List of webhook URLs
        title: Embed title
        description: Embed description
        color: Embed color (hex)
        fields: Optional list of field dicts with name/value/inline
        thumbnail_url: Optional thumbnail image URL
        url: Optional URL for the embed title
    """
    if not webhooks:
        return

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {
            "text": "Bambu Lab Scraper"
        }
    }

    if fields:
        embed["fields"] = fields
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    for webhook_url in webhooks:
        if not webhook_url:
            continue
        try:
            response = requests.post(
                webhook_url,
                json=payload,
                timeout=10
            )
            if response.status_code == 204:
                print(f"[DISCORD] Notification sent successfully")
            else:
                print(f"[DISCORD] Failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"[DISCORD] Error sending notification: {e}")


def get_webhooks_for_event(config: dict, event_type: str) -> list[str]:
    """Get all webhook URLs for a specific event type."""
    webhooks = config.get("discord_webhooks", {})

    # Combine specific webhooks with "all" webhooks
    specific = webhooks.get(event_type, [])
    all_hooks = webhooks.get("all", [])

    # Remove duplicates while preserving order
    combined = []
    seen = set()
    for url in specific + all_hooks:
        if url and url not in seen:
            combined.append(url)
            seen.add(url)

    return combined


def notify_out_of_stock(config: dict, products: list[dict]) -> None:
    """Send notification for products that went out of stock."""
    if not config.get("notify_on", {}).get("out_of_stock", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title="⚠️ Out of Stock",
            description=f"**{product['name']}** is now out of stock",
            color=COLORS["out_of_stock"],
            fields=[
                {"name": "Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_in_stock(config: dict, products: list[dict]) -> None:
    """Send notification for products that came back in stock."""
    if not config.get("notify_on", {}).get("in_stock", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title="✅ Back in Stock!",
            description=f"**{product['name']}** is now available",
            color=COLORS["in_stock"],
            fields=[
                {"name": "Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_new_items(config: dict, products: list[dict]) -> None:
    """Send notification for newly added products."""
    if not config.get("notify_on", {}).get("new_items", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "new_items")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title="🆕 New Product!",
            description=f"**{product['name']}** has been added to the store",
            color=COLORS["new_item"],
            fields=[
                {"name": "Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_removed_items(config: dict, products: list[dict]) -> None:
    """Send notification for removed products."""
    if not config.get("notify_on", {}).get("removed_items", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "removed_items")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title="🗑️ Product Removed",
            description=f"**{product['name']}** is no longer available",
            color=COLORS["removed_item"],
            fields=[
                {"name": "Last Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_price_changes(config: dict, changes: list[dict]) -> None:
    """Send notification for price changes."""
    if not config.get("notify_on", {}).get("price_changes", True):
        return
    if not changes:
        return

    webhooks = get_webhooks_for_event(config, "price_changes")

    for change in changes:
        is_drop = change.get("change", "").startswith("-")

        send_discord_notification(
            webhooks=webhooks,
            title="💰 Price Drop!" if is_drop else "📈 Price Increase",
            description=f"**{change['name']}** price changed",
            color=COLORS["price_drop"] if is_drop else COLORS["price_increase"],
            fields=[
                {"name": "Old Price", "value": change.get("old_price", "N/A"), "inline": True},
                {"name": "New Price", "value": change.get("new_price", "N/A"), "inline": True},
                {"name": "Change", "value": change.get("change", "N/A"), "inline": True},
            ],
            url=change.get("url")
        )


# =============================================================================
# SCRAPING
# =============================================================================

async def scrape_collection(url: str) -> dict:
    """
    Scrape a collection page and extract product data.

    Args:
        url: The collection URL to scrape

    Returns:
        dict with products list and metadata
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping: {url}")

    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(
            url=url,
            bypass_cache=True,
            page_timeout=60000,
        )

        if not result.success:
            raise Exception(f"Failed to fetch page: {result.error_message}")

        return parse_products(result.markdown)


def parse_products(markdown: str) -> dict:
    """
    Parse product data from markdown content.

    Args:
        markdown: Raw markdown from crawler

    Returns:
        dict with products list and metadata
    """
    products = []

    # Find all product links with full URL
    product_links = re.findall(
        r'\[([^\]]+)\]\((https://eu\.store\.bambulab\.com/nl/products/[^)]+)\)',
        markdown
    )

    # Find all prices in the markdown
    all_prices = re.findall(r'(?:Van\s+)?€\s*([\d,\.]+)\s*EUR?', markdown)

    # Check for out of stock indicators
    out_of_stock_indicators = ['uitverkocht', 'out of stock', 'sold out', 'niet beschikbaar']
    markdown_lower = markdown.lower()

    # Track seen URLs to avoid duplicates
    seen_urls = set()
    price_index = 0

    for name, url in product_links:
        # Skip duplicates and non-product links
        if url in seen_urls:
            continue
        if not any(kw in name.upper() for kw in ['PLA', 'PETG', 'ABS', 'TPU', 'PA', 'ASA', 'PPS', 'PVA', 'SUPPORT']):
            continue

        seen_urls.add(url)

        # Try to get corresponding price
        price = None
        price_eur = None
        if price_index < len(all_prices):
            price_str = all_prices[price_index]
            price = f"€{price_str}"
            try:
                price_eur = float(price_str.replace(',', '.'))
            except ValueError:
                pass
            price_index += 1

        # Extract product handle from URL
        handle = url.split('/products/')[-1] if '/products/' in url else None

        # Check stock status (basic check - can be enhanced)
        # Look for out of stock text near the product name in markdown
        in_stock = True
        name_pos = markdown_lower.find(name.lower())
        if name_pos != -1:
            # Check 500 chars after product name for stock indicators
            context = markdown_lower[name_pos:name_pos + 500]
            if any(indicator in context for indicator in out_of_stock_indicators):
                in_stock = False

        products.append({
            "name": name.strip(),
            "handle": handle,
            "price": price,
            "price_eur": price_eur,
            "url": url,
            "in_stock": in_stock,
        })

    return {
        "scraped_at": datetime.now().isoformat(),
        "source_url": CONFIG["collection_url"],
        "product_count": len(products),
        "products": products,
    }


# =============================================================================
# COMPARISON
# =============================================================================

def compare_data(old_data: dict, new_data: dict) -> dict:
    """
    Compare old and new data to detect changes.

    Returns:
        dict with lists of: new_items, removed_items, out_of_stock, in_stock, price_changes
    """
    if not old_data:
        return {
            "new_items": [],
            "removed_items": [],
            "out_of_stock": [],
            "in_stock": [],
            "price_changes": [],
        }

    old_products = {p["handle"]: p for p in old_data.get("products", [])}
    new_products = {p["handle"]: p for p in new_data.get("products", [])}

    old_handles = set(old_products.keys())
    new_handles = set(new_products.keys())

    # New items (in new but not in old)
    new_items = [new_products[h] for h in (new_handles - old_handles)]

    # Removed items (in old but not in new)
    removed_items = [old_products[h] for h in (old_handles - new_handles)]

    # Stock and price changes (items in both)
    out_of_stock = []
    in_stock = []
    price_changes = []

    for handle in (old_handles & new_handles):
        old_p = old_products[handle]
        new_p = new_products[handle]

        # Stock change: was in stock, now out of stock
        if old_p.get("in_stock", True) and not new_p.get("in_stock", True):
            out_of_stock.append(new_p)

        # Stock change: was out of stock, now in stock
        if not old_p.get("in_stock", True) and new_p.get("in_stock", True):
            in_stock.append(new_p)

        # Price change
        old_price = old_p.get("price_eur")
        new_price = new_p.get("price_eur")

        if old_price and new_price and old_price != new_price:
            change_pct = ((new_price - old_price) / old_price) * 100
            price_changes.append({
                "name": new_p["name"],
                "handle": handle,
                "old_price": f"€{old_price:.2f}",
                "new_price": f"€{new_price:.2f}",
                "change": f"{change_pct:+.1f}%",
                "url": new_p.get("url"),
            })

    return {
        "new_items": new_items,
        "removed_items": removed_items,
        "out_of_stock": out_of_stock,
        "in_stock": in_stock,
        "price_changes": price_changes,
    }


# =============================================================================
# FILE I/O
# =============================================================================

def save_json(data: dict, filepath: str) -> None:
    """Save data to JSON file."""
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Saved JSON: {filepath}")


def save_csv(data: dict, filepath: str) -> None:
    """Save products to CSV file."""
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    products = data.get("products", [])
    if not products:
        print("No products to save to CSV")
        return

    fieldnames = ["name", "handle", "price", "price_eur", "url", "in_stock"]

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(products)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Saved CSV: {filepath}")


def load_previous_data(filepath: str) -> dict | None:
    """Load previous scrape data for comparison."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# =============================================================================
# MAIN
# =============================================================================

async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Scrape Bambu Lab filament prices")
    parser.add_argument("--output", "-o", help="Output JSON file path")
    parser.add_argument("--csv", action="store_true", help="Also export to CSV")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--no-notify", action="store_true", help="Skip Discord notifications")
    parser.add_argument("--test-webhook", help="Send test notification to webhook URL")
    args = parser.parse_args()

    # Setup paths
    script_dir = Path(__file__).parent
    output_dir = script_dir / CONFIG["output_dir"]
    json_path = args.output or str(output_dir / CONFIG["output_file"])
    csv_path = str(output_dir / CONFIG["csv_file"])
    config_path = str(script_dir / CONFIG["config_file"])

    # Load config
    config = load_config(config_path)

    # Test webhook mode
    if args.test_webhook:
        print(f"[TEST] Sending test notification to webhook...")
        send_discord_notification(
            webhooks=[args.test_webhook],
            title="🧪 Test Notification",
            description="Your Bambu Lab Scraper webhook is working!",
            color=0x00FF00,
            fields=[
                {"name": "Status", "value": "Connected", "inline": True},
                {"name": "Time", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
            ]
        )
        return 0

    # Load previous data for comparison
    old_data = load_previous_data(json_path)

    try:
        # Scrape the collection
        data = await scrape_collection(CONFIG["collection_url"])

        if not args.quiet:
            print(f"\n{'='*50}")
            print(f"Scraped {data['product_count']} products")
            print(f"{'='*50}\n")

            # Show sample products
            for product in data["products"][:5]:
                stock_status = "✅" if product.get("in_stock", True) else "❌"
                print(f"  {stock_status} {product['name']}: {product['price'] or 'N/A'}")
            if data["product_count"] > 5:
                print(f"  ... and {data['product_count'] - 5} more\n")

        # Compare with previous data
        changes = compare_data(old_data, data)

        # Log changes
        if not args.quiet:
            if changes["new_items"]:
                print(f"\n🆕 NEW ITEMS ({len(changes['new_items'])}):")
                for item in changes["new_items"]:
                    print(f"   + {item['name']}: {item.get('price', 'N/A')}")

            if changes["removed_items"]:
                print(f"\n🗑️ REMOVED ITEMS ({len(changes['removed_items'])}):")
                for item in changes["removed_items"]:
                    print(f"   - {item['name']}")

            if changes["out_of_stock"]:
                print(f"\n⚠️ OUT OF STOCK ({len(changes['out_of_stock'])}):")
                for item in changes["out_of_stock"]:
                    print(f"   ! {item['name']}")

            if changes["in_stock"]:
                print(f"\n✅ BACK IN STOCK ({len(changes['in_stock'])}):")
                for item in changes["in_stock"]:
                    print(f"   + {item['name']}: {item.get('price', 'N/A')}")

            if changes["price_changes"]:
                print(f"\n💰 PRICE CHANGES ({len(changes['price_changes'])}):")
                for change in changes["price_changes"]:
                    print(f"   {change['name']}: {change['old_price']} → {change['new_price']} ({change['change']})")

        # Send Discord notifications
        if not args.no_notify:
            notify_new_items(config, changes["new_items"])
            notify_removed_items(config, changes["removed_items"])
            notify_out_of_stock(config, changes["out_of_stock"])
            notify_in_stock(config, changes["in_stock"])
            notify_price_changes(config, changes["price_changes"])

        # Save results
        save_json(data, json_path)

        if args.csv:
            save_csv(data, csv_path)

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Done!")
        return 0

    except Exception as e:
        print(f"[ERROR] Scraping failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
