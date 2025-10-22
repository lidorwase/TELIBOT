import os
import re
import hmac
import time
import html
import hashlib
import logging
import requests
import urllib.parse

from dotenv import load_dotenv
from telebot import TeleBot, types
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

# --- חיבור OpenAI ---
openai = OpenAI(api_key=OPENAI_API_KEY)

def generate_description(title: str) -> str:
    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "אתה עוזר שיווקי שכותב תיאורים שיווקיים קצרים בעברית"},
                {"role": "user", "content": f"תכתוב תיאור שיווקי קצר למוצר בשם: {title}"}
            ]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return ""

# --- עיבוד קישור ---
def resolve_url(url: str) -> str:
    try:
        return requests.get(url, timeout=10).url
    except:
        return url

def extract_pid(url: str) -> Optional[str]:
    resolved = resolve_url(url)
    logger.info(f"Resolved to: {resolved}")
    
    # תנסה למצוא ID לפי דפוס של AliExpress
    match = re.search(r"/item/(\d+)\.html", resolved)
    if match:
        return match.group(1)
    
    # חפש במספר משתנים ב-URL
    query = urllib.parse.parse_qs(urllib.parse.urlparse(resolved).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r"\D", "", query[key][0])
            if pid:
                return pid

    # ניסיון נוסף לחלץ מתוך מסלול הקישור
    match = re.search(r"/(\d{10,20})", resolved)
    if match:
        return match.group(1)

    logger.warning(f"⚠️ לא נמצא product_id מתוך: {resolved}")
    return None


def ali_sign(params: Dict[str, Optional[str]], secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()

def pull_product(link: str) -> Optional[Dict[str, str]]:
    url = resolve_url(link)
    product_id = extract_pid(url)
    if not product_id:
        return None

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
        product = res.json()["aliexpress_affiliate_productdetail_get_response"]["result"]["products"][0]
        return {
            "title": product["product_title"],
            "image": product["product_main_image_url"],
            "price": product["target_sale_price"],
            "rating": product["evaluate_rate"],
            "orders": product["sale_count"],
        }
    except Exception as e:
        logger.error(f"API error: {e}")
        return None

from telebot.types import Message

@bot.message_handler(func=lambda m: isinstance(m.text, str) and "http" in m.text.lower())
def handle_link(message: Message):
    try:
        link = message.text.strip()
        logger.info(f"📩 קיבלתי קישור: {link}")

        data = pull_product(link)
        if not data:
            bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
            return

        # תיאור עם OpenAI
        prompt = f"תאר את המוצר הבא בעברית בצורה שיווקית:\n{data['title']}"
        ai_description = get_ai_description(prompt)

        text = f"{ai_description}\n\nמחיר: {data['price']}\nדירוג: {data['rating']}\nהזמנות: {data['orders']}"

        if data.get("image"):
            bot.send_photo(CHANNEL_USERNAME, data["image"], caption=text)
        else:
            bot.send_message(CHANNEL_USERNAME, text)

        bot.reply_to(message, "✅ פורסם לערוץ בהצלחה!")
    
    except Exception as e:
        logger.error(f"❗ שגיאה בטיפול בקישור: {e}")
        bot.reply_to(message, "⚠️ התרחשה שגיאה בעת עיבוד הקישור.")


# --- /start ---
@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    bot.reply_to(message, "היי! שלח לי קישור למוצר מאלי אקספרס, ואני אפרסם אותו בערוץ עם תיאור שיווקי 🔥")

# --- הרצה ---
def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))).start()

def main():
    keep_alive()
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"✅ Bot is live! Webhook set to: {WEBHOOK_URL}")

if __name__ == "__main__":
    main()
