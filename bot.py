#!/usr/bin/env python3
import os
import time
import json
import random
import threading
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, jsonify

# ---------- CONFIG (Environment Variables) ----------
# Required:
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Optional (Google Sheets): set GOOGLE_CREDS to the JSON string of the service account key,
# and SHEET_NAME to the spreadsheet title you created (or ID if you prefer to use open_by_key).
GOOGLE_CREDS = os.environ.get("GOOGLE_CREDS")  # JSON text
SHEET_NAME = os.environ.get("SHEET_NAME", "VintedBot")

# Port for Flask (Railway will set PORT)
PORT = int(os.environ.get("PORT", 5000))

if not BOT_TOKEN or not CHAT_ID:
    raise Exception("Missing BOT_TOKEN or CHAT_ID environment variables. Set them on Railway before starting.")

# ---------- Optional Google Sheets setup ----------
sheet = None
if GOOGLE_CREDS:
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        creds_dict = json.loads(GOOGLE_CREDS)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        # Try to open by name, otherwise you may want to use open_by_key and provide SHEET_NAME as the key
        try:
            sh = gc.open(SHEET_NAME)
        except Exception:
            # if SHEET_NAME isn't a name, try by key
            sh = gc.open_by_key(SHEET_NAME)
        # Ensure worksheets exist: Trovati, Comprati, Rivenduti
        def _ensure_ws(name):
            try:
                return sh.worksheet(name)
            except Exception:
                return sh.add_worksheet(title=name, rows="1000", cols="20")
        sheet = {
            "trovati": _ensure_ws("Trovati"),
            "comprati": _ensure_ws("Comprati"),
            "rivenduti": _ensure_ws("Rivenduti")
        }
        print("Google Sheets connected.")
    except Exception as e:
        print("Google Sheets setup failed or GOOGLE_CREDS invalid:", e)
        sheet = None
else:
    print("Google Sheets not configured (GOOGLE_CREDS env missing).")

# ---------- Basic settings ----------
SEEN_FILE = "seen_items.txt"
seen_hashes = set()
recent_items = []  # small memory store for dashboard: list of dicts
MAX_RECENT = 200

# load seen items at startup
if os.path.exists(SEEN_FILE):
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                h = line.strip()
                if h:
                    seen_hashes.add(h)
    except Exception as e:
        print("Could not read seen_items.txt:", e)

# ---------- User-Agent rotation (simple anti-ban) ----------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Mozilla/5.0 (X11; Linux x86_64)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X)",
    "Mozilla/5.0 (Android 10; Mobile)"
]

# ---------- Final search list (brand/category/price) ----------
SEARCHES = [
    {"brand": "Nike", "category": "tuta", "price": 35},
    {"brand": "Nike", "category": "scarpe", "price": 30},
    {"brand": "Nike", "category": "felpa", "price": 30},
    {"brand": "Nike", "category": "maglione", "price": 30},
    {"brand": "Nike", "category": "giacca", "price": 40},

    {"brand": "Ralph Lauren", "category": "tuta", "price": 50},
    {"brand": "Ralph Lauren", "category": "polo", "price": 30},
    {"brand": "Ralph Lauren", "category": "giacca", "price": 60},
    {"brand": "Ralph Lauren", "category": "felpa", "price": 40},
    {"brand": "Ralph Lauren", "category": "maglione", "price": 40},

    {"brand": "Lacoste", "category": "tuta", "price": 50},
    {"brand": "Lacoste", "category": "felpa", "price": 40},
    {"brand": "Lacoste", "category": "maglione", "price": 40},

    {"brand": "Adidas", "category": "tuta", "price": 35},
    {"brand": "Adidas", "category": "felpa", "price": 25},
    {"brand": "Adidas", "category": "maglione", "price": 25},

    {"brand": "Blauer", "category": "giacca", "price": 50},
    {"brand": "Canada Goose", "category": "giacca", "price": 50},

    {"brand": "North Face", "category": "giacca", "price": 35},
    {"brand": "North Face", "category": "felpa", "price": 20},
    {"brand": "North Face", "category": "maglione", "price": 20},
    {"brand": "North Face", "category": "tuta", "price": 30},

    {"brand": "Tommy Hilfiger", "category": "tuta", "price": 40},
    {"brand": "Tommy Hilfiger", "category": "felpa", "price": 30},
    {"brand": "Tommy Hilfiger", "category": "maglione", "price": 30},
]

def build_url(brand: str, category: str, price: int) -> str:
    q = f"{brand} {category}".replace(" ", "+")
    return f"https://www.vinted.it/api/v2/catalog/items?search_text={q}&price_to={price}"

API_URLS = [build_url(s["brand"], s["category"], s["price"]) for s in SEARCHES]

# ---------- Helper: extract best photo url robustly ----------
def extract_photo_url(item: dict) -> Optional[str]:
    # Try multiple common locations
    if not item:
        return None
    # legacy: photos list with url_fullxfull
    photos = item.get("photos") or item.get("photos_urls") or []
    if isinstance(photos, list) and photos:
        # try common keys
        for p in photos:
            if isinstance(p, dict):
                for k in ("url_fullxfull", "url_full", "url", "xlarge", "thumb"):
                    if p.get(k):
                        return p.get(k)
            elif isinstance(p, str):
                return p
    # sometimes vinted returns main_photo or photo dict
    main = item.get("main_photo") or item.get("photo") or item.get("photos_urls")
    if isinstance(main, dict):
        for k in ("url_fullxfull", "url_full", "url", "xlarge"):
            if main.get(k):
                return main.get(k)
    if isinstance(main, str):
        return main
    return None

