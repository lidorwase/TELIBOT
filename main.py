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

# ----- Load Environment Variables -----
load_dotenv(dotenv_path=".env", override=True)

# ----- Setup Logging -----
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ----- Validate Environment Variables -----
def validate_env_variables() -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
    required = ["BOT_TOKEN", "CHANNEL_USERNAME", "ALI_APP_KEY", "ALI_APP_SECRET"]
    optional = ["OPENAI_API_KEY"]

    loaded: Dict[str, str] = {}
    optional_values: Dict[str, Optional[str]] = {}
    missing = []

    logger.info("🔍 Checking environment variables…")
    for var in required:
        value = os.getenv(var)
        if value:
            logger.info("✅ %s loaded (%d chars)", var, len(value))
            loaded[var] = value
        else:
            logger.error("❌ %s is missing!", var)
            missing.append(var)

    for var in optional:
        value = os.getenv(var)
        if value:
            logger.info("ℹ️ %s loaded (%d chars)", var, len(value))
        else:
            logger.warning("⚠️ %s not set – falling back to static copy")
        optional_values[var] = value

    if missing:
        raise EnvironmentError("Missing required environment variables: " + ", ".join(missing))

    logger.info("------------------------------------------------")
    return loaded, optional_values

REQUIRED_ENV, OPTIONAL_ENV = validate_env_variables()
BOT_TOKEN = REQUIRED_ENV["BOT_TOKEN"]
CHANNEL_USERNAME = REQUIRED_ENV["CHANNEL_USERNAME"]
ALI_APP_KEY = REQUIRED_ENV["ALI_APP_KEY"]
ALI_APP_SECRET = REQUIRED_ENV["ALI_APP_SECRET"]
OPENAI_API_KEY = OPTIONAL_ENV.get("OPENAI_API_KEY") or ""

# ----- OpenAI Client Setup -----
try:
    from openai import OpenAI
    OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
    OPENAI_USES_MODERN_API = True
except ImportError:
    try:
        import openai as openai_legacy
        openai_legacy.api_key = OPENAI_API_KEY
        OPENAI_CLIENT = openai_legacy
        OPENAI_USES_MODERN_API = False
    except ImportError:
        OPENAI_CLIENT = None
        OPENAI_USES_MODERN_API = False

# ----- Init Bot & Flask -----
bot = TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

# ----- Keep-Alive Server -----
def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# ----- AliExpress API Utilities -----
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8"
})

def http_get(url: str, timeout: int = 20) -> requests.Response:
    return SESSION.get(url, timeout=timeout, allow_redirects=True)

def resolve_url(url: str) -> str:
    try:
        resolved_url = http_get(url).url
        logger.info("Resolved to: %s", resolved_url)
        return resolved_url
    except requests.RequestException as exc:
        logger.warning("Failed to resolve URL: %s", exc)
        return url

def extract_pid(url: str) -> Optional[str]:
    match = re.search(r"/item/(\d{6,20})\.html", url)
    if match:
        return match.group(1)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("productId", "itemId", "item_id", "objectId"):
        if key in query and query[key]:
            pid = re.sub(r"\D", "", query[key][0])
            if pid:
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
        products = data.get("aliexpress_affiliate_productdetail_get_response", {}).get("result", {}).get("products", [])
        if not isinstance(products, list) or not products:
            logger.error("API error: No products returned")
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
    resolved = resolve_url(url)
    product_id = extract_pid(resolved)
    if not product_id:
        logger.warning("❌ לא הצלחתי לזהות מזהה מוצר מהקישור")
        return None
    return ali_productdetail(product_id)

def generate_description_ai(title: str) -> str:
    prompt = f"כתוב תיאור שיווקי בעברית למוצר בשם: {title}. השתמש בסגנון מושך, קצר ואמין, עם דגש על יתרונות, בלי הגזמות."
    if not OPENAI_CLIENT:
        return f"{title} - מוצר איכותי ושימושי במיוחד 👌"
    try:
        if OPENAI_USES_MODERN_API:
            response = OPENAI_CLIENT.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים למוצרים"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.8
            )
            return response.choices[0].message.content.strip()
        else:
            response = OPENAI_CLIENT.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים למוצרים"},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.8
            )
            return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("OpenAI fallback: %s", e)
        return f"{title} - מוצר איכותי ושימושי במיוחד 👌"

def format_post(data: Dict[str, Optional[str]], url: str) -> str:
    desc = generate_description_ai(data.get("title", "מוצר"))
    return f"""<b>{html.escape(data.get("title", "מוצר"))}</b>

{html.escape(desc)}

⭐ דירוג: {data.get("rating", "לא זמין")}
💰 מחיר: {data.get("price", "לא זמין")} ₪
📦 הזמנות: {data.get("orders", "לא ידוע")}

🔗 <a href="{html.escape(url)}">לרכישה באלי אקספרס</a>
"""

# ----- Webhook route -----
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = bot.de_json(json_string)
        if update.message:
            bot.process_new_updates([update])
        return "", 200
    else:
        return "Unsupported Media Type", 415

# ----- Bot Handlers -----
@bot.message_handler(commands=['start'])
def handle_start(message: Message):
    bot.reply_to(message, "היי! אני חי ובועט 🦾 שלח לי קישור שותף של אלי אקספרס 📦")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and ("aliexpress" in m.text.lower() or "s.click.aliexpress.com" in m.text.lower()))
def handle_aff_link(message: Message):
    link = message.text.strip()
    logger.info("📩 קיבלתי קישור: %s", link)
    data = pull_product(link)
    if not data:
        bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
        return
    caption = format_post(data, link)
    if data.get("image"):
        bot.send_photo(CHANNEL_USERNAME, data["image"], caption=caption)
    else:
        bot.send_message(CHANNEL_USERNAME, caption)
    bot.reply_to(message, "✅ פורסם לערוץ בהצלחה!")

# ----- Main -----
def main():
    keep_alive()
    logger.info("✅ Bot is running!")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
