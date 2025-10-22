import os
import re
import time
import hmac
import html
import logging
import hashlib
import requests
import urllib.parse

from typing import Optional, Dict
from flask import Flask, request
from telebot import TeleBot, types
from dotenv import load_dotenv
from threading import Thread

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import openai as openai_legacy
except ImportError:
    openai_legacy = None

# Setup
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
ALI_APP_KEY = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# OpenAI Setup
OPENAI_CLIENT = None
if OPENAI_API_KEY:
    if OpenAI:
        OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)
        OPENAI_MODERN = True
    elif openai_legacy:
        openai_legacy.api_key = OPENAI_API_KEY
        OPENAI_CLIENT = openai_legacy
        OPENAI_MODERN = False
    else:
        logger.warning("⚠️ OpenAI library not found")

def generate_hebrew_description(title: str) -> str:
    if not OPENAI_CLIENT:
        return f"{title} - מוצר איכותי במיוחד ✨"

    prompt = f"כתוב תיאור שיווקי קצר בעברית למוצר בשם: {title}. התיאור צריך להיות מושך, אמין וללא הגזמות."

    try:
        if 'OPENAI_MODERN' in globals() and OPENAI_MODERN:
            res = OPENAI_CLIENT.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים בעברית"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=100,
                temperature=0.8,
            )
            return res.choices[0].message.content.strip()
        else:
            res = OPENAI_CLIENT.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים בעברית"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=100,
                temperature=0.8,
            )
            return res.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("Fallback description used: %s", e)
        return f"{title} - מוצר איכותי במיוחד ✨"

# Resolve shortlink
def http_get(url: str, timeout: int = 15) -> requests.Response:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session.get(url, timeout=timeout, allow_redirects=True)

def extract_pid(url: str) -> Optional[str]:
    match = re.search(r'/item/(\d{6,20})\.html', url)
    if match:
        return match.group(1)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r'\D', '', query[key][0])
            return pid
    return None

def ali_sign(params: dict, app_secret: str) -> str:
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
        res = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
        res.raise_for_status()
        data = res.json()
        product = data.get("aliexpress_affiliate_productdetail_get_response", {}).get("result", {}).get("products", [])[0]
        return {
            "title": product.get("product_title"),
            "image": product.get("product_main_image_url"),
            "price": product.get("target_sale_price"),
            "rating": product.get("evaluate_rate"),
            "orders": product.get("sale_count"),
        }
    except Exception as e:
        logger.error("API error: %s", e)
        return None

def pull_product(url: str) -> Optional[Dict[str, Optional[str]]]:
    try:
        resolved = http_get(url).url
        logger.info("Resolved to: %s", resolved)
        product_id = extract_pid(resolved)
        if not product_id:
            return None
        return ali_productdetail(product_id)
    except Exception as e:
        logger.error("Link resolving failed: %s", e)
        return None

# Telegram Handlers
@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    bot.reply_to(message, "היי! שלח לי קישור שותפים מאלי אקספרס ואשלח אותו לערוץ עם תיאור שיווקי 🤖")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and "aliexpress" in m.text.lower())
def handle_affiliate_link(message: types.Message):
    link = message.text.strip()
    data = pull_product(link)
    if not data:
        bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
        return

    desc = generate_hebrew_description(data["title"] or "המוצר הזה")

    text = f"<b>{html.escape(data['title'])}</b>\n\n" +            f"{html.escape(desc)}\n\n" +            f"⭐ דירוג: {html.escape(str(data['rating']))}\n" +            f"💰 מחיר: {html.escape(str(data['price']))} ₪\n" +            f"📦 הזמנות: {html.escape(str(data['orders']))}\n\n" +            f"🔗 <a href='{html.escape(link)}'>לרכישה</a>"

    if data.get("image"):
        bot.send_photo(CHANNEL_USERNAME, data["image"], caption=text, parse_mode="HTML")
    else:
        bot.send_message(CHANNEL_USERNAME, text, parse_mode="HTML")

    bot.reply_to(message, "✅ פורסם לערוץ!")

# Webhook (optional)
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_str = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "", 200
    return "Unsupported", 415

def main():
    keep_alive()
    logger.info("🚀 Bot is running with webhook and OpenAI")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
