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
    Send notification to multiple Discord webhooks with retry on rate limit.

    Args:
        webhooks: List of webhook URLs
        title: Embed title
        description: Embed description
        color: Embed color (hex)
        fields: Optional list of field dicts with name/value/inline
        thumbnail_url: Optional thumbnail image URL
        url: Optional URL for the embed title
    """
    import time

    if not webhooks:
        return

    embed = {
        "title": title,
        "description": description[:4096] if description else "",  # Discord limit
        "color": color,
        "timestamp": datetime.utcnow().isoformat(),
        "footer": {
            "text": "Bambu Lab Scraper"
        }
    }

    if fields:
        embed["fields"] = fields[:25]  # Discord limit: max 25 fields
    if thumbnail_url:
        embed["thumbnail"] = {"url": thumbnail_url}
    if url:
        embed["url"] = url

    payload = {"embeds": [embed]}

    for i, webhook_url in enumerate(webhooks):
        if not webhook_url:
            continue

        # Add delay between webhooks to avoid rate limiting
        if i > 0:
            time.sleep(0.5)

        # Retry logic for rate limits
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    webhook_url,
                    json=payload,
                    timeout=10
                )
                if response.status_code == 204:
                    print(f"[DISCORD] Notification sent successfully")
                    break
                elif response.status_code == 429:
                    # Rate limited - parse Retry-After header
                    retry_after = float(response.headers.get("Retry-After", 1))
                    print(f"[DISCORD] Rate limited, waiting {retry_after}s...")
                    time.sleep(retry_after)
                else:
                    print(f"[DISCORD] Failed: {response.status_code} - {response.text}")
                    break
            except Exception as e:
                print(f"[DISCORD] Error sending notification: {e}")
                break


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


def notify_out_of_stock(config: dict, variants: list[dict]) -> None:
    """Send a SINGLE notification for ALL variants that went out of stock."""
    if not config.get("notify_on", {}).get("out_of_stock", True):
        return
    if not variants:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")

    # Group variants by product
    by_product = {}
    for v in variants:
        product = v.get("product_name", "Unknown")
        if product not in by_product:
            by_product[product] = {"variants": [], "url": v.get("url"), "eta": v.get("eta")}
        by_product[product]["variants"].append(v.get("variant_name", "?"))
        if v.get("eta"):
            by_product[product]["eta"] = v.get("eta")

    total_colors = len(variants)
    total_products = len(by_product)

    # Build description with all products
    lines = []
    for product_name, data in by_product.items():
        variant_names = data["variants"]
        colors_text = ", ".join(variant_names[:8])
        if len(variant_names) > 8:
            colors_text += f"... +{len(variant_names) - 8}"
        eta_text = f" (ETA: {data['eta']})" if data.get("eta") else ""
        lines.append(f"**{product_name}** ({len(variant_names)}): {colors_text}{eta_text}")

    # Discord description limit is 4096 chars
    description = "\n".join(lines)
    if len(description) > 3900:
        # Truncate and add summary
        description = description[:3900] + f"\n\n... and more products"

    send_discord_notification(
        webhooks=webhooks,
        title=f"⚠️ Out of Stock Report ({total_colors} colors)",
        description=description,
        color=COLORS["out_of_stock"],
    )


def notify_in_stock(config: dict, variants: list[dict]) -> None:
    """Send a SINGLE notification for ALL variants that came back in stock."""
    if not config.get("notify_on", {}).get("in_stock", True):
        return
    if not variants:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")

    # Group variants by product
    by_product = {}
    for v in variants:
        product = v.get("product_name", "Unknown")
        if product not in by_product:
            by_product[product] = {"variants": [], "url": v.get("url")}
        by_product[product]["variants"].append(v.get("variant_name", "?"))

    total_colors = len(variants)
    total_products = len(by_product)

    # Build description with all products
    lines = []
    for product_name, data in by_product.items():
        variant_names = data["variants"]
        colors_text = ", ".join(variant_names[:8])
        if len(variant_names) > 8:
            colors_text += f"... +{len(variant_names) - 8}"
        lines.append(f"**{product_name}** ({len(variant_names)}): {colors_text}")

    # Discord description limit is 4096 chars
    description = "\n".join(lines)
    if len(description) > 3900:
        description = description[:3900] + f"\n\n... and more products"

    send_discord_notification(
        webhooks=webhooks,
        title=f"✅ Back in Stock! ({total_colors} colors)",
        description=description,
        color=COLORS["in_stock"],
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
    Visits each product page to check stock status.

    Args:
        url: The collection URL to scrape

    Returns:
        dict with products list and metadata
    """
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping collection: {url}")

    async with AsyncWebCrawler(
        headless=True,
        verbose=False,
    ) as crawler:
        # First get the collection page to find all products
        result = await crawler.arun(
            url=url,
            bypass_cache=True,
            page_timeout=60000,
        )

        if not result.success:
            raise Exception(f"Failed to fetch page: {result.error_message}")

        # Parse basic product info from collection
        products = parse_products_from_collection(result.markdown)

        # Now visit each product page to check stock status
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking stock for {len(products)} products...")

        for i, product in enumerate(products):
            if product.get("url"):
                try:
                    print(f"  [{i+1}/{len(products)}] {product['name']}...", end=" ", flush=True)
                    stock_result = await crawler.arun(
                        url=product["url"],
                        bypass_cache=True,
                        page_timeout=30000,
                    )
                    if stock_result.success:
                        # crawl4ai returns raw_html or html depending on version
                        html = getattr(stock_result, 'raw_html', None) or getattr(stock_result, 'html', None) or ""
                        any_in_stock, eta, variants = check_stock_status(stock_result.markdown, html)
                        product["in_stock"] = any_in_stock
                        product["eta"] = eta
                        product["variants"] = variants

                        # Count stock status
                        if variants:
                            in_count = sum(1 for v in variants if v["in_stock"])
                            out_count = len(variants) - in_count
                            if out_count == 0:
                                print(f"✓ ({len(variants)} colors)")
                            elif in_count == 0:
                                print(f"✗ ALL OUT OF STOCK ({len(variants)} colors)" + (f" ETA: {eta}" if eta else ""))
                            else:
                                print(f"⚠ {out_count}/{len(variants)} colors out of stock")
                        else:
                            if any_in_stock:
                                print("✓")
                            else:
                                print(f"✗ OUT OF STOCK" + (f" (ETA: {eta})" if eta else ""))
                    else:
                        print("? (failed)")
                except Exception as e:
                    print(f"? (error: {e})")

        return {
            "scraped_at": datetime.now().isoformat(),
            "source_url": CONFIG["collection_url"],
            "product_count": len(products),
            "products": products,
        }


