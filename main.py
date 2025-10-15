import os, re, json, html, math
import requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

# ===== הגדרות =====
BOT_TOKEN = os.getenv("BOT_TOKEN")          # להגדיר ב-Railway > Variables
CHANNEL_USERNAME = "@dealsumustcheck"       # עדכן אם צריך
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ===== שרת קטן לשמירה בחיים =====
app = Flask(__name__)

@app.get("/")
def home():
    return "AliExpress bot is alive!"

def run_web():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# ===== עזרות =====
CURRENCY_SYMBOLS = {
    "$": "USD", "US$": "USD", "USD": "USD",
    "₪": "ILS", "ILS": "ILS", "ש״ח": "ILS", "שח": "ILS",
    "€": "EUR", "EUR": "EUR",
    "£": "GBP", "GBP": "GBP",
    "руб": "RUB", "RUB": "RUB",
    "AED": "AED", "SAR": "SAR", "₹": "INR", "INR": "INR"
}

def detect_currency(text: str):
    if not text:
        return None
    t = text.upper()
    # חפש קוד מטבע מפורש
    for code in CURRENCY_SYMBOLS.values():
        if code in t:
            return code
    # חפש סימן
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return None

def parse_amount(text: str):
    """החזר מספר float מתוך מחרוזת מחיר (כולל טווחים: '10.99-12.50' -> 10.99)"""
    if not text:
        return None
    # החלף פסיקים אלפים
    t = text.replace(",", " ")
    # מצא את המספר הראשון (כולל נקודה/ספרות)
    m = re.search(r"(\d+(?:\.\d{1,4})?)", t)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None

def fetch_rate(base: str, target: str) -> float | None:
    """שימוש ב-exchangerate.host (חינמי, בלי מפתח)"""
    if base == target:
        return 1.0
    try:
        url = f"https://api.exchangerate.host/latest?base={base}&symbols={target}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return float(data["rates"][target])
    except Exception:
        return None

def fmt_num(n):
    try:
        n = float(n)
        # ללא נקודות מיותרות
        return f"{n:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(n)

def fmt_int_with_sep(n):
    try:
        return f"{int(n):,}".replace(",", ",")
    except Exception:
        return str(n)

def _deep_find(d, keys):
    """חיפוש רקורסיבי במבני JSON של עלי אקספרס"""
    if isinstance(d, dict):
        for k, v in d.items():
            lk = k.lower()
            if any(t in lk for t in keys):
                if isinstance(v, (str, int, float)):
                    return v
                if isinstance(v, dict):
                    for sub in ("value", "amount", "min", "max", "display", "text"):
                        if sub in v and isinstance(v[sub], (str, int, float)):
                            return v[sub]
                if isinstance(v, list) and v:
                    first = v[0]
                    if isinstance(first, dict):
                        for sub in ("value", "amount", "display", "text"):
                            if sub in first and isinstance(first[sub], (str, int, float)):
                                return first[sub]
                    else:
                        return first
            res = _deep_find(v, keys)
            if res is not None:
                return res
    elif isinstance(d, list):
        for it in d:
            res = _deep_find(it, keys)
            if res is not None:
                return res
    return None

def extract_from_json_blob(txt):
    patterns = [
        r'window\.runParams\s*=\s*(\{.*?\});',
        r'app\.runParams\s*=\s*(\{.*?\});',
        r'window\.__AER_DATA__\s*=\s*(\{.*?\});',
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.DOTALL)
        if m:
            raw = m.group(1)
            cleaned = re.sub(r'(\w+):\s*undefined', r'"\1": null', raw).replace("undefined", "null")
            try:
                return json.loads(cleaned)
            except Exception:
                try:
                    end = cleaned.rfind("}")
                    return json.loads(cleaned[:end+1])
                except Exception:
                    continue
    return None

