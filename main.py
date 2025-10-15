import os, re, json, html, urllib.parse, requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

# ===== Settings =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = "@dealsumustcheck"   # עדכן אם צריך
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
ANALYSES = {}  # user_id -> last analyzed product info

# ===== keep-alive server (Railway) =====
app = Flask(__name__)
@app.get("/")
def home(): return "ok"
def keep_alive(): Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# ===== helpers =====
def fetch_rate(base, target):
    if not base or base == target: return 1.0
    try:
        data = requests.get(f"https://api.exchangerate.host/latest?base={base}&symbols={target}", timeout=10).json()
        return float(data["rates"][target])
    except Exception:
        return None

def parse_amount(text):
    if not text: return None
    m = re.search(r"(\d+(?:\.\d{1,4})?)", str(text).replace(",", ""))
    return float(m.group(1)) if m else None

def detect_currency(txt):
    if not txt: return None
    for k,v in {"₪":"ILS","US$":"USD","$":"USD","USD":"USD","€":"EUR","EUR":"EUR","£":"GBP","GBP":"GBP","руб":"RUB","AED":"AED","SAR":"SAR","₹":"INR"}.items():
        if k in str(txt): return v
    return None

def _deep_find(d, keys):
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
            r=_deep_find(v,keys)
            if r is not None: return r
    elif isinstance(d,list):
        for it in d:
            r=_deep_find(it,keys)
            if r is not None: return r
    return None

def extract_from_json_blob(txt):
    for pat in (r'window\.runParams\s*=\s*(\{.*?\});', r'__INIT_DATA__\s*=\s*(\{.*?\})\s*;'):
        m=re.search(pat,txt,re.DOTALL)
        if m:
            raw=m.group(1)
            try: return json.loads(raw)
            except Exception:
                try:
                    end=raw.rfind("}")
                    return json.loads(raw[:end+1])
                except Exception: pass
    return None

def extract_from_jsonld(soup):
    for tag in soup.find_all("script",{"type":"application/ld+json"}):
        try:
            data=json.loads(tag.string or tag.text or "{}")
            if isinstance(data,list):
                for o in data:
                    if isinstance(o,dict) and o.get("@type")=="Product": return o
            if isinstance(data,dict) and data.get("@type")=="Product": return data
        except Exception: continue
    return None

def resolve_url(url:str)->str:
    s=requests.Session()
    s.headers.update({"User-Agent":USER_AGENT,"Accept-Language":"en-US,en;q=0.9,he;q=0.8"})
    try:
        resp=s.get(url,allow_redirects=True,timeout=20)
        final=resp.url
    except Exception:
        final=url
    try:
        pu=urllib.parse.urlparse(final)
        if pu.netloc.lower().startswith("aliexpress.us"):
            final=urllib.parse.urlunparse(pu._replace(netloc="www.aliexpress.com"))
    except Exception: pass
    return final

def pull_product_data(any_url:str):
    final_url = resolve_url(any_url)
    pid=None
    m=re.search(r'/item/(\d{6,20})\.html',final_url)
    if m: pid=m.group(1)
    else:
        qs=urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
        for key in ("productId","itemId","item_id","objectId"):
            if key in qs and qs[key]:
                pid=re.sub(r'\D','',qs[key][0]) or None
                break

    r=requests.get(final_url,headers={"User-Agent":USER_AGENT,"Accept-Language":"en-US,en;q=0.9,he;q=0.8"},timeout=20)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    ld=extract_from_jsonld(soup)
    blob=extract_from_json_blob(r.text)

    title=(ld.get("name") if ld else None) or _deep_find(blob,["producttitle","subject","title","pagetitle"]) or (soup.title.string if soup.title else None) or "מוצר מאלי אקספרס"
    image=(ld.get("image")[0] if ld and isinstance(ld.get("image"),list) else (ld.get("image") if ld else None)) or ((soup.find("meta",{"property":"og:image"}) or {}).get("content"))
    rating=(ld.get("aggregateRating",{}) or {}).get("ratingValue") if ld else _deep_find(blob,["averagerating","ratingvalue","rating","starrating"])
    price_raw=(ld.get("offers",{}) or {}).get("price") if ld else _deep_find(blob,["saleprice","price","skuprice","actminprice","displayprice"])
    orders=_deep_find(blob,["tradecount","orders","soldcount","ordercount"]) if blob else None

    if (not price_raw or not rating) and pid:
        m_url=f"https://m.aliexpress.com/item/{pid}.html"
        rm=requests.get(m_url,headers={"User-Agent":USER_AGENT,"Accept-Language":"en-US,en;q=0.9,he;q=0.8"},timeout=20)
        if rm.ok:
            msoup=BeautifulSoup(rm.text,"html.parser")
            data=extract_from_json_blob(rm.text) or extract_from_jsonld(msoup)
            if data:
                title=str(_deep_find(data,["producttitle","subject","title"]) or title)
                image=_deep_find(data,["image","imageurl","mainimage"]) or image
                price_raw=_deep_find(data,["saleprice","discountprice","price","skuprice","minprice","displayprice"]) or price_raw
                rating=_deep_find(data,["averagestar","avgstar","rating","ratingvalue"]) or rating
                orders=_deep_find(data,["tradecount","soldcount","orders"]) or orders

    cur=detect_currency(price_raw) or (ld.get("offers",{}).get("priceCurrency") if ld else None) or "USD"
    amount=parse_amount(price_raw) if price_raw else None
    if amount:
        if cur!="ILS":
            rate=fetch_rate(cur,"ILS")
            price_line=f"💰 מחיר: {round(amount*rate,2)} ₪ (≈ {amount} {cur})" if rate else f"💰 מחיר: {amount} {cur}"
        else:
            price_line=f"💰 מחיר: {amount} ₪"
    else:
        price_line="💰 מחיר: לא זמין"

    rating_line=f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
    orders_line=f"📦 {orders} הזמנות" if orders else None

    return {
        "title": str(title).strip(),
        "image": image,
        "price_line": price_line,
        "rating_line": rating_line,
        "orders_line": orders_line,
        "product_url": final_url
    }

