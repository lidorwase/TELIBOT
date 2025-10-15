import os, re, json, html, urllib.parse, requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

# === הגדרות בסיס ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@dealsumustcheck"  # תעדכן אם צריך
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# === שרת קטן לשמירה על חיים (Railway) ===
app = Flask(__name__)
@app.get("/")
def home(): return "Bot is alive"
def keep_alive(): Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# === פונקציות עזר ===
def fetch_rate(base, target):
    try:
        data = requests.get(f"https://api.exchangerate.host/latest?base={base}&symbols={target}", timeout=10).json()
        return float(data["rates"][target])
    except: return None

def parse_amount(text):
    if not text: return None
    m = re.search(r"(\d+(?:\.\d{1,2})?)", str(text).replace(",", ""))
    return float(m.group(1)) if m else None

def detect_currency(txt):
    if not txt: return None
    for k,v in {"₪":"ILS","$":"USD","US$":"USD","USD":"USD","€":"EUR","EUR":"EUR","£":"GBP","GBP":"GBP"}.items():
        if k in str(txt): return v
    return None

def extract_json(soup):
    for tag in soup.find_all("script", {"type":"application/ld+json"}):
        try:
            data=json.loads(tag.string or tag.text or "{}")
            if isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict) and obj.get("@type")=="Product": return obj
            if isinstance(data, dict) and data.get("@type")=="Product": return data
        except: continue
    return None

def resolve_url(url:str)->str:
    s=requests.Session()
    s.headers.update({"User-Agent":USER_AGENT})
    try:
        resp=s.get(url,allow_redirects=True,timeout=15)
        return resp.url
    except: return url

def extract_info(url):
    final_url = resolve_url(url)
    r = requests.get(final_url, headers={"User-Agent":USER_AGENT}, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    data = extract_json(soup)
    title = data.get("name") if data else (soup.title.string if soup.title else "מוצר מאלי אקספרס")
    image = None
    if data and "image" in data:
        image = data["image"][0] if isinstance(data["image"], list) else data["image"]
    elif soup.find("meta", {"property":"og:image"}):
        image = soup.find("meta", {"property":"og:image"}).get("content")

    price = (data.get("offers",{}) or {}).get("price") if data else None
    currency = (data.get("offers",{}) or {}).get("priceCurrency") if data else "USD"
    rating = (data.get("aggregateRating",{}) or {}).get("ratingValue") if data else None

    if not price:
        m=re.search(r'\"price\"\s*:\s*\"(\d+(?:\.\d{1,2})?)\"', r.text)
        price=m.group(1) if m else None

    amount=parse_amount(price)
    if amount:
        if currency!="ILS":
            rate=fetch_rate(currency,"ILS")
            price_text=f"💰 מחיר: {round(amount*(rate or 1),2)} ₪ (≈ {amount} {currency})"
        else:
            price_text=f"💰 מחיר: {amount} ₪"
    else:
        price_text="💰 מחיר: לא זמין"

    rating_text=f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"

    return title.strip(), price_text, rating_text, image, url

# === נוסח שיווקי ===
def marketing_caption(title, price_text, rating_text, url):
    # ניסוח שיווקי שמניע לפעולה
    promo = f"🔥 {html.escape(title)} 🔥\n\n"
    promo += f"{rating_text}\n{price_text}\n\n"
    promo += "מוצר איכותי עם משלוחים מהירים במיוחד 🚀\n"
    promo += "נראה לי דיל ששווה לבדוק לא ? 🤔\n\n"
    promo += f'🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>'
    return promo

# === טיפול בהודעות ===
@bot.message_handler(func=lambda m: "aliexpress" in m.text.lower() or "s.click" in m.text.lower())
def handle_link(m):
    url = m.text.strip()
    try:
        title, price_text, rating_text, image, clean_url = extract_info(url)
        caption = marketing_caption(title, price_text, rating_text, clean_url)
        if image:
            bot.send_photo(CHANNEL_USERNAME, image, caption=caption)
        else:
            bot.send_message(CHANNEL_USERNAME, caption, disable_web_page_preview=False)
        bot.reply_to(m, "✅ פורסם לערוץ בהצלחה!")
    except Exception as e:
        bot.reply_to(m, f"❌ שגיאה: {e}")

# === התחלה ===
keep_alive()
print("🤖 AliExpress auto-poster online [v1-marketing]")
bot.infinity_polling(timeout=60, long_polling_timeout=30)
