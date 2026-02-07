#!/usr/bin/env python3
"""
Bambu Lab Filament Scraper
==========================
Scrapes filament products from Bambu Lab stores (US, EU, CA, UK, AU, JP, KR, ASIA).
Sends Discord notifications for stock changes, new items, and removed items.

Requirements:
    pip install crawl4ai requests

Usage:
    python scraper.py                      # Scrape EU store (default)
    python scraper.py --stores US,UK       # Scrape specific stores
    python scraper.py --list-stores        # List all configured stores
    python scraper.py --multi-loop         # Run multi-store rotation loop
    python scraper.py --loop 60            # Run EU store every 60 minutes
    python scraper.py --csv                # Also export to CSV
    python scraper.py --no-notify          # Skip Discord notifications

Multi-Store Loop:
    python scraper.py --multi-loop --force-notify

    Runs all enabled stores in a staggered rotation with configurable intervals.
    Edit config.json to enable/disable stores or adjust intervals.

Cron Example (single store):
    0 * * * * cd /path/to/scraper && /usr/bin/python3 scraper.py --csv >> /var/log/bambu-scraper.log 2>&1

Author: HageneeZ
Version: 3.0.0
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
from urllib.parse import urlparse

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig
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
    "collection_url": "https://eu.store.bambulab.com/collections/bambu-lab-3d-printer-filament",
    "output_dir": "data",
    "output_file": "bambulab_filaments.json",
    "csv_file": "bambulab_filaments.csv",
    "config_file": "config.json",  # For webhooks
}

# Store flags for Discord notifications
STORE_FLAGS = {
    "US": "🇺🇸", "EU": "🇪🇺", "CA": "🇨🇦", "UK": "🇬🇧",
    "AU": "🇦🇺", "JP": "🇯🇵", "KR": "🇰🇷", "ASIA": "🌏",
}

# Default store configurations
DEFAULT_STORES = {
    "US": {"url": "https://us.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "EU": {"url": "https://eu.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "CA": {"url": "https://ca.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "UK": {"url": "https://uk.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "AU": {"url": "https://au.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "JP": {"url": "https://jp.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "KR": {"url": "https://kr.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
    "ASIA": {"url": "https://asia.store.bambulab.com/collections/bambu-lab-3d-printer-filament", "enabled": True, "interval_minutes": 7.5},
}

# Default global settings
DEFAULT_GLOBAL_SETTINGS = {
    "delay_between_pages_seconds": 2,
    "delay_between_stores_seconds": 10,
    "browser_timeout_ms": 30000,
    "max_retries": 2,
    "proxy": "",
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
            "stores": DEFAULT_STORES,
            "global_settings": DEFAULT_GLOBAL_SETTINGS,
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

    # Ensure stores and global_settings exist (for backward compatibility)
    if "stores" not in config:
        config["stores"] = DEFAULT_STORES
    if "global_settings" not in config:
        config["global_settings"] = DEFAULT_GLOBAL_SETTINGS

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


def get_enabled_stores(config: dict) -> dict:
    """Return only enabled stores from config."""
    stores = config.get("stores", DEFAULT_STORES)
    return {k: v for k, v in stores.items() if v.get("enabled", True)}


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


def notify_out_of_stock(config: dict, variants: list[dict], store_id: str = "EU") -> None:
    """Send a SINGLE notification for ALL variants that went out of stock."""
    if not config.get("notify_on", {}).get("out_of_stock", True):
        return
    if not variants:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")
    flag = STORE_FLAGS.get(store_id, "🌐")

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
        title=f"{flag} {store_id} - ⚠️ Out of Stock Report ({total_colors} colors)",
        description=description,
        color=COLORS["out_of_stock"],
    )


def notify_in_stock(config: dict, variants: list[dict], store_id: str = "EU") -> None:
    """Send a SINGLE notification for ALL variants that came back in stock."""
    if not config.get("notify_on", {}).get("in_stock", True):
        return
    if not variants:
        return

    webhooks = get_webhooks_for_event(config, "stock_alerts")
    flag = STORE_FLAGS.get(store_id, "🌐")

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
        title=f"{flag} {store_id} - ✅ Back in Stock! ({total_colors} colors)",
        description=description,
        color=COLORS["in_stock"],
    )


def notify_new_items(config: dict, products: list[dict], store_id: str = "EU") -> None:
    """Send notification for newly added products."""
    if not config.get("notify_on", {}).get("new_items", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "new_items")
    flag = STORE_FLAGS.get(store_id, "🌐")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title=f"{flag} {store_id} - 🆕 New Product!",
            description=f"**{product['name']}** has been added to the store",
            color=COLORS["new_item"],
            fields=[
                {"name": "Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_removed_items(config: dict, products: list[dict], store_id: str = "EU") -> None:
    """Send notification for removed products."""
    if not config.get("notify_on", {}).get("removed_items", True):
        return
    if not products:
        return

    webhooks = get_webhooks_for_event(config, "removed_items")
    flag = STORE_FLAGS.get(store_id, "🌐")

    for product in products:
        send_discord_notification(
            webhooks=webhooks,
            title=f"{flag} {store_id} - 🗑️ Product Removed",
            description=f"**{product['name']}** is no longer available",
            color=COLORS["removed_item"],
            fields=[
                {"name": "Last Price", "value": product.get("price", "N/A"), "inline": True},
                {"name": "Handle", "value": product.get("handle", "N/A"), "inline": True},
            ],
            url=product.get("url")
        )


def notify_price_changes(config: dict, changes: list[dict], store_id: str = "EU") -> None:
    """Send notification for price changes."""
    if not config.get("notify_on", {}).get("price_changes", True):
        return
    if not changes:
        return

    webhooks = get_webhooks_for_event(config, "price_changes")
    flag = STORE_FLAGS.get(store_id, "🌐")

    for change in changes:
        is_drop = change.get("change", "").startswith("-")

        send_discord_notification(
            webhooks=webhooks,
            title=f"{flag} {store_id} - {'💰 Price Drop!' if is_drop else '📈 Price Increase'}",
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

async def scrape_store(store_id: str, collection_url: str, config: dict, store_config: dict = None) -> dict:
    """
    Scrape a store collection page and extract product data.
    Visits each product page to check stock status.

    Args:
        store_id: Store identifier (e.g., "EU", "US")
        collection_url: The collection URL to scrape
        config: Configuration dict with global_settings
        store_config: Store-specific configuration dict (optional)

    Returns:
        dict with products list, metadata, and store_id
    """
    global_settings = config.get("global_settings", DEFAULT_GLOBAL_SETTINGS)
    delay_between_pages = global_settings.get("delay_between_pages_seconds", 2)
    browser_timeout = global_settings.get("browser_timeout_ms", 30000)

    # Check for proxy configuration
    proxy = None
    if store_config:
        proxy = store_config.get("proxy")
    if not proxy:
        proxy = global_settings.get("proxy")

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
    )
    if proxy:
        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
            proxy=proxy,
        )
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Using proxy for {store_id}: {proxy.split('@')[-1] if '@' in proxy else proxy}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping {store_id} collection: {collection_url}")

    async with AsyncWebCrawler(
        config=browser_config,
    ) as crawler:
        # First get the collection page to find all products
        result = await crawler.arun(
            url=collection_url,
            bypass_cache=True,
            page_timeout=60000,
        )

        if not result.success:
            raise Exception(f"Failed to fetch page: {result.error_message}")

        # Detect geo-redirect: check if we got redirected to a different store
        parse_url = collection_url
        redirected = False
        expected_host = urlparse(collection_url).netloc
        if result.url:
            actual_host = urlparse(result.url).netloc
            if actual_host != expected_host:
                redirected = True
                parse_url = result.url  # Use actual URL for parsing
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️  GEO-REDIRECT: {store_id} redirected {expected_host} → {actual_host}")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️  Scraping {actual_host} data instead (configure proxy in config.json to fix)")

        # Parse basic product info from collection (using actual URL after redirect)
        products = parse_products_from_collection(result.markdown, parse_url)

        # Now visit each product page to check stock status
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Checking stock for {len(products)} products...")

        for i, product in enumerate(products):
            if product.get("url"):
                try:
                    print(f"  [{i+1}/{len(products)}] {product['name']}...", end=" ", flush=True)
                    stock_result = await crawler.arun(
                        url=product["url"],
                        bypass_cache=True,
                        page_timeout=browser_timeout,
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
                            spool_count = sum(1 for v in variants if v.get("spool_type") == "spool")
                            refill_count = sum(1 for v in variants if v.get("spool_type") == "refill")
                            type_info = ""
                            if spool_count or refill_count:
                                type_info = f" [{spool_count} spool, {refill_count} refill]"
                            if out_count == 0:
                                print(f"✓ ({len(variants)} variants{type_info})")
                            elif in_count == 0:
                                print(f"✗ ALL OUT OF STOCK ({len(variants)} variants{type_info})" + (f" ETA: {eta}" if eta else ""))
                            else:
                                print(f"⚠ {out_count}/{len(variants)} variants out of stock{type_info}")
                        else:
                            if any_in_stock:
                                print("✓")
                            else:
                                print(f"✗ OUT OF STOCK" + (f" (ETA: {eta})" if eta else ""))
                    else:
                        print("? (failed)")
                except Exception as e:
                    print(f"? (error: {e})")

                # Delay between product pages
                if i < len(products) - 1:
                    await asyncio.sleep(delay_between_pages)

        return {
            "store_id": store_id,
            "scraped_at": datetime.now().isoformat(),
            "source_url": collection_url,
            "product_count": len(products),
            "products": products,
        }


async def scrape_collection(url: str) -> dict:
    """
    LEGACY: Scrape a collection page (backward compatibility).
    Use scrape_store() for new code.
    """
    config = {"global_settings": DEFAULT_GLOBAL_SETTINGS}
    return await scrape_store("EU", url, config)


def _parse_spool_type(raw: str) -> str:
    """Parse spool type from variant name part (e.g. 'Refill', 'Filament with spool')."""
    raw_lower = raw.strip().lower()
    if raw_lower in ("bijvullen", "navulling", "refill"):
        return "refill"
    elif "spool" in raw_lower or "spoel" in raw_lower:
        return "spool"
    return raw.strip()


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
                    spool_type = None
                    # Parse spool type from variant name if present
                    if " / " in variant_name:
                        parts = variant_name.split(" / ")
                        variant_name = parts[0].split(" - ")[-1] if " - " in parts[0] else parts[0]
                        if len(parts) > 1:
                            spool_type = _parse_spool_type(parts[1])
                    # Check availability - could be "available", "in_stock", etc.
                    available = v.get("available", v.get("availableForSale", v.get("in_stock", True)))
                    variants.append({
                        "name": variant_name,
                        "in_stock": bool(available),
                        "price": v.get("price", {}).get("amount") if isinstance(v.get("price"), dict) else v.get("price"),
                        "spool_type": spool_type,
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
                            spool_type = None
                            # Parse variant name: "PLA Basic - Jadewit (10100) / Bijvullen / 1kg"
                            if " / " in variant_name:
                                parts = variant_name.split(" / ")
                                variant_name = parts[0].split(" - ")[-1] if " - " in parts[0] else parts[0]
                                if len(parts) > 1:
                                    spool_type = _parse_spool_type(parts[1])

                            offers = v.get("offers", {})
                            availability = offers.get("availability", "")
                            in_stock = "InStock" in str(availability)
                            variants.append({
                                "name": variant_name,
                                "in_stock": in_stock,
                                "price": offers.get("price"),
                                "spool_type": spool_type,
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


def parse_products_from_collection(markdown: str, collection_url: str) -> list[dict]:
    """Parse basic product info from collection page."""
    products = []

    # Extract base URL from collection URL
    parsed = urlparse(collection_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Primary: match expected store URL
    escaped_base = re.escape(base_url)
    product_links = re.findall(
        rf'\[([^\]]+)\]\(({escaped_base}(?:/[a-z]{{2}})?/products/[^)]+)\)',
        markdown
    )

    # Fallback: if no products found, try matching ANY bambulab store URL
    if not product_links:
        product_links = re.findall(
            r'\[([^\]]+)\]\((https://[a-z]+\.store\.bambulab\.com(?:/[a-z]{2})?/products/[^)]+)\)',
            markdown
        )

    # Find all prices in the markdown (support multiple currencies)
    all_prices = re.findall(r'(?:Van\s+)?[€$£¥₩]\s*([\d,\.]+)', markdown)

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
        price_numeric = None
        if price_index < len(all_prices):
            price_str = all_prices[price_index]
            # Detect currency from original markdown (simple heuristic)
            currency_symbol = "€"  # Default
            if "$" in markdown[max(0, markdown.find(price_str) - 5):markdown.find(price_str) + len(price_str) + 5]:
                currency_symbol = "$"
            elif "£" in markdown[max(0, markdown.find(price_str) - 5):markdown.find(price_str) + len(price_str) + 5]:
                currency_symbol = "£"
            elif "¥" in markdown[max(0, markdown.find(price_str) - 5):markdown.find(price_str) + len(price_str) + 5]:
                currency_symbol = "¥"
            elif "₩" in markdown[max(0, markdown.find(price_str) - 5):markdown.find(price_str) + len(price_str) + 5]:
                currency_symbol = "₩"

            price = f"{currency_symbol}{price_str}"
            try:
                price_numeric = float(price_str.replace(',', '.'))
            except ValueError:
                pass
            price_index += 1

        handle = url.split('/products/')[-1] if '/products/' in url else None

        products.append({
            "name": name.strip(),
            "handle": handle,
            "price": price,
            "price_eur": price_numeric,  # Keep field name for backward compat, but it's now multi-currency
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

        # Build variant lookup by name + spool_type
        def _variant_key(v):
            return (v["name"], v.get("spool_type") or "")

        old_variants = {_variant_key(v): v for v in old_p.get("variants", [])}
        new_variants = {_variant_key(v): v for v in new_p.get("variants", [])}

        # Check each variant for stock changes
        # Only compare variants that exist in BOTH old and new data
        common_variants = set(old_variants.keys()) & set(new_variants.keys())

        for vkey in common_variants:
            old_v = old_variants[vkey]
            new_v = new_variants[vkey]
            variant_name = vkey[0]
            spool_type = new_v.get("spool_type")
            variant_label = f"{variant_name} ({spool_type})" if spool_type else variant_name

            old_in_stock = old_v.get("in_stock", True)
            new_in_stock = new_v.get("in_stock", True)

            # Variant went out of stock
            if old_in_stock and not new_in_stock:
                out_of_stock_variants.append({
                    "product_name": product_name,
                    "variant_name": variant_label,
                    "spool_type": spool_type,
                    "price": new_v.get("price"),
                    "eta": new_v.get("eta") or new_p.get("eta"),
                    "url": product_url,
                })

            # Variant came back in stock (only if it exists in both!)
            if not old_in_stock and new_in_stock:
                in_stock_variants.append({
                    "product_name": product_name,
                    "variant_name": variant_label,
                    "spool_type": spool_type,
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

    fieldnames = ["name", "handle", "price", "price_eur", "url", "in_stock", "eta", "spool_type"]

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
    parser.add_argument("--loop", type=int, metavar="MINUTES", help="Keep running, scrape every N minutes (EU store only)")
    parser.add_argument("--multi-loop", action="store_true", help="Run multi-store staggered rotation loop")
    parser.add_argument("--stores", help="Comma-separated list of stores to scrape (e.g., EU,US,UK)")
    parser.add_argument("--list-stores", action="store_true", help="List all configured stores and exit")
    args = parser.parse_args()

    # Setup paths
    script_dir = Path(__file__).parent
    output_dir = script_dir / CONFIG["output_dir"]
    config_path = str(script_dir / CONFIG["config_file"])

    # Load config
    config = load_config(config_path)

    # List stores mode
    if args.list_stores:
        stores = config.get("stores", DEFAULT_STORES)
        print("\n=== Configured Stores ===")
        for store_id, store_config in stores.items():
            flag = STORE_FLAGS.get(store_id, "🌐")
            enabled = "✓" if store_config.get("enabled", True) else "✗"
            interval = store_config.get("interval_minutes", 7.5)
            url = store_config.get("url", "N/A")
            print(f"{flag} {store_id:8s} {enabled} - {interval:5.1f} min - {url}")
        return 0

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

    # Parse stores filter if provided
    store_filter = None
    if args.stores:
        store_filter = [s.strip().upper() for s in args.stores.split(",")]
        print(f"[FILTER] Scraping only: {', '.join(store_filter)}")

    # Determine which stores to scrape
    if store_filter:
        # Use filter
        all_stores = config.get("stores", DEFAULT_STORES)
        stores_to_scrape = {k: v for k, v in all_stores.items() if k in store_filter and v.get("enabled", True)}
        if not stores_to_scrape:
            print(f"[ERROR] No enabled stores match filter: {store_filter}")
            return 1
    else:
        # Default to EU for backward compatibility
        stores_to_scrape = {"EU": config.get("stores", DEFAULT_STORES).get("EU", DEFAULT_STORES["EU"])}

    try:
        # Scrape each store
        for store_id, store_config in stores_to_scrape.items():
            collection_url = store_config.get("url", CONFIG["collection_url"])

            if len(stores_to_scrape) > 1:
                print(f"\n{'='*60}")
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Scraping store: {STORE_FLAGS.get(store_id, '🌐')} {store_id}")
                print(f"{'='*60}")

            # Setup paths for this store
            json_path = args.output or str(output_dir / f"{store_id}_filaments.json")
            csv_path = str(output_dir / f"{store_id}_filaments.csv")

            # Load previous data for comparison
            old_data = load_previous_data(json_path)

            # Scrape the collection
            data = await scrape_store(store_id, collection_url, config, store_config)

            if not args.quiet:
                print(f"\n{'='*50}")
                print(f"Scraped {data['product_count']} products from {store_id}")
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
                            spool_type = variant.get("spool_type")
                            variant_label = f"{variant.get('name')} ({spool_type})" if spool_type else variant.get("name")
                            all_out_of_stock.append({
                                "product_name": product.get("name"),
                                "variant_name": variant_label,
                                "spool_type": spool_type,
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
                        price = f": {item['price']}" if item.get('price') else ""
                        print(f"   + {item['product_name']} - {item['variant_name']}{price}")

                if changes["price_changes"]:
                    print(f"\n💰 PRICE CHANGES ({len(changes['price_changes'])}):")
                    for change in changes["price_changes"]:
                        print(f"   {change['name']}: {change['old_price']} → {change['new_price']} ({change['change']})")

            # Send Discord notifications
            if not args.no_notify:
                notify_new_items(config, changes["new_items"], store_id)
                notify_removed_items(config, changes["removed_items"], store_id)
                notify_out_of_stock(config, changes["out_of_stock_variants"], store_id)
                notify_in_stock(config, changes["in_stock_variants"], store_id)
                notify_price_changes(config, changes["price_changes"], store_id)

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
    """Run scraper in a loop with specified interval (single store - EU)."""
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
                            spool_type = variant.get("spool_type")
                            variant_label = f"{variant.get('name')} ({spool_type})" if spool_type else variant.get("name")
                            all_out_of_stock.append({
                                "product_name": product.get("name"),
                                "variant_name": variant_label,
                                "spool_type": spool_type,
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
                notify_new_items(config, changes["new_items"], "EU")
                notify_removed_items(config, changes["removed_items"], "EU")
                notify_out_of_stock(config, changes["out_of_stock_variants"], "EU")
                notify_in_stock(config, changes["in_stock_variants"], "EU")
                notify_price_changes(config, changes["price_changes"], "EU")

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


async def run_multi_store_loop(args):
    """Run scraper in staggered rotation across enabled stores."""
    import time as _time

    script_dir = Path(__file__).parent
    config_path = str(script_dir / CONFIG["config_file"])
    config = load_config(config_path)

    stores = get_enabled_stores(config)
    if not stores:
        print("[ERROR] No enabled stores found in config!")
        return

    store_ids = list(stores.keys())
    print(f"[MULTI] Starting staggered rotation for {len(store_ids)} stores: {', '.join(store_ids)}")

    # Build schedule queue: [[next_run_time, store_id, interval]]
    store_queue = []
    global_settings = config.get("global_settings", DEFAULT_GLOBAL_SETTINGS)

    for i, store_id in enumerate(store_ids):
        store_config = stores[store_id]
        interval = store_config.get("interval_minutes", 7.5) * 60
        # Stagger: first store starts immediately, others are spaced out
        offset = i * global_settings.get("delay_between_stores_seconds", 10)
        store_queue.append([_time.time() + offset, store_id, interval])

    first_run = True

    while True:
        # Sort by next run time
        store_queue.sort(key=lambda x: x[0])
        next_time, store_id, interval = store_queue[0]

        # Wait until it's time
        sleep_for = max(0, next_time - _time.time())
        if sleep_for > 0:
            next_str = datetime.fromtimestamp(next_time).strftime('%H:%M:%S')
            print(f"\n[MULTI] Next: {STORE_FLAGS.get(store_id, '🌐')} {store_id} at {next_str} (sleeping {sleep_for:.0f}s)")
            await asyncio.sleep(sleep_for)

        # Reload config each cycle (allows live changes)
        config = load_config(config_path)
        stores = get_enabled_stores(config)

        # Skip if store was disabled
        if store_id not in stores:
            store_queue.pop(0)
            continue

        store_config = stores[store_id]
        collection_url = store_config["url"]

        print(f"\n{'='*60}")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {STORE_FLAGS.get(store_id, '🌐')} {store_id}: Starting scrape...")
        print(f"{'='*60}")

        output_dir = script_dir / CONFIG["output_dir"]
        json_path = str(output_dir / f"{store_id}_filaments.json")

        try:
            old_data = load_previous_data(json_path)
            data = await scrape_store(store_id, collection_url, config, store_config)

            changes = compare_data(old_data, data)

            # Force notify on first run
            if first_run and args.force_notify:
                all_oos = []
                for product in data.get("products", []):
                    for variant in product.get("variants", []):
                        if not variant.get("in_stock", True):
                            spool_type = variant.get("spool_type")
                            label = f"{variant.get('name')} ({spool_type})" if spool_type else variant.get("name")
                            all_oos.append({
                                "product_name": product.get("name"),
                                "variant_name": label,
                                "spool_type": spool_type,
                                "price": variant.get("price"),
                                "eta": variant.get("eta") or product.get("eta"),
                                "url": product.get("url"),
                            })
                changes["out_of_stock_variants"] = all_oos
                first_run = False

            # Log
            if changes["out_of_stock_variants"]:
                print(f"⚠️ {store_id}: {len(changes['out_of_stock_variants'])} colors out of stock")
            if changes["in_stock_variants"]:
                print(f"✅ {store_id}: {len(changes['in_stock_variants'])} colors back in stock")
            if changes["price_changes"]:
                print(f"💰 {store_id}: {len(changes['price_changes'])} price changes")

            # Notify
            if not args.no_notify:
                notify_new_items(config, changes["new_items"], store_id)
                notify_removed_items(config, changes["removed_items"], store_id)
                notify_out_of_stock(config, changes["out_of_stock_variants"], store_id)
                notify_in_stock(config, changes["in_stock_variants"], store_id)
                notify_price_changes(config, changes["price_changes"], store_id)

            save_json(data, json_path)
            if args.csv:
                csv_path = str(output_dir / f"{store_id}_filaments.csv")
                save_csv(data, csv_path)

        except Exception as e:
            print(f"[ERROR] {store_id}: Scrape failed: {e}")

        # Reschedule this store
        new_interval = store_config.get("interval_minutes", 7.5) * 60
        store_queue[0] = [_time.time() + new_interval, store_id, new_interval]


if __name__ == "__main__":
    # Quick parse to check for loop modes
    import sys

    # Parse args
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "-o")
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--force-notify", action="store_true")
    parser.add_argument("--test-webhook")
    parser.add_argument("--loop", type=int, metavar="MINUTES")
    parser.add_argument("--multi-loop", action="store_true")
    parser.add_argument("--stores")
    parser.add_argument("--list-stores", action="store_true")
    args = parser.parse_args()

    if args.multi_loop:
        # Multi-store staggered rotation
        asyncio.run(run_multi_store_loop(args))
    elif args.loop:
        # Single-store loop (backward compatibility)
        asyncio.run(run_loop(args.loop, args))
    else:
        # Single run
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
