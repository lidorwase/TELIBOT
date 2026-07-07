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

# --- בדיקת משתני סביבה קריטיים בעלייה ---
REQUIRED_ENV = ["BOT_TOKEN", "ALI_APP_KEY", "ALI_APP_SECRET", "OPENAI_API_KEY", "RAILWAY_STATIC_URL"]
for var in REQUIRED_ENV:
    if not os.getenv(var):
        raise EnvironmentError(f"Missing required environment variable: {var}")

BOT_TOKEN        = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
ALI_APP_KEY      = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET   = os.getenv("ALI_APP_SECRET")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")

bot    = TeleBot(BOT_TOKEN, parse_mode="HTML")
app    = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL  = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

CAPTION_LIMIT = 1024  # מגבלת Telegram לכיתוב תמונה

waiting_for_affiliate = {}

# --- Webhook ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    return "Unsupported Media Type", 415


# --- עיבוד קישור ---
def resolve_url(url: str) -> str:
    """פותר קישור מקוצר לקישור המלא."""
    try:
        response = requests.get(
            url,
            timeout=10,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return response.url
    except Exception as e:
        logger.warning(f"שגיאה ב-resolve_url: {e}")
        return url


def extract_pid(resolved_url: str) -> Optional[str]:
    """
    מקבל URL שכבר עבר resolve, ומחלץ ממנו את ה-product_id.
    שים לב: לא קורא resolve_url שוב!
    """
    logger.info(f"Extracting PID from: {resolved_url}")

    # דפוס רגיל של AliExpress
    match = re.search(r"/item/(\d+)\.html", resolved_url)
    if match:
        return match.group(1)

    # חיפוש בפרמטרי ה-URL
    query = urllib.parse.parse_qs(urllib.parse.urlparse(resolved_url).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r"\D", "", query[key][0])
            if pid:
                return pid

    # חיפוש מספר ארוך במסלול
    match = re.search(r"/(\d{10,20})", resolved_url)
    if match:
        return match.group(1)

    logger.warning(f"לא נמצא product_id מתוך: {resolved_url}")
    return None


def ali_sign(params: Dict[str, str], app_secret: str) -> str:
    sorted_items = sorted(
        (k, v) for k, v in params.items() if k != "sign" and v is not None
    )
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(
        app_secret.encode(), joined.encode(), hashlib.sha256
    ).hexdigest().upper()


def ali_productdetail(product_id: str) -> Optional[Dict[str, Optional[str]]]:
    ts = str(int(time.time() * 1000))

    params: Dict[str, str] = {
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
        time.sleep(1)

        res = requests.post(
            "https://api-sg.aliexpress.com/sync",
            data=params,
            timeout=15
        )

        logger.info(f"HTTP Status: {res.status_code}")
        logger.info(f"Response: {res.text}")

        res.raise_for_status()

        data = res.json()

        if "error_response" in data:
            error = data["error_response"]

            if error.get("code") == "ApiCallLimit":
                logger.warning("AliExpress rate limit hit")
                time.sleep(2)
                return None

            logger.error(f"AliExpress API error: {error}")
            return None

        logger.info(
            "תשובת API:\n%s",
            json.dumps(data, indent=2, ensure_ascii=False)
        )

        products_container = (
            data.get("aliexpress_affiliate_productdetail_get_response", {})
                .get("resp_result", {})
                .get("result", {})
                .get("products")
        )
        if isinstance(products_container, dict) and "product" in products_container:
            products = products_container["product"]

        elif isinstance(products_container, list):
            products = products_container

        else:
            logger.error(
                "No valid product list for %s | raw: %s",
                product_id,
                data
            )
            return None

        if not isinstance(products, list) or not products:
            logger.error("Empty product list for %s", product_id)
            return None

        p = products[0]

    except Exception as exc:
        logger.error(f"API error: {exc}")
        return None

    return {
        "title": p.get("product_title"),
        "image": p.get("product_main_image_url"),
        "rating": p.get("evaluate_rate"),
        "price": p.get("target_app_sale_price") or p.get("target_sale_price"),
        "orders": p.get("lastest_volume") or p.get("sale_count"),
    
    }


def pull_product(url: str):
    resolved = resolve_url(url)
    logger.info(f"Resolved URL: {resolved}")

    product_id = extract_pid(resolved)
    logger.info(f"Extracted PID: {product_id}")

    if not product_id:
        return None

    return ali_productdetail(product_id)


# --- יצירת תיאור שיווקי ---
def generate_description(data: Dict[str, Optional[str]]) -> Optional[str]:
    try:
        prompt = (
            "כתוב תיאור שיווקי קצר (עד 3 משפטים), קולח ומושך בעברית למוצר הבא, "
            "עם אימוג'ים וקריאה לפעולה:\n"
            f"שם: {data.get('title')}\n"
            f"מחיר: {data.get('price')} ₪\n"
            f"דירוג: {data.get('rating')}\n"
            f"כמות הזמנות: {data.get('orders')}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,  # קצר כדי לא לחרוג ממגבלת caption
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return None


def build_caption(data: Dict[str, Optional[str]], description: str) -> str:
    """בונה caption ומוודא שלא עולה על 1024 תווים."""
    footer = (
        f"\n\n"
        f"💰 מחיר: {data.get('price')} ₪\n"
        f"⭐ דירוג: {data.get('rating')}\n"
        f"🛒 הזמנות: {data.get('orders')}\n"
        f"🔗 <a href=\"{data.get('link')}\">לרכישה כאן</a>"
    )
    max_desc_len = CAPTION_LIMIT - len(footer) - 10  # 10 תווים buffer
    if len(description) > max_desc_len:
        description = description[:max_desc_len].rstrip() + "..."
    return description + footer


# --- Handler קישורים ---
@bot.message_handler(
    func=lambda m: isinstance(m.text, str) and "http" in m.text.lower()
)
def handle_link(message: Message):
    try:
        link = message.text.strip()
        logger.info(f"קיבלתי קישור: {link}")

        # אינדיקציה שהבוט עובד
        bot.send_chat_action(message.chat.id, "typing")

        data = pull_product(link)
        if not data:
            bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.\nוודא שהקישור הוא ממוצר AliExpress תקין.")
            return

        description = generate_description(data) or data.get("title") or "מוצר מומלץ!"

        waiting_for_affiliate[message.chat.id] = {
            "data": data,
            "description": description
        }

        bot.reply_to(
            message,
            "✅ קיבלתי את פרטי המוצר.\n\nשלח עכשיו את קישור השותפים שיופיע במודעה."
        )
        return

        # --- קבלת קישור שותפים ---
@bot.message_handler(
    func=lambda m: m.chat.id in waiting_for_affiliate and not m.text.startswith("/")
)
def receive_affiliate_link(message):
    try:
        affiliate_link = message.text.strip()

        product = waiting_for_affiliate.pop(message.chat.id)

        data = product["data"]
        description = product["description"]

        data["link"] = affiliate_link

        caption = build_caption(data, description)

        if data.get("image"):
            bot.send_photo(
                message.chat.id,
                data["image"],
                caption=caption,
                parse_mode="HTML",
            )
        
        else:
            bot.send_message(
                message.chat.id,
                caption,
                parse_mode="HTML"
            )
        
    except Exception as e:
        logger.error(f"Affiliate handler error: {e}")
        bot.reply_to(message, "❌ שגיאה בהוספת קישור השותפים.")


# --- /start ---
@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    bot.reply_to(
        message,
        "היי! שלח לי קישור למוצר מאלי אקספרס ואני אחזיר לך תיאור שיווקי מוכן לפרסום.",
    )


# --- הרצה ---
def main():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Bot is live! Webhook: {WEBHOOK_URL}")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )


if __name__ == "__main__":
    main()