def check_stock_status(markdown: str, html: str = None) -> tuple[bool, str | None, list[dict]]:
    """
    Check stock status for all variants of a product.

    Returns:
        tuple of (any_in_stock: bool, eta: str | None, variants: list[dict])
        variants contains: [{"name": "White", "in_stock": True, "eta": None}, ...]
    """
    markdown_lower = markdown.lower()
    variants = []

    if html:
        # Method 1: Try __NEXT_DATA__ (Next.js stores product data here)
        next_data_match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_data_match:
            try:
                next_data = json.loads(next_data_match.group(1))
                # Navigate to product data - structure varies but usually in props
                page_props = next_data.get("props", {}).get("pageProps", {})
                product = page_props.get("product", {}) or page_props.get("data", {}).get("product", {})

                # Get variants from product
                product_variants = product.get("variants", [])
                for v in product_variants:
                    variant_name = v.get("title", v.get("name", "Default"))
                    # Check availability - could be "available", "in_stock", etc.
                    available = v.get("available", v.get("availableForSale", v.get("in_stock", True)))
                    variants.append({
                        "name": variant_name,
                        "in_stock": bool(available),
                        "price": v.get("price", {}).get("amount") if isinstance(v.get("price"), dict) else v.get("price"),
                        "eta": None,
                    })
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Method 2: Try JSON-LD schema
        if not variants:
            schema_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for schema_text in schema_matches:
                try:
                    schema_data = json.loads(schema_text)
                    if isinstance(schema_data, list):
                        schema_data = schema_data[0] if schema_data else {}

                    # ProductGroup with hasVariant (Bambu Lab uses this)
                    if schema_data.get("@type") == "ProductGroup" and "hasVariant" in schema_data:
                        for v in schema_data.get("hasVariant", []):
                            variant_name = v.get("name", "Default")
                            # Clean up variant name (remove long product prefix)
                            if " / " in variant_name:
                                parts = variant_name.split(" / ")
                                variant_name = parts[0].split(" - ")[-1] if " - " in parts[0] else parts[0]

                            offers = v.get("offers", {})
                            availability = offers.get("availability", "")
                            in_stock = "InStock" in str(availability)
                            variants.append({
                                "name": variant_name,
                                "in_stock": in_stock,
                                "price": offers.get("price"),
                                "eta": None,
                            })
                        if variants:
                            break

                    # Standard Product with offers (fallback)
                    elif schema_data.get("@type") == "Product" or "offers" in schema_data:
                        offers = schema_data.get("offers", [])
                        if isinstance(offers, dict):
                            offers = [offers]

                        for offer in offers:
                            variant_name = offer.get("name", offer.get("sku", "Default"))
                            availability = offer.get("availability", "")
                            in_stock = "InStock" in str(availability)
                            variants.append({
                                "name": variant_name,
                                "in_stock": in_stock,
                                "price": offer.get("price"),
                                "eta": None,
                            })
                        if variants:
                            break
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

    # Extract ETA date if present
    eta = None
    eta_patterns = [
        r'eta[:\s]+(\d{1,2}\s+\w+\s+\d{4})',
        r'eta[:\s]+(\d{1,2}\s+\w+)',
        r'expected[:\s]+(\d{1,2}\s+\w+\s+\d{4})',
        r'verwacht[:\s]+(\d{1,2}\s+\w+\s+\d{4})',
    ]
    for pattern in eta_patterns:
        match = re.search(pattern, markdown_lower)
        if match:
            eta = match.group(1).title()
            break

    # If we found variants from schema, use that data
    if variants:
        any_in_stock = any(v["in_stock"] for v in variants)
        out_of_stock_variants = [v for v in variants if not v["in_stock"]]
        # Add ETA to out of stock variants
        for v in out_of_stock_variants:
            v["eta"] = eta
        return any_in_stock, eta, variants

    # Fallback: text-based detection (no variant info)
    out_of_stock_indicators = [
        'uitverkocht', 'out of stock', 'sold out', 'niet beschikbaar',
        'currently unavailable', 'niet op voorraad',
        'laat het me weten als het beschikbaar is', 'notify me when available',
    ]
    in_stock_indicators = [
        'in winkelwagen', 'add to cart', 'toevoegen aan winkelwagen',
        'in stock', 'op voorraad',
    ]

    for indicator in out_of_stock_indicators:
        if indicator in markdown_lower:
            return False, eta, []

    for indicator in in_stock_indicators:
        if indicator in markdown_lower:
            return True, None, []

    return True, None, []


