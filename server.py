#!/usr/bin/env python3
"""
Bambu Lab Filament Scraper - Dashboard Server
Port: 5300
"""
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

# Paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = BASE_DIR / "config.json"
DASHBOARD_PATH = BASE_DIR / "dashboard.html"

# State
scraper_process = None
scraper_log = deque(maxlen=500)  # Last 500 lines
scraper_status = {"running": False, "current_store": None, "started_at": None}


def load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except:
        return {}


def save_config(config):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def get_store_status():
    """Get status of all stores including last scrape data."""
    config = load_config()
    stores = config.get("stores", {})
    result = {}

    for store_id, store_config in stores.items():
        data_file = DATA_DIR / f"{store_id}_filaments.json"
        last_scrape = None
        product_count = 0

        if data_file.exists():
            try:
                with open(data_file, 'r') as f:
                    data = json.load(f)
                    last_scrape = data.get("scraped_at")
                    product_count = data.get("product_count", 0)
            except:
                pass

        result[store_id] = {
            "url": store_config.get("url", ""),
            "enabled": store_config.get("enabled", True),
            "interval_minutes": store_config.get("interval_minutes", 7.5),
            "last_scrape": last_scrape,
            "product_count": product_count,
        }

    return result


def read_scraper_output(process):
    """Read scraper output in background thread."""
    for line in iter(process.stdout.readline, ''):
        if line:
            timestamp = datetime.now().strftime('%H:%M:%S')
            scraper_log.append(f"[{timestamp}] {line.rstrip()}")
    process.stdout.close()


@app.route('/')
def dashboard():
    return send_file(DASHBOARD_PATH)


@app.route('/api/stores')
def api_stores():
    return jsonify(get_store_status())


@app.route('/api/stores/<store_id>/config', methods=['POST'])
def api_store_config(store_id):
    config = load_config()
    stores = config.get("stores", {})

    if store_id not in stores:
        return jsonify({"error": "Store not found"}), 404

    data = request.json
    if "enabled" in data:
        stores[store_id]["enabled"] = bool(data["enabled"])
    if "interval_minutes" in data:
        val = float(data["interval_minutes"])
        stores[store_id]["interval_minutes"] = max(5.0, min(120.0, val))

    config["stores"] = stores
    save_config(config)
    return jsonify({"ok": True})


@app.route('/api/stores/<store_id>/data')
def api_store_data(store_id):
    data_file = DATA_DIR / f"{store_id}_filaments.json"
    if not data_file.exists():
        return jsonify({"error": "No data yet"}), 404
    with open(data_file, 'r') as f:
        return jsonify(json.load(f))


@app.route('/api/scraper/start', methods=['POST'])
def api_scraper_start():
    global scraper_process

    if scraper_process and scraper_process.poll() is None:
        return jsonify({"error": "Already running"}), 400

    scraper_log.clear()
    scraper_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Starting multi-store scraper loop...")

    scraper_process = subprocess.Popen(
        ["python", str(BASE_DIR / "scraper.py"), "--multi-loop", "--csv"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(BASE_DIR),
    )

    scraper_status["running"] = True
    scraper_status["started_at"] = datetime.now().isoformat()

    thread = threading.Thread(target=read_scraper_output, args=(scraper_process,), daemon=True)
    thread.start()

    return jsonify({"ok": True, "pid": scraper_process.pid})


@app.route('/api/scraper/stop', methods=['POST'])
def api_scraper_stop():
    global scraper_process

    if not scraper_process or scraper_process.poll() is not None:
        scraper_status["running"] = False
        return jsonify({"error": "Not running"}), 400

    scraper_process.terminate()
    try:
        scraper_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        scraper_process.kill()

    scraper_status["running"] = False
    scraper_status["current_store"] = None
    scraper_log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Scraper stopped")

    return jsonify({"ok": True})


@app.route('/api/scraper/status')
def api_scraper_status():
    running = scraper_process is not None and scraper_process.poll() is None
    scraper_status["running"] = running
    return jsonify(scraper_status)


@app.route('/api/log')
def api_log():
    return jsonify({"lines": list(scraper_log)})


@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'GET':
        config = load_config()
        return jsonify(config.get("global_settings", {}))

    config = load_config()
    data = request.json

    settings = config.get("global_settings", {})
    if "delay_between_pages_seconds" in data:
        settings["delay_between_pages_seconds"] = max(0, min(30, int(data["delay_between_pages_seconds"])))
    if "delay_between_stores_seconds" in data:
        settings["delay_between_stores_seconds"] = max(0, min(120, int(data["delay_between_stores_seconds"])))

    config["global_settings"] = settings
    save_config(config)
    return jsonify({"ok": True})


if __name__ == '__main__':
    DATA_DIR.mkdir(exist_ok=True)
    print(f"Bambu Lab Dashboard: http://localhost:5300")
    app.run(host='0.0.0.0', port=5300, debug=False)