def compose_caption(info:dict, outbound_url:str):
    lines=[f"💥 <b>{html.escape(info['title'])}</b>", info["rating_line"], info["price_line"]]
    if info.get("orders_line"): lines.append(info["orders_line"])
    lines.append("נראה לי דיל ששווה לבדוק לא ? 🤔  [v2step]")
    lines.append(f'🔗 <a href="{html.escape(outbound_url)}">לקנייה באלי אקספרס</a>')
    return "\n".join(lines)

# ===== Commands only (אין שום handler של 'aliexpress' אוטומטי!) =====
@bot.message_handler(commands=["start","help"])
def start_help(m):
    bot.reply_to(m,
        "עובדים בשני שלבים:\n"
        "1) /analyze <קישור מוצר> – הבוט שולף תיאור/מחיר/דירוג ושומר לך תצוגה מקדימה\n"
        "2) /post <קישור אפיליאייט> – מפרסם לערוץ עם הקישור שלך\n"
        "אפשר לנקות עם /reset")

@bot.message_handler(commands=["analyze"])
def cmd_analyze(m):
    parts=m.text.split(maxsplit=1)
    if len(parts)<2:
        bot.reply_to(m,"שלח: /analyze <קישור מוצר מאלי אקספרס>")
        return
    url=parts[1].strip()
    try:
        info=pull_product_data(url)
        ANALYSES[m.from_user.id]=info
        preview=compose_caption(info, outbound_url=info["product_url"])
        if info.get("image"):
            bot.send_photo(m.chat.id, info["image"], caption=preview, reply_to_message_id=m.id)
        else:
            bot.send_message(m.chat.id, preview, reply_to_message_id=m.id, disable_web_page_preview=True)
        bot.send_message(m.chat.id, "✅ נותח. עכשיו /post <קישור אפיליאייט> כדי לפרסם לערוץ.")
    except Exception as e:
        bot.reply_to(m,f"❌ שגיאה: {e}")

@bot.message_handler(commands=["post"])
def cmd_post(m):
    parts=m.text.split(maxsplit=1)
    if len(parts)<2:
        bot.reply_to(m,"שלח: /post <קישור אפיליאייט>")
        return
    aff=parts[1].strip()
    info=ANALYSES.get(m.from_user.id)
    if not info:
        bot.reply_to(m,"🔎 קודם הרץ /analyze עם קישור מוצר, ואז /post עם קישור האפיליאייט שלך.")
        return
    try:
        caption=compose_caption(info, outbound_url=aff)  # תמיד מפרסם עם הקישור שלך
        if info.get("image"):
            bot.send_photo(CHANNEL_USERNAME, info["image"], caption=caption)
        else:
            bot.send_message(CHANNEL_USERNAME, caption, disable_web_page_preview=True)
        bot.reply_to(m,"✅ פורסם לערוץ!")
    except Exception as e:
        bot.reply_to(m,f"❌ שגיאה: {e}")

@bot.message_handler(commands=["reset"])
def cmd_reset(m):
    ANALYSES.pop(m.from_user.id, None)
    bot.reply_to(m,"🧹 אופס! נוקה. אפשר להתחיל שוב עם /analyze")

# ===== Run =====
keep_alive()
print("🤖 Two-step AliExpress bot online [v2step]")
bot.infinity_polling(timeout=60, long_polling_timeout=30)