# ===== שליפת מידע מוצר (עם פתיחת קישורי s.click) =====
def get_product_info(url: str):
    # אם זה קישור affiliate קצר – פתח ל-URL הסופי
    if "s.click.aliexpress.com" in url:
        try:
            r0 = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10, allow_redirects=True)
            url = r0.url
        except Exception:
            pass

    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    r.raise_for_status()
    html_text = r.text
    soup = BeautifulSoup(html_text, "html.parser")

    # גיבויים ממטא
    get_meta = lambda p, n=None: (soup.find("meta", {"property": p}) or soup.find("meta", {"name": (n or p)}))
    title = (get_meta("og:title") or {}).get("content") or (soup.title.string if soup.title else "")
    price_raw = (get_meta("og:price:amount") or {}).get("content")
    rating_raw = (get_meta("og:rating") or get_meta("ratingValue") or {}).get("content")
    image = (get_meta("og:image") or {}).get("content")

    # JSON פנימי
    blob = extract_from_json_blob(html_text)
    orders = None
    currency_code = detect_currency(price_raw) if price_raw else None

    if blob:
        # כותרת/תיאור קצר
        t = _deep_find(blob, ["title", "subject", "producttitle", "pagetitle"])
        if t: title = str(t)

        # מחיר + מטבע
        p = _deep_find(blob, ["saleprice", "price", "skuprice", "actminprice", "displayprice"])
        if p: price_raw = str(p)
        # לעיתים המטבע בשדה נפרד
        c = _deep_find(blob, ["currency", "currencysymbol"])
        if c: currency_code = str(c).upper()

        # דירוג
        r_ = _deep_find(blob, ["rating", "ratingvalue", "averagerating", "starrating"])
        if r_: rating_raw = str(r_)

        # הזמנות
        orders = _deep_find(blob, ["tradecount", "orders", "soldcount", "ordercount"])

        # תמונה
        if not image:
            img = _deep_find(blob, ["imagedefault", "mainimage", "imageurl"])
            if img: image = str(img)

    # נסיון אחרון לתמונה
    if not image:
        img_tag = soup.find("img", {"src": re.compile(r'\.(jpg|jpeg|png)')})
        if img_tag and img_tag.get("src"):
            image = img_tag["src"]

    # ---- עיבוד ערכים ----
    title = (title or "מוצר מאלי אקספרס").strip()

    # מחיר + המרה לש״ח
    price_num = parse_amount(price_raw) if price_raw else None
    if not currency_code:
        currency_code = detect_currency(price_raw or "")

    price_line = "💰 מחיר: לא זמין"
    if price_num:
        base = currency_code or "USD"
        # המרה ל-ILS אם צריך
        if base != "ILS":
            rate = fetch_rate(base, "ILS")
            if rate:
                ils = price_num * rate
                price_line = f"💰 מחיר: {fmt_num(ils)} ₪ (≈ {fmt_num(price_num)} {base})"
            else:
                price_line = f"💰 מחיר: {fmt_num(price_num)} {base}"
        else:
            price_line = f"💰 מחיר: {fmt_num(price_num)} ₪"

    # דירוג
    rating_line = "⭐ דירוג: לא זמין"
    if rating_raw:
        rating_line = f"⭐ דירוג: {fmt_num(rating_raw)}"

    orders_line = None
    if orders:
        orders_line = f"📦 {fmt_int_with_sep(orders)} הזמנות"

    # בניית טקסט
    lines = [
        f"💥 <b>{html.escape(title)}</b>",
        rating_line,
        price_line
    ]
    if orders_line:
        lines.append(orders_line)
    lines.append("נראה לי דיל ששווה לבדוק לא ? 🤔")
    lines.append(f'🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>')
    caption = "\n".join(lines)

    return caption, image

# ===== Handlers =====
@bot.message_handler(commands=["start"])
def start_msg(m):
    bot.reply_to(m, "שלח לי קישור של מוצר מ-AliExpress (גם קישור מקוצר עובד) ואני אפרסם אותו לערוץ 😊")

@bot.message_handler(func=lambda m: m.text and ("aliexpress" in m.text.lower() or "s.click.aliexpress" in m.text.lower()))
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

# ===== Run =====
keep_alive()
print("🤖 Bot is running on Railway (with ILS conversion).")
bot.infinity_polling(timeout=60, long_polling_timeout=30)