# ---------- Helper: compute a stable hash for item to avoid duplicates ----------
def item_hash(item: dict) -> str:
    try:
        id_ = str(item.get("id", ""))
        title = item.get("title", "") or ""
        price = str(item.get("price", "") or "")
        return str(abs(hash((id_, title, price))))
    except Exception:
        return str(time.time())

# ---------- Telegram send helper ----------
def send_telegram_message(text: str, photo_url: Optional[str] = None):
    try:
        if photo_url:
            endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            requests.get(endpoint, params={"chat_id": CHAT_ID, "photo": photo_url, "caption": text}, timeout=15)
        else:
            endpoint = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.get(endpoint, params={"chat_id": CHAT_ID, "text": text}, timeout=15)
    except Exception as e:
        print("Telegram send error:", e)

# ---------- Process a single item: notify and save ----------
def process_item(item: dict, search_spec: dict):
    try:
        h = item_hash(item)
        if h in seen_hashes:
            return False
        # add to seen and persist
        seen_hashes.add(h)
        try:
            with open(SEEN_FILE, "a", encoding="utf-8") as f:
                f.write(h + "\n")
        except Exception as e:
            print("Could not write seen file:", e)

        title = item.get("title") or item.get("description") or "Senza titolo"
        price = item.get("price") or item.get("price_amount") or "N/A"
        # Some endpoints store price as dict
        if isinstance(price, dict):
            price = price.get("amount") or price.get("value") or price.get("raw") or "N/A"
        try:
            price_val = float(price)
            price_str = f"{price_val:.2f}"
        except Exception:
            price_str = str(price)

        sizes = item.get("size_title") or item.get("sizes") or "N/A"
        if isinstance(sizes, list):
            sizes = ", ".join(sizes)
        condition = item.get("condition_title") or item.get("condition") or "N/A"
        pid = item.get("id") or item.get("item_id") or ""
        link = f"https://www.vinted.it/items/{pid}" if pid else item.get("url") or item.get("permalink") or "N/A"
        photo_url = extract_photo_url(item)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        text = (
            f"🔥 NUOVA OFFERTA!\n"
            f"{search_spec['brand']} - {search_spec['category']}\n"
            f"💶 Prezzo: {price_str}€\n"
            f"📌 {title}\n"
            f"🎽 Taglia: {sizes}\n"
            f"🔧 Condizione: {condition}\n"
            f"🕒 Trovato il: {timestamp}\n"
            f"🔗 {link}"
        )

        # send to telegram (all photos will be sent in sequence if available)
        if photo_url:
            send_telegram_message(text, photo_url)
        else:
            send_telegram_message(text)

        # write to Google Sheets if configured
        if sheet:
            try:
                sheet["trovati"].append_row([pid, search_spec['brand'], search_spec['category'],
                                            price_str, title, sizes, condition, link, timestamp, "Trovato"])
            except Exception as e:
                print("Error writing to sheet:", e)

        # update recent items for dashboard
        rec = {"id": pid, "brand": search_spec['brand'], "category": search_spec['category'],
               "price": price_str, "title": title, "sizes": sizes, "condition": condition,
               "link": link, "timestamp": timestamp, "state": "Trovato"}
        recent_items.insert(0, rec)
        if len(recent_items) > MAX_RECENT:
            recent_items.pop()

        print("Notified:", title)
        return True
    except Exception as e:
        print("process_item error:", e)
        return False

# ---------- Scraper loop ----------
def scraper_loop():
    print("Scraper started: checking every 15 seconds.")
    while True:
        for spec, url in zip(SEARCHES, API_URLS):
            try:
                headers = {"User-Agent": random.choice(USER_AGENTS)}
                resp = requests.get(url, headers=headers, timeout=20)
                # Try to parse JSON; if not JSON skip
                try:
                    data = resp.json()
                except Exception:
                    print("Non-JSON response from Vinted for URL:", url)
                    continue

                # "items" is the usual container; try alternatives too
                items = data.get("items") or data.get("data") or []
                # if nested structure
                if isinstance(items, dict) and "items" in items:
                    items = items.get("items", [])

                if not items:
                    # no results
                    continue

                for it in items:
                    # sometimes item is nested under 'item' key
                    if isinstance(it, dict) and 'item' in it and isinstance(it['item'], dict):
                        candidate = it['item']
                    else:
                        candidate = it
                    try:
                        process_item(candidate, spec)
                    except Exception as e:
                        print("Error processing candidate:", e)
            except Exception as e:
                print("Request error:", e)
        # fixed wait 15 seconds as requested
        time.sleep(15)

# ---------- Flask dashboard ----------
app = Flask(__name__)

@app.route("/")
def index():
    return jsonify({
        "status": "ok",
        "tracked_total": len(seen_hashes),
        "recent_count": len(recent_items),
        "recent": recent_items[:50]
    })

@app.route("/health")
def health():
    return jsonify({"status": "up", "time": datetime.now().isoformat()})

@app.route("/item/<item_id>")
def get_item(item_id):
    for r in recent_items:
        if str(r.get("id")) == str(item_id):
            return jsonify(r)
    return jsonify({"error": "not found"}), 404

# ---------- Start scraper in background and run Flask ----------
if __name__ == "__main__":
    # start scraper thread
    t = threading.Thread(target=scraper_loop, daemon=True)
    t.start()
    # run flask (Railway will expose this)
    app.run(host="0.0.0.0", port=PORT)