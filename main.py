import os, re, json, html, requests, urllib.parse
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@dealsumustcheck"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# === שרת לשמירה בחיים ===
app = Flask(__name__)
@app.get("/")
def home():
    return "Bot online"
def keep_alive(): Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

# === כלים ===
def fetch_rate(base, target):
    try:
        data = requests.get(f"https://api.exchangerate.host/latest?base={base}&symbols={target}", timeout=10).json()
        return float(data["rates"][target])
    except:
        return None

def parse_amount(text):
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else None

def detect_currency(txt):
    for sym, code in {"₪":"ILS","$":"USD","€":"EUR","£":"GBP","руб":"RUB"}.items():
        if sym in str(txt): return code
    return "USD"

def deep_find(d, keys):
    if isinstance(d, dict):
        for k, v in d.items():
            if any(k.lower()==kk.lower() for kk in keys): return v
            res = deep_find(v, keys)
            if res: return res
    if isinstance(d, list):
        for i in d:
            res = deep_find(i, keys)
            if res: return res
    return None

def extract_json(txt):
    m = re.search(r'window\.runParams\s*=\s*(\{.*?\});', txt, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: pass
    m = re.search(r'__INIT_DATA__\s*=\s*(\{.*?\})\s*;', txt, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except: pass
    return None

def resolve_url(url):
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    try:
        r = s.head(url, allow_redirects=True, timeout=12)
        final = r.url
        if "s.click" in final:
            r = s.get(url, allow_redirects=True, timeout=15)
            final = r.url
        return final
    except:
        return url

# === שליפת פרטי מוצר ===
def get_product_info(url):
    url = resolve_url(url)
    pid = re.search(r'/item/(\d+)\.html', url)
    pid = pid.group(1) if pid else None
    if pid:
        m_url = f"https://m.aliexpress.com/item/{pid}.html"
        r = requests.get(m_url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        script = soup.find("script", string=re.compile(r'__INIT_DATA__|runParams'))
        data = extract_json(script.text) if script else None
    else:
        r = requests.get(url, headers={"User-Agent": USER_AGENT})
        soup = BeautifulSoup(r.text, "html.parser")
        data = extract_json(r.text)

    title = deep_find(data, ["productTitle","title","subject"]) or "מוצר מאלי אקספרס"
    price = deep_find(data, ["salePrice","price","discountPrice","displayPrice"])
    rating = deep_find(data, ["averageStar","avgStar","rating","ratingValue"])
    orders = deep_find(data, ["tradeCount","soldCount","orders"])
    img = deep_find(data, ["image","mainImage","imageUrl"])

    amount = parse_amount(price)
    cur = detect_currency(price)
    price_line = "💰 מחיר: לא זמין"
    if amount:
        if cur != "ILS":
            rate = fetch_rate(cur,"ILS") or 0
            if rate:
                ils = round(amount*rate,2)
                price_line = f"💰 מחיר: {ils} ₪ (≈ {amount} {cur})"
            else:
                price_line = f"💰 מחיר: {amount} {cur}"
        else:
            price_line = f"💰 מחיר: {amount} ₪"

    rating_line = f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
    orders_line = f"📦 {orders} הזמנות" if orders else ""

    text = f"""💥 <b>{html.escape(title)}</b>
{rating_line}
{price_line}
{orders_line}
נראה לי דיל ששווה לבדוק לא ? 🤔
🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>"""
    return text, img

# === הפעלה ===
@bot.message_handler(commands=["start"])
def start(m): bot.reply_to(m, "שלח לי קישור מ-AliExpress 😊")

@bot.message_handler(func=lambda m: "aliexpress" in m.text)
def post(m):
    try:
        cap, img = get_product_info(m.text.strip())
        if img:
            bot.send_photo(CHANNEL_USERNAME, img, caption=cap)
        else:
            bot.send_message(CHANNEL_USERNAME, cap)
        bot.reply_to(m, "✅ פורסם לערוץ!")
    except Exception as e:
        bot.reply_to(m, f"שגיאה: {e}")

keep_alive()
print("🤖 Bot online")
bot.infinity_polling()

