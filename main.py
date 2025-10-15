import os, re, json, html, urllib.parse, requests
import os, re, json, html, time, hmac, hashlib, urllib.parse, requests
from bs4 import BeautifulSoup
import telebot
from flask import Flask
from threading import Thread

# ========= Settings =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@dealsumustcheck")

ALI_APP_KEY = os.getenv("ALI_APP_KEY")       # ממסך ה-Open Platform
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET") # ממסך ה-Open Platform

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ========= keep-alive (Railway) =========
app = Flask(__name__)
@app.get("/")
def home(): return "ok"
def keep_alive(): Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()

# ========= Utils =========
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
    for k, v in {"₪":"ILS","US$":"USD","$":"USD","USD":"USD",
                 "€":"EUR","EUR":"EUR","£":"GBP","GBP":"GBP",
                 "руб":"RUB","AED":"AED","SAR":"SAR","₹":"INR"}.items():
        if k in str(txt): return v
    return None

def deep_find(d, keys):
    if isinstance(d, dict):
        for k, v in d.items():
            if any(x in k.lower() for x in keys):
                if isinstance(v, (str, int, float)): return v
                if isinstance(v, dict):
                    for sub in ("value","amount","min","max","display","text","price","lowPrice"):
                        if sub in v and isinstance(v[sub], (str,int,float)): return v[sub]
                if isinstance(v, list) and v:
                    f = v[0]
                    if isinstance(f, dict):
                        for sub in ("value","amount","display","text","price"):
                            if sub in f and isinstance(f[sub], (str,int,float)): return f[sub]
                    else:
                        return f
            r = deep_find(v, keys)
            if r is not None: return r
    elif isinstance(d, list):
        for it in d:
            r = deep_find(it, keys)
            if r is not None: return r
    return None

def extract_from_json_blob(txt):
    # runParams / __INIT_DATA__
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
                    if isinstance(obj, dict) and obj.get("@type") == "Product":
                        return obj
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        except Exception:
            continue
    return None

def resolve_url(url: str) -> str:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language":"en-US,en;q=0.9,he;q=0.8"})
    try:
        resp = s.get(url, allow_redirects=True, timeout=20)
        final = resp.url
    except Exception:
        final = url
    # אחידות host: aliexpress.us -> www.aliexpress.com
    try:
        pu = urllib.parse.urlparse(final)
        if pu.netloc.lower().startswith("aliexpress.us"):
            final = urllib.parse.urlunparse(pu._replace(netloc="www.aliexpress.com"))
    except Exception:
        pass
    return final

def extract_pid_from_url(u: str):
    m = re.search(r'/item/(\d{6,20})\.html', u)
    if m: return m.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    for key in ("productId","itemId","item_id","objectId"):
        if key in qs and qs[key]:
            pid = re.sub(r'\D','', qs[key][0])
            if pid: return pid
    return None

# ========= AliExpress Open Platform (optional) =========
ALI_GATEWAY = "https://api-sg.aliexpress.com/sync"  # גייטווי סטנדרטי; אם לא יעבוד -> נשתמש בפולבק

def ali_sign(params: dict, app_secret: str) -> str:
    """
    חתימה בסגנון HMAC-SHA256 על פרמטרים ממוימיינים לפי מפתח.
    אם ל-AE יש פורמט מעט שונה בחשבון שלך, היתרון כאן: אם ייכשל -> נופל לפולבק.
    """
    # סידור אלפביתי של key=value (ללא sign עצמו)
    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
    joined = "".join(f"{k}{v}" for k, v in sorted_items)
    digest = hmac.new(app_secret.encode("utf-8"), joined.encode("utf-8"), hashlib.sha256).hexdigest().upper()
    return digest

def ali_productdetail_via_api(product_id: str):
    """
    ניסיון להביא נתוני מוצר דרך ה-API הרשמי.
    אם נכשל מכל סיבה -> נחזיר None וניפול ל-scraping.
    """
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
        "need_promotion_link": "true"
    }
    params["sign"] = ali_sign(params, ALI_APP_SECRET)
    try:
        r = requests.post(ALI_GATEWAY, data=params, timeout=15)
        data = r.json()
        # המבנה משתנה מעט; ננסה לאסוף בצורה סלחנית:
        # חפש fields סטנדרטיים
        j = data
        for path in (
            ["aliexpress_affiliate_productdetail_get_response", "result"],
            ["result"],
        ):
            cur = j
            ok = True
            for p in path:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    ok = False; break
            if ok:
                j = cur
                break
        # עכשיו j אמור להכיל products / list
        prods = None
        if isinstance(j, dict):
            prods = j.get("products") or j.get("result") or j.get("items")
        if not prods and isinstance(j, list):
            prods = j
        if not prods:
            return None
        prod = prods[0] if isinstance(prods, list) else prods
        title = prod.get("product_title") or prod.get("title")
        image = prod.get("product_main_image_url") or prod.get("main_image")
        rating = prod.get("evaluate_rate") or prod.get("avg_evaluation_rating") or prod.get("rating")
        orders = prod.get("sale_count") or prod.get("orders")
        price_ils = prod.get("target_original_price") or prod.get("target_sale_price") or prod.get("sale_price")
        currency = prod.get("target_currency") or "ILS"
        # בונים שורות
        if price_ils:
            price_line = f"💰 מחיר: {price_ils} ₪" if currency == "ILS" else f"💰 מחיר: {price_ils} {currency}"
        else:
            price_line = "💰 מחיר: לא זמין"
        rating_line = f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
        orders_line = f"📦 {orders} הזמנות" if orders else None
        return {
            "title": title or "מוצר מאלי אקספרס",
            "image": image,
            "price_line": price_line,
            "rating_line": rating_line,
            "orders_line": orders_line
        }
    except Exception:
        return None

