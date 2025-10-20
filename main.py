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
import openai
from flask import Flask
from threading import Thread

# ===== SETTINGS =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")

# AliExpress Open Platform
ALI_APP_KEY = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")

# OpenAI Key
openai.api_key = os.getenv("OPENAI_API_KEY")
# ===== DEBUG TEST - check environment variables =====
print("🔍 DEBUG: Checking environment variables loaded from Railway...")

vars_to_check = {
    "BOT_TOKEN": os.getenv("BOT_TOKEN"),
    "CHANNEL_USERNAME": os.getenv("CHANNEL_USERNAME"),
    "ALI_APP_KEY": os.getenv("ALI_APP_KEY"),
    "ALI_APP_SECRET": os.getenv("ALI_APP_SECRET"),
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY")
}

for name, value in vars_to_check.items():
    if value:
        print(f"✅ {name} detected successfully ({len(value)} chars)")
    else:
        print(f"❌ {name} NOT FOUND – check Railway Variables tab!")

print("-----------------------------------------------------\n")


ALI_GATEWAY = "https://api-sg.aliexpress.com/sync"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
LANG = {"Accept-Language": "en-US,en;q=0.9,he;q=0.8"}

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ========= keep-alive =========
app = Flask(__name__)
@app.get("/")
def home(): return "ok"
def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# ========= HELPERS =========
def http_get(url, timeout=20):
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, **LANG})
    return s.get(url, timeout=timeout, allow_redirects=True)

def resolve_url(url: str) -> str:
    try:
        final = http_get(url).url
    except Exception:
        final = url
    try:
        pu = urllib.parse.urlparse(final)
        if pu.netloc.lower().startswith("aliexpress.us"):
            final = urllib.parse.urlunparse(pu._replace(netloc="www.aliexpress.com"))
    except Exception:
        pass
    return final

def parse_amount(text):
    if not text: return None
    m = re.search(r"(\d+(?:\.\d{1,4})?)", str(text).replace(",", ""))
    return float(m.group(1)) if m else None

def detect_currency(txt):
    if not txt: return None
    for k,v in {"₪":"ILS","US$":"USD","$":"USD","USD":"USD",
                "€":"EUR","EUR":"EUR","£":"GBP","GBP":"GBP",
                "руб":"RUB","AED":"AED","SAR":"SAR","₹":"INR"}.items():
        if k in str(txt): return v
    return None

def fetch_rate(base, target="ILS"):
    if not base or base == target: return 1.0
    try:
        j = requests.get(f"https://api.exchangerate.host/latest?base={base}&symbols={target}", timeout=10).json()
        return float(j["rates"][target])
    except Exception:
        return None

def deep_find(d, keys):
    if isinstance(d, dict):
        for k,v in d.items():
            if any(x in k.lower() for x in keys):
                if isinstance(v,(str,int,float)): return v
                if isinstance(v,dict):
                    for sub in ("value","amount","min","max","display","text","price","lowPrice"):
                        if sub in v and isinstance(v[sub],(str,int,float)): return v[sub]
                if isinstance(v,list) and v:
                    f=v[0]
                    if isinstance(f,dict):
                        for sub in ("value","amount","display","text","price"):
                            if sub in f and isinstance(f[sub],(str,int,float)): return f[sub]
                    else: return f
            r = deep_find(v, keys)
            if r is not None: return r
    elif isinstance(d, list):
        for it in d:
            r = deep_find(it, keys)
            if r is not None: return r
    return None

def extract_from_json_blob(txt):
    for pat in (r'window\.runParams\s*=\s*(\{.*?\});', r'__INIT_DATA__\s*=\s*(\{.*?\})\s*;'):
        m = re.search(pat, txt, re.DOTALL)
        if m:
            raw = m.group(1)
            try: return json.loads(raw)
            except Exception:
                try:
                    end = raw.rfind("}")
                    return json.loads(raw[:end+1])
                except Exception: pass
    return None

