import os
import re
import hmac
import time
import hashlib
import logging
import requests
import urllib.parse
import json

from dotenv import load_dotenv
from telebot import TeleBot, types
from telebot.types import Message
from flask import Flask, request
from threading import Thread
from openai import OpenAI
from typing import Optional, Dict

# --- הגדרות בסיס ---
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
ALI_APP_KEY = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# --- חיבור OpenAI (לקוח, לא דורס את המודול) ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Webhook ---
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"


@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "Unsupported Media Type", 415


# --- עיבוד קישור (הגדרה אחת בלבד!) ---
def resolve_url(url: str) -> str:
    """מנסה לפתור קישור מקוצר לקישור המלא. אם נכשל - מחזיר את המקורי."""
    try:
        response = requests.get(url, timeout=10, allow_redirects=True)
        return response.url
    except Exception as e:
        logger.warning(f"⚠️ שגיאה ב-resolve_url: {e}")
        return url


def extract_pid(url: str) -> Optional[str]:
    resolved = resolve_url(url)
    logger.info(f"Resolved to: {resolved}")

    match = re.search(r"/item/(\d+)\.html", resolved)
    if match:
        return match.group(1)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(resolved).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r"\D", "", query[key][0])
            if pid:
                return pid

    match = re.search(r"/(\d{10,20})", resolved)
    if match:
        return match.group(1)

    logger.warning(f"⚠️ לא נמצא product_id מתוך: {resolved}")
    return None


def ali_sign(params: Dict[str, Optional[str]], app_secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(app_secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()


def ali_productdetail(product_id: str) -> Optional[Dict[str, Optional[str]]]:
    ts = str(int(time.time() * 1000))
    params: Dict[str, Optional[str]] = {
        "app_key": ALI_APP_KEY,
        "method": "aliexpress.affiliate.productdetail.get",
        "sign_method": "sha256",
        "timestamp": ts,
        "product_ids": product_id,
        "target_currency": "ILS",
        "target_language": "HE",
        "need_promotion_link": "true",
    }
    params["sign"] = ali_sign(params, ALI_APP_SECRET)

    try:
        res = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
        res.raise_for_status()
        data = res.json()
        logger.info("📦 תשובת API מלאה:\n%s", json.dumps(data, indent=2, ensure_ascii=False))

        products_container = (
            data.get("aliexpress_affiliate_productdetail_get_response", {})
            .get("result", {})
            .get("products")
        )

        if isinstance(products_container, dict) and "product" in products_container:
            products = products_container["product"]
        elif isinstance(products_container, list):
            products = products_container
        else:
            logger.error("❌ No valid product list for %s", product_id)
            return None

        if not isinstance(products, list) or not products:
            logger.error("❌ Empty product list for %s", product_id)
            return None

        product = products[0]

    except Exception as exc:
        logger.error(f"❌ API error: {exc}")
        return None

    return {
        "title": product.get("product_title"),
        "image": product.get("product_main_image_url"),
        "rating": product.get("evaluate_rate"),
        "price": product.get("target_sale_price"),
        "orders": product.get("lastest_volume") or product.get("sale_count"),
        "link": product.get("promotion_link"),
    }


def pull_product(url: str) -> Optional[Dict[str, Optional[str]]]:
    try:
        product_id = extract_pid(resolve_url(url))
        if not product_id:
            logger.warning("Could not extract product id from %s", url)
            return None
        return ali_productdetail(product_id)
    except Exception as e:
        logger.error(f"⚠️ שגיאה ב-pull_product: {e}")
        return None


# --- יצירת תיאור שיווקי עם OpenAI (תחביר חדש ותקין) ---
def generate_description(data: Dict[str, Optional[str]]) -> Optional[str]:
    try:
        prompt = (
            "כתוב תיאור שיווקי קצר, קולח ומושך בעברית למוצר הבא, "
            "עם אימוג'ים וקריאה לפעולה:\n"
            f"שם: {data.get('title')}\n"
            f"מחיר: {data.get('price')} ₪\n"
            f"דירוג: {data.get('rating')}\n"
            f"כמות הזמנות: {data.get('orders')}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # מודל עדכני וזול
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=250,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ OpenAI error: {e}")
        return None


# --- טיפול בקישורים ---
@bot.message_handler(func=lambda m: isinstance(m.text, str) and "http" in m.text.lower())
def handle_link(message: Message):
    try:
        link = message.text.strip()
        logger.info(f"📩 קיבלתי קישור: {link}")

        data = pull_product(link)
        if not data:
            bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
            return

        description = generate_description(data)
        if not description:
            description = data.get("title") or "מוצר מומלץ!"

        caption = (
            f"{description}\n\n"
            f"💰 מחיר: {data.get('price')} ₪\n"
            f"⭐ דירוג: {data.get('rating')}\n"
            f"🛒 הזמנות: {data.get('orders')}\n"
            f"🔗 <a href=\"{data.get('link')}\">לרכישה כאן</a>"
        )

        if data.get("image"):
            bot.send_photo(message.chat.id, data["image"], caption=caption)
        else:
            bot.reply_to(message, caption, disable_web_page_preview=False)

    except Exception as e:
        logger.error(f"❌ שגיאה ב-handle_link: {e}")
        bot.reply_to(message, "❌ אירעה שגיאה בעיבוד הקישור.")


# --- /start ---
@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    bot.reply_to(
        message,
        "היי! שלח לי קישור למוצר מאלי אקספרס, ואני אחזיר לך תיאור שיווקי מוכן 🔥",
    )


# --- הרצה ---
def keep_alive():
    Thread(
        target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    ).start()


def main():
    keep_alive()
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"✅ Bot is live! Webhook set to: {WEBHOOK_URL}")


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    main()
