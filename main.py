import os, re, json, html
import requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

# -------- הגדרות --------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # שים ב-Railway > Variables
CHANNEL_USERNAME = "@dealsumustcheck"  # עדכן אם צריך
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# שרת קטן לשמירה בחיים (Railway/Render)
app = Flask(__name__)
@app.get("/")
def home():
    return "AliExpress bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# -------- עזרות --------
def _first(v):
    return v if isinstance(v, (str, int, float)) else None

def _deep_find(d, keys):
    """חפש בצורה רקורסיבית מפתחות אפשריים ברשימות/דקטים"""
    if isinstance(d, dict):
        for k, v in d.items():
            lk = k.lower()
            for target in keys:
                if target in lk:
                    if isinstance(v, (str, int, float)):
                        return v
                    if isinstance(v, dict):
                        # נסה תתי-שדות סטנדרטיים
                        for sub in ("value","amount","min","max","display","text"):
                            if sub in v and isinstance(v[sub], (str,int,float)):
                                return v[sub]
                    if isinstance(v, list) and v and isinstance(v[0], (str,int,float,dict)):
                        if isinstance(v[0], dict):
                            for sub in ("value","amount","display","text"):
                                if sub in v[0] and isinstance(v[0][sub], (str,int,float)):
                                    return v[0][sub]
                        else:
                            return v[0]
            # חפש פנימה
            val = _deep_find(v, keys)
            if val is not None:
                return val
    elif isinstance(d, list):
        for it in d:
            val = _deep_find(it, keys)
            if val is not None:
                return val
    return None

def extract_from_json_blob(txt):
    """חפש window.runParams / app.runParams וכד' והחזר דיקט"""
    patterns = [
        r'window\.runParams\s*=\s*(\{.*?\});',
        r'app\.runParams\s*=\s*(\{.*?\});',
        r'window\.__AER_DATA__\s*=\s*(\{.*?\});',
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.DOTALL)
        if m:
            raw = m.group(1)
            # נקה זבל נפוץ
            cleaned = raw
            cleaned = re.sub(r'(\w+):\s*undefined', r'"\1": null', cleaned)
            cleaned = cleaned.replace("undefined", "null")
            try:
                return json.loads(cleaned)
            except Exception:
                # לפעמים יש פסיקים עודפים; נסה גישה רכה
                try:
                    end = cleaned.rfind("}")
                    return json.loads(cleaned[:end+1])
                except Exception:
                    continue
    return None

def get_product_info(url: str):
    # שלוף דף
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    html_text = r.text
    soup = BeautifulSoup(html_text, "html.parser")

    # מטא-תגיות לגיבוי
    meta = lambda p, n=None: (soup.find("meta", { "property": p }) or
                              soup.find("meta", { "name": (n or p) }))
    title = (meta("og:title") or {}).get("content") or (soup.title.string if soup.title else "")
    price = (meta("og:price:amount") or {}).get("content")
    rating = (meta("og:rating") or meta("ratingValue") or {}).get("content")
    image = (meta("og:image") or {}).get("content")

    # JSON פנימי של אלי
    blob = extract_from_json_blob(html_text)
    if blob:
        # כותרת/תיאור
        t = _deep_find(blob, ["title","subject","producttitle","pageTitle"])
        if t: title = str(t)
        # מחיר
        p = _deep_find(blob, ["saleprice","price","skuprice","actminprice","displayprice"])
        if p: price = str(p)
        # דירוג
        r_ = _deep_find(blob, ["rating","ratingvalue","averagerating","starrating"])
        if r_: rating = str(r_)
        # הזמנות
        orders = _deep_find(blob, ["tradecount","orders","soldcount","ordercount"])
    else:
        orders = None

    # אם אין תמונה – נסה מהדף
    if not image:
        img = soup.find("img", {"src": re.compile(r'\.(jpg|jpeg|png)')})
        if img and img.get("src"):
            image = img["src"]

    # עיצוב ערכים
    title = (title or "מוצר מאלי אקספרס").strip()
    if price:
        # נקי מ-$ וכתיב
        price = str(price).replace("USD", "").replace("$","").strip()
    if rating:
        rating = str(rating).strip()
    text_lines = []

    # תיאור אינדיבידואלי
    text_lines.append(f"💥 <b>{html.escape(title)}</b>")
    # שורות מידע
    text_lines.append(f"⭐ דירוג: {html.escape(rating)}" if rating else "⭐ דירוג: לא זמין")
    text_lines.append(f"💰 מחיר: {html.escape(price)} ₪" if price else "💰 מחיר: לא זמין")
    if orders:
        text_lines.append(f"📦 {html.escape(str(orders))} רכישות")

    # הסיומת הממותגת
    text_lines.append("נראה לי דיל ששווה לבדוק לא ? 🤔")
    text_lines.append(f'🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>')

    caption = "\n".join(text_lines)
    return caption, image

# -------- Handlers --------
@bot.message_handler(commands=["start"])
def start_msg(m):
    bot.reply_to(m, "שלח לי קישור של מוצר מ-AliExpress ואני אפרסם אותו לערוץ 😊")

@bot.message_handler(func=lambda m: m.text and ("aliexpress" in m.text.lower() or "ali" in m.text.lower()))
def on_link(m):
    url = m.text.strip()
    try:
        caption, image = get_product_info(url)
        if image:
            bot.send_photo(CHANNEL_USERNAME, image, caption=caption)
        else:
            bot.send_message(CHANNEL_USERNAME, caption)
        bot.reply_to(m, "✅ פורסם לערוץ!")
    except Exception as e:
        bot.reply_to(m, f"❌ שגיאה בפרסום: {e}")

# -------- Run --------
keep_alive()
print("🤖 Bot is running on Railway.")
bot.infinity_polling(timeout=60, long_polling_timeout=30)

