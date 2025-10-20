import os
import re
import json
import html
import time
import hmac
import hashlib
import urllib.parse
import requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread
import openai

# ====== LOAD ENVIRONMENT VARIABLES ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
ALI_APP_KEY = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY

# ====== DEBUG: Check Variables Loaded ======
print("🔍 Checking environment variables...")
for var in ["BOT_TOKEN", "CHANNEL_USERNAME", "ALI_APP_KEY", "ALI_APP_SECRET", "OPENAI_API_KEY"]:
    value = os.getenv(var)
    if value:
        print(f"✅ {var} loaded ({len(value)} chars)")
    else:
        print(f"❌ {var} is missing!")
print("------------------------------------------------")

# ====== INITIAL SETUP ======
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# ====== HELPER FUNCTIONS ======
def http_get(url, timeout=20):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9,he;q=0.8"})
    return s.get(url, timeout=timeout, allow_redirects=True)

def resolve_url(url):
    try:
        return http_get(url).url
    except Exception:
        return url

def extract_pid(u: str):
    m = re.search(r'/item/(\d{6,20})\.html', u)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    for key in ("productId","itemId","item_id","objectId"):
        if key in qs and qs[key]:
            pid = re.sub(r'\D','', qs[key][0])
            if pid:
                return pid
    return None

def ali_sign(params: dict, app_secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(app_secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()

def ali_productdetail(product_id: str):
    ts = int(time.time() * 1000)
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
        r = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
        data = r.json()
        p = data["aliexpress_affiliate_productdetail_get_response"]["result"]["products"][0]
        return {
            "title": p.get("product_title"),
            "image": p.get("product_main_image_url"),
            "rating": p.get("evaluate_rate"),
            "price": p.get("target_sale_price"),
            "orders": p.get("sale_count")
        }
    except Exception:
        return None

def pull_product(url):
    pid = extract_pid(resolve_url(url))
    return ali_productdetail(pid)

# ====== AI MARKETING COPY ======
def generate_description_ai(title):
    prompt = f"כתוב תיאור שיווקי בעברית למוצר בשם: {title}. השתמש בסגנון מושך, קצר ואמין, עם דגש על יתרונות, בלי הגזמות."
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים למוצרים"},
                      {"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.8
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return f"{title} - מוצר איכותי ושימושי במיוחד 👌"

def format_post(data, url):
    desc = generate_description_ai(data['title'])
    price = data.get("price", "לא זמין")
    rating = data.get("rating", "לא זמין")
    orders = data.get("orders", "לא ידוע")
    text = f"""<b>{data['title']}</b>

{desc}

⭐ דירוג: {rating}
💰 מחיר: {price} ₪
📦 הזמנות: {orders}

נראה לי דיל ששווה לבדוק לא? 🤔
🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>"""
    return text

# ====== TELEGRAM HANDLER ======
@bot.message_handler(func=lambda m: "aliexpress" in m.text.lower())
def send_post(m):
    link = m.text.strip()
    try:
        data = pull_product(link)
        if not data:
            bot.reply_to(m, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
            return
        caption = format_post(data, link)
        if data.get("image"):
            bot.send_photo(CHANNEL_USERNAME, data["image"], caption=caption)
        else:
            bot.send_message(CHANNEL_USERNAME, caption)
        bot.reply_to(m, "✅ פורסם לערוץ בהצלחה!")
    except Exception as e:
        bot.reply_to(m, f"שגיאה: {e}")

# ====== START ======
keep_alive()
print("✅ Bot is running and ready!")
bot.infinity_polling()