# ========= Scraping Fallback =========
def pull_product_by_scrape(affiliate_url: str):
    final_url = resolve_url(affiliate_url)
    pid = extract_pid_from_url(final_url)

    r = requests.get(final_url, headers={"User-Agent": USER_AGENT, "Accept-Language":"en-US,en;q=0.9,he;q=0.8"}, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    ld   = extract_from_jsonld(soup)
    blob = extract_from_json_blob(r.text)

    title = (ld.get("name") if ld else None) or deep_find(blob, ["producttitle","subject","title","pagetitle"]) or (soup.title.string if soup.title else None) or "מוצר מאלי אקספרס"
    image = (ld.get("image")[0] if ld and isinstance(ld.get("image"), list) else (ld.get("image") if ld else None)) or ((soup.find("meta", {"property": "og:image"}) or {}).get("content"))
    rating = (ld.get("aggregateRating", {}) or {}).get("ratingValue") if ld else deep_find(blob, ["averagerating","ratingvalue","rating","starrating"])
    price_raw = (ld.get("offers", {}) or {}).get("price") if ld else deep_find(blob, ["saleprice","price","skuprice","actminprice","displayprice"])
    orders = deep_find(blob, ["tradecount","orders","soldcount","ordercount"]) if blob else None

    # Mobile assist
    if (not price_raw or not rating) and pid:
        m_url = f"https://m.aliexpress.com/item/{pid}.html"
        rm = requests.get(m_url, headers={"User-Agent": USER_AGENT, "Accept-Language":"en-US,en;q=0.9,he;q=0.8"}, timeout=20)
        if rm.ok:
            msoup = BeautifulSoup(rm.text, "html.parser")
            data = extract_from_json_blob(rm.text) or extract_from_jsonld(msoup)
            if data:
                title     = str(deep_find(data, ["producttitle","subject","title"]) or title)
                image     = deep_find(data, ["image","imageurl","mainimage"]) or image
                price_raw = deep_find(data, ["saleprice","discountprice","price","skuprice","minprice","displayprice"]) or price_raw
                rating    = deep_find(data, ["averagestar","avgstar","rating","ratingvalue"]) or rating
                orders    = deep_find(data, ["tradecount","soldcount","orders"]) or orders

    currency = detect_currency(price_raw) or (ld.get("offers",{}).get("priceCurrency") if ld else None) or "USD"
    amount = parse_amount(price_raw) if price_raw else None
    if amount:
        if currency != "ILS":
            rate = fetch_rate(currency, "ILS")
            price_line = f"💰 מחיר: {round(amount*rate,2)} ₪ (≈ {amount} {currency})" if rate else f"💰 מחיר: {amount} {currency}"
        else:
            price_line = f"💰 מחיר: {amount} ₪"
    else:
        price_line = "💰 מחיר: לא זמין"
    rating_line = f"⭐ דירוג: {rating}" if rating else "⭐ דירוג: לא זמין"
    orders_line = f"📦 {orders} הזמנות" if orders else None

    return {
        "title": str(title).strip(),
        "image": image,
        "price_line": price_line,
        "rating_line": rating_line,
        "orders_line": orders_line
    }

def pull_product(affiliate_url: str):
    # 1) נסה API רשמי אם יש מפתחות:
    pid = extract_pid_from_url(resolve_url(affiliate_url))
    if pid:
        api_res = ali_productdetail_via_api(pid)
        if api_res:
            return api_res
    # 2) פולבק לסקרייפינג:
    return pull_product_by_scrape(affiliate_url)

def compose_caption(info: dict, outbound_url: str):
    lines = [
        f"💥 <b>{html.escape(info['title'])}</b>",
        info["rating_line"],
        info["price_line"]
    ]
    if info.get("orders_line"):
        lines.append(info["orders_line"])
    lines.append("מוצר שמביא תמורה מעולה למחיר 💯")
    lines.append("נראה לי דיל ששווה לבדוק לא ? 🤔")
    lines.append(f'🔗 <a href="{html.escape(outbound_url)}">לק