def parse_products_from_collection(markdown: str) -> list[dict]:
    """Parse basic product info from collection page."""
    products = []

    # Find all product links with full URL
    product_links = re.findall(
        r'\[([^\]]+)\]\((https://eu\.store\.bambulab\.com/nl/products/[^)]+)\)',
        markdown
    )

    # Find all prices in the markdown
    all_prices = re.findall(r'(?:Van\s+)?€\s*([\d,\.]+)\s*EUR?', markdown)

    # Track seen URLs to avoid duplicates
    seen_urls = set()
    price_index = 0

    for name, url in product_links:
        if url in seen_urls:
            continue
        if not any(kw in name.upper() for kw in ['PLA', 'PETG', 'ABS', 'TPU', 'PA', 'ASA', 'PPS', 'PVA', 'SUPPORT']):
            continue

        seen_urls.add(url)

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

        handle = url.split('/products/')[-1] if '/products/' in url else None

        products.append({
            "name": name.strip(),
            "handle": handle,
            "price": price,
            "price_eur": price_eur,
            "url": url,
            "in_stock": True,   # Will be updated when visiting product page
            "eta": None,        # Will be updated if out of stock with ETA
            "variants": [],     # Will contain per-color stock info
        })

    return products




# =============================================================================
# COMPARISON
# =============================================================================

