import html
import logging
import os
import re
import time
import hmac
import hashlib
import urllib.parse
from typing import Dict, Optional, Tuple

import requests
from telebot import TeleBot
from telebot.types import Message
from dotenv import load_dotenv
from flask import Flask, request
from threading import Thread

# Setup logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Load environment
load_dotenv(dotenv_path=".env", override=True)

# Validate environment
def validate_env_variables() -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
    required = ["BOT_TOKEN", "CHANNEL_USERNAME", "ALI_APP_KEY", "ALI_APP_SECRET"]
    optional = ["OPENAI_API_KEY"]

    loaded, optional_values, missing = {}, {}, []

    logger.info("🔍 Checking environment variables…")
    for var in required:
        val = os.getenv(var)
        if val:
            logger.info("✅ %s loaded (%d chars)", var, len(val))
            loaded[var] = val
        else:
            logger.error("❌ %s is missing!", var)
            missing.append(var)

    for var in optional:
        val = os.getenv(var)
        optional_values[var] = val
        if val:
            logger.info("ℹ️ %s loaded (%d chars)", var, len(val))
        else:
            logger.warning("⚠️ %s not set")

    if missing:
        raise EnvironmentError("Missing required environment variables: " + ", ".join(missing))

    logger.info("------------------------------------------------")
    return loaded, optional_values

REQUIRED, OPTIONAL = validate_env_variables()

BOT_TOKEN = REQUIRED["BOT_TOKEN"]
CHANNEL_USERNAME = REQUIRED["CHANNEL_USERNAME"]
ALI_APP_KEY = REQUIRED["ALI_APP_KEY"]
ALI_APP_SECRET = REQUIRED["ALI_APP_SECRET"]
OPENAI_API_KEY = OPTIONAL.get("OPENAI_API_KEY") or ""

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8"
})

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

def http_get(url: str, timeout: int = 20) -> requests.Response:
    return SESSION.get(url, timeout=timeout, allow_redirects=True)

def resolve_url(url: str) -> str:
    try:
        resolved = http_get(url).url
        logger.info("Resolved to: %s", resolved)
        return resolved.replace("aliexpress.us", "aliexpress.com")
    except requests.RequestException as exc:
        logger.warning("Failed to resolve URL: %s", exc)
        return url

def extract_pid(url: str) -> Optional[str]:
    match = re.search(r"/item/(\d{6,20})\.html", url)
    if match:
        pid = match.group(1)
        logger.info("Extracted product ID: %s", pid)
        return pid

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r"\D", "", query[key][0])
            logger.info("Extracted product ID from query: %s", pid)
            return pid
    return None

def ali_sign(params: Dict[str, Optional[str]], app_secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(app_secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()

def ali_productdetail(product_id: str) -> Optional[Dict[str, Optional[str]]]:
    ts = str(int(time.time() * 1000))
    params = {
        "app_key": ALI_APP_KEY,
        "method": "aliexpress.affiliate.productdetail.get",
        "sign_method": "sha256",
        "timestamp": ts,
        "product_ids": product_id,
        "target_currency": "ILS",
        "target_language": "HE",
        "need_promotion_link": "false",
    }
    params["sign"] = ali_sign(params, ALI_APP_SECRET)

    try:
        response = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.info("Full API response: %s", data)
        products = data.get("aliexpress_affiliate_productdetail_get_response", {}).get("result", {}).get("products", [])
        if not products:
            logger.warning("No products returned for product_id: %s", product_id)
            return None
        return {
            "title": products[0].get("product_title"),
            "image": products[0].get("product_main_image_url"),
            "rating": products[0].get("evaluate_rate"),
            "price": products[0].get("target_sale_price"),
            "orders": products[0].get("sale_count"),
        }
    except Exception as e:
        logger.error("API error: %s", e)
        return None

def pull_product(url: str) -> Optional[Dict[str, Optional[str]]]:
    product_id = extract_pid(resolve_url(url))
    if not product_id:
        return None
    return ali_productdetail(product_id)

@bot.message_handler(commands=['start'])
def handle_start(message: Message):
    bot.reply_to(message, "היי! אני חי ובועט 🦾 שלח לי קישור שיווק שותפים של אלי אקספרס 📦")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and "aliexpress" in m.text.lower())
def handle_link(message: Message):
    link = message.text.strip()
    data = pull_product(link)
    if not data:
        bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
        return
    text = f"<b>{html.escape(data['title'])}</b>\n\n" \
           f"⭐ דירוג: {html.escape(data['rating'] or 'N/A')}\n" \
           f"💰 מחיר: {html.escape(data['price'] or 'N/A')} ₪\n" \
           f"📦 הזמנות: {html.escape(data['orders'] or 'N/A')}\n\n" \
           f"🔗 <a href='{html.escape(link)}'>קנה עכשיו באלי אקספרס</a>"
    if data.get("image"):
        bot.send_photo(CHANNEL_USERNAME, data["image"], caption=text, parse_mode="HTML")
    else:
        bot.send_message(CHANNEL_USERNAME, text, parse_mode="HTML")
    bot.reply_to(message, "✅ פורסם לערוץ בהצלחה!")

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = Message.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "Unsupported Media Type", 415

def main():
    keep_alive()
    logger.info("✅ Bot is running with Webhook!")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