def extract_from_jsonld(soup):
    for tag in soup.find_all("script", {"type":"application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.text or "{}")
            if isinstance(data, list):
                for obj in data:
                    if isinstance(obj, dict) and obj.get("@type")=="Product": return obj
            if isinstance(data, dict) and data.get("@type")=="Product":
                return data
        except Exception:
            continue
    return None

def extract_pid(u: str):
    m = re.search(r'/item/(\d{6,20})\.html', u)
    if m: return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    for key in ("productId","itemId","item_id","objectId"):
        if key in qs and qs[key]:
            pid = re.sub(r'\D','', qs[key][0])
            if pid: return pid
    return None

# ========= API =========
def ali_sign(params: dict, app_secret: str) -> str:
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    return hmac.new(app_secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()

def ali_productdetail(product_id: str):
    if not (ALI_APP_KEY and ALI_APP_SECRET and product_id):
        return None
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
        r = requests.post(ALI_GATEWAY, data=params, timeout=15)
        data = r.json()
        res = data.get("aliexpress_affiliate_productdetail_get_response", {}).get("result", {})
        prods = res.get("products")
        if not prods: return None
        p = prods[0]
        title  = p.get("product_title")
        image  = p.get("product_main_image_url")
        rating = p.get("evaluate_rate")
        orders = p.get("sale_count")
        price  = p.get("target_sale_price")
        price_line = f"💰 מחיר: {price} ₪" if price else "💰 מחיר: לא זמין"
        rating_line = f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
        orders_line = f"📦 {orders} הזמנות" if orders else None
        return {"title": title or "מוצר מאלי אקספרס", "image": image,
                "price_line": price_line, "rating_line": rating_line, "orders_line": orders_line}
    except Exception:
        return None

# ========= SCRAPE BACKUP =========
def pull_product_fallback(affiliate_url: str):
    r = http_get(resolve_url(affiliate_url))
    soup = BeautifulSoup(r.text, "html.parser")
    ld = extract_from_jsonld(soup)
    blob = extract_from_json_blob(r.text)
    title = (ld.get("name") if ld else None) or deep_find(blob, ["title","subject"]) or "מוצר מאלי אקספרס"
    image = (ld.get("image") if ld else None) or (soup.find("meta", {"property":"og:image"}) or {}).get("content")
    rating = deep_find(blob, ["rating","starrating"])
    price = deep_find(blob, ["price","saleprice"])
    orders = deep_find(blob, ["orders","soldcount"])
    price_line = f"💰 מחיר: {price} ₪" if price else "💰 מחיר: לא זמין"
    rating_line = f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
    orders_line = f"📦 {orders} הזמנות" if orders else None
    return {"title": title, "image": image, "price_line": price_line, "rating_line": rating_line, "orders_line": orders_line}

def pull_product(url): 
    pid = extract_pid(resolve_url(url))
    return ali_productdetail(pid) or pull_product_fallback(url)

# ========= MARKETING =========
def marketing_copy(info, url):
    title = info["title"]; rating = info["rating_line"]; price = info["price_line"]; orders = info.get("orders_line","")
    text = f"""<b>{title}</b>

איכות מצוינת ושימושיות גבוהה ביום-יום ✅
מתנה מושלמת לעצמך או למישהו שאוהבים 🎁

{rating}
{price}
{orders}

נראה לי דיל ששווה לבדוק לא ? 🤔

🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>"""
    return text

# ========= HANDLERS =========
@bot.message_handler(func=lambda m: "aliexpress" in m.text.lower())
def send_post(m):
    link = m.text.strip()
    try:
        data = pull_product(link)
        caption = marketing_copy(data, link)
        if data.get("image"): bot.send_photo(CHANNEL_USERNAME, data["image"], caption=caption)
        else: bot.send_message(CHANNEL_USERNAME, caption)
        bot.reply_to(m, "✅ פורסם לערוץ!")
    except Exception as e:
        bot.reply_to(m, f"שגיאה: {e}")

keep_alive()
print("✅ בוט פעיל ומוכן!")
bot.infinity_polling()
# Redeploy check