def compare_data(old_data: dict, new_data: dict) -> dict:
    """
    Compare old and new data to detect changes at the variant (color) level.

    Returns:
        dict with lists of: new_items, removed_items, out_of_stock_variants, in_stock_variants, price_changes
    """
    if not old_data:
        return {
            "new_items": [],
            "removed_items": [],
            "out_of_stock_variants": [],
            "in_stock_variants": [],
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

    # Variant-level stock changes
    out_of_stock_variants = []
    in_stock_variants = []
    price_changes = []

    for handle in (old_handles & new_handles):
        old_p = old_products[handle]
        new_p = new_products[handle]
        product_name = new_p.get("name", handle)
        product_url = new_p.get("url")

        # Build variant lookup by name
        old_variants = {v["name"]: v for v in old_p.get("variants", [])}
        new_variants = {v["name"]: v for v in new_p.get("variants", [])}

        # Check each variant for stock changes
        # Only compare variants that exist in BOTH old and new data
        common_variants = set(old_variants.keys()) & set(new_variants.keys())

        for variant_name in common_variants:
            old_v = old_variants[variant_name]
            new_v = new_variants[variant_name]

            old_in_stock = old_v.get("in_stock", True)
            new_in_stock = new_v.get("in_stock", True)

            # Variant went out of stock
            if old_in_stock and not new_in_stock:
                out_of_stock_variants.append({
                    "product_name": product_name,
                    "variant_name": variant_name,
                    "price": new_v.get("price"),
                    "eta": new_v.get("eta") or new_p.get("eta"),
                    "url": product_url,
                })

            # Variant came back in stock (only if it exists in both!)
            if not old_in_stock and new_in_stock:
                in_stock_variants.append({
                    "product_name": product_name,
                    "variant_name": variant_name,
                    "price": new_v.get("price"),
                    "url": product_url,
                })

        # Price change (product level)
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
        "out_of_stock_variants": out_of_stock_variants,
        "in_stock_variants": in_stock_variants,
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

    fieldnames = ["name", "handle", "price", "price_eur", "url", "in_stock", "eta"]

    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
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
    parser.add_argument("--force-notify", action="store_true", help="Send notifications for ALL current out-of-stock items (useful for first run)")
    parser.add_argument("--test-webhook", help="Send test notification to webhook URL")
    parser.add_argument("--loop", type=int, metavar="MINUTES", help="Keep running, scrape every N minutes")
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

        # Force notify: build list of ALL current out-of-stock variants
        if args.force_notify:
            all_out_of_stock = []
            for product in data.get("products", []):
                for variant in product.get("variants", []):
                    if not variant.get("in_stock", True):
                        all_out_of_stock.append({
                            "product_name": product.get("name"),
                            "variant_name": variant.get("name"),
                            "price": variant.get("price"),
                            "eta": variant.get("eta") or product.get("eta"),
                            "url": product.get("url"),
                        })
            changes["out_of_stock_variants"] = all_out_of_stock
            print(f"\n[FORCE] Found {len(all_out_of_stock)} out-of-stock variants to notify")

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

            if changes["out_of_stock_variants"]:
                print(f"\n⚠️ OUT OF STOCK ({len(changes['out_of_stock_variants'])} colors):")
                for item in changes["out_of_stock_variants"]:
                    eta = f" (ETA: {item['eta']})" if item.get('eta') else ""
                    print(f"   ! {item['product_name']} - {item['variant_name']}{eta}")

            if changes["in_stock_variants"]:
                print(f"\n✅ BACK IN STOCK ({len(changes['in_stock_variants'])} colors):")
                for item in changes["in_stock_variants"]:
                    price = f": €{item['price']}" if item.get('price') else ""
                    print(f"   + {item['product_name']} - {item['variant_name']}{price}")

            if changes["price_changes"]:
                print(f"\n💰 PRICE CHANGES ({len(changes['price_changes'])}):")
                for change in changes["price_changes"]:
                    print(f"   {change['name']}: {change['old_price']} → {change['new_price']} ({change['change']})")

        # Send Discord notifications
        if not args.no_notify:
            notify_new_items(config, changes["new_items"])
            notify_removed_items(config, changes["removed_items"])
            notify_out_of_stock(config, changes["out_of_stock_variants"])
            notify_in_stock(config, changes["in_stock_variants"])
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


async def run_loop(interval_minutes: int, args):
    """Run scraper in a loop with specified interval."""
    import time

    print(f"[LOOP] Starting scraper loop - running every {interval_minutes} minutes")
    print(f"[LOOP] Press Ctrl+C to stop\n")

    # Store args for reuse but disable force_notify after first run
    first_run = True

    while True:
        try:
            print(f"\n{'='*60}")
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting scrape...")
            print(f"{'='*60}")

            # Run the scraper (inline version of main logic)
            script_dir = Path(__file__).parent
            output_dir = script_dir / CONFIG["output_dir"]
            json_path = args.output or str(output_dir / CONFIG["output_file"])
            csv_path = str(output_dir / CONFIG["csv_file"])
            config_path = str(script_dir / CONFIG["config_file"])

            config = load_config(config_path)
            old_data = load_previous_data(json_path)

            data = await scrape_collection(CONFIG["collection_url"])

            print(f"\nScraped {data['product_count']} products")

            changes = compare_data(old_data, data)

            # Force notify only on first run if flag was set
            if first_run and args.force_notify:
                all_out_of_stock = []
                for product in data.get("products", []):
                    for variant in product.get("variants", []):
                        if not variant.get("in_stock", True):
                            all_out_of_stock.append({
                                "product_name": product.get("name"),
                                "variant_name": variant.get("name"),
                                "price": variant.get("price"),
                                "eta": variant.get("eta") or product.get("eta"),
                                "url": product.get("url"),
                            })
                changes["out_of_stock_variants"] = all_out_of_stock
                print(f"[FORCE] First run: notifying {len(all_out_of_stock)} out-of-stock variants")
                first_run = False

            # Log changes
            if changes["out_of_stock_variants"]:
                print(f"\n⚠️ OUT OF STOCK ({len(changes['out_of_stock_variants'])} colors)")
            if changes["in_stock_variants"]:
                print(f"✅ BACK IN STOCK ({len(changes['in_stock_variants'])} colors)")
            if changes["price_changes"]:
                print(f"💰 PRICE CHANGES ({len(changes['price_changes'])})")

            # Send notifications
            if not args.no_notify:
                notify_new_items(config, changes["new_items"])
                notify_removed_items(config, changes["removed_items"])
                notify_out_of_stock(config, changes["out_of_stock_variants"])
                notify_in_stock(config, changes["in_stock_variants"])
                notify_price_changes(config, changes["price_changes"])

            save_json(data, json_path)
            if args.csv:
                save_csv(data, csv_path)

            next_run = datetime.now().timestamp() + (interval_minutes * 60)
            next_run_str = datetime.fromtimestamp(next_run).strftime('%H:%M:%S')
            print(f"\n[LOOP] Next run at {next_run_str} (sleeping {interval_minutes} min)")

            time.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            print("\n[LOOP] Stopped by user")
            break
        except Exception as e:
            print(f"[ERROR] Scrape failed: {e}")
            print(f"[LOOP] Retrying in {interval_minutes} minutes...")
            time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    # Quick parse to check for --loop
    import sys
    if "--loop" in sys.argv:
        # Parse args first
        parser = argparse.ArgumentParser()
        parser.add_argument("--output", "-o")
        parser.add_argument("--csv", action="store_true")
        parser.add_argument("--quiet", "-q", action="store_true")
        parser.add_argument("--no-notify", action="store_true")
        parser.add_argument("--force-notify", action="store_true")
        parser.add_argument("--test-webhook")
        parser.add_argument("--loop", type=int, metavar="MINUTES")
        args = parser.parse_args()

        asyncio.run(run_loop(args.loop, args))
    else:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
