import os
import re
import hmac
import time
import hashlib
import logging
import requests
import urllib.parse
import json
import html
import random

from dotenv import load_dotenv
from telebot import TeleBot, types
from telebot.types import Message
from flask import Flask, request
from threading import Thread, Lock
from datetime import datetime
from zoneinfo import ZoneInfo
from openai import OpenAI
from typing import Optional, Dict, List

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

# כתובת ה-Webhook נגזרת מהטוקן (hash) כדי שהטוקן עצמו לא יופיע בלוגים
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET[:32]}"
WEBHOOK_URL  = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

CAPTION_LIMIT = 1024  # מגבלת Telegram לכיתוב תמונה

# משפט פתיחה שמופיע בראש כל פוסט
HEADLINE = "דילים ששווה לבדוק לא? 👀"

# שורה לקונים חדשים מתחת למחיר. אפשר לשנות דרך NEW_BUYER_LINE, או לכבות עם NEW_BUYER_LINE=off
NEW_BUYER_LINE = os.getenv(
    "NEW_BUYER_LINE",
    "💡 קונים חדשים ב-AliExpress? המחיר שלכם עשוי להיות עוד יותר נמוך!",
).strip()
if NEW_BUYER_LINE.lower() in ("off", "false", "0", "no"):
    NEW_BUYER_LINE = ""

# --- הגדרות פרסום אוטומטי ---
# שעות פרסום (שעון ישראל), מופרדות בפסיק. למשל: "10:00,14:00,20:30"
POST_TIMES = [
    t.strip() for t in os.getenv("POST_TIMES", "10:00,14:00,20:00").split(",") if t.strip()
]
TIMEZONE   = ZoneInfo(os.getenv("TIMEZONE", "Asia/Jerusalem"))
QUEUE_FILE = os.getenv("QUEUE_FILE", "queue.json")
# מזהי טלגרם (מספרים) של מי שמורשה להוסיף פוסטים לתור. ריק = כולם.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# --- הגדרות מציאת מוצרים אוטומטית מה-API ---
# כשהתור ריק בשעת פרסום, הבוט מחפש לבד מוצר פופולרי ומפרסם אותו.
ALI_TRACKING_ID = os.getenv("ALI_TRACKING_ID")  # נדרש כדי שהקישורים יהיו קישורי שותפים
AUTO_FIND = os.getenv("AUTO_FIND", "true").lower() in ("1", "true", "yes", "on")
# מילות חיפוש (באנגלית עובד הכי טוב), מופרדות בפסיק. בכל פעם נבחרת אחת אקראית.
AUTO_KEYWORDS = [
    k.strip() for k in os.getenv(
        "AUTO_KEYWORDS",
        "gadgets,kitchen,home organization,phone accessories,car accessories,smart home,camping",
    ).split(",") if k.strip()
]
# Tracking ID נפרד לכל קטגוריה (לא חובה). פורמט:
#   TRACKING_ID:מילת חיפוש|מילת חיפוש,TRACKING_ID:מילת חיפוש
# למשל: CARS:car accessories|car gadgets,KITCHEN:kitchen gadgets
# אם מוגדר - הבוט מחפש רק בקטגוריות האלה, וכל מוצר מקבל את ה-Tracking ID של הקטגוריה שלו.
def parse_categories(raw: str) -> List[tuple]:
    categories = []
    raw = raw.strip().strip('"').strip("'")
    for part in re.split(r"[,;\n]", raw):
        if ":" not in part:
            continue
        tracking_id, words = part.split(":", 1)
        tracking_id = tracking_id.strip().strip('"').strip("'")
        words = words.strip().strip('"').strip("'")
        keywords = [w.strip() for w in words.split("|") if w.strip()]
        if tracking_id.strip() and keywords:
            categories.append((tracking_id.strip(), keywords))
    return categories


AUTO_CATEGORIES_RAW = os.getenv("AUTO_CATEGORIES", "")
AUTO_CATEGORIES = parse_categories(AUTO_CATEGORIES_RAW)


def auto_config_status() -> str:
    """תיאור קצר של הגדרות המציאה האוטומטית - לאבחון בעיות."""
    if AUTO_CATEGORIES:
        return "קטגוריות: " + ", ".join(tid for tid, _ in AUTO_CATEGORIES)
    if AUTO_CATEGORIES_RAW:
        return (
            f"AUTO_CATEGORIES קיים ({len(AUTO_CATEGORIES_RAW)} תווים) אבל לא הצלחתי לקרוא אותו. "
            f"מתחיל ב: {AUTO_CATEGORIES_RAW[:30]!r}"
        )
    if ALI_TRACKING_ID:
        return f"Tracking ID יחיד: {ALI_TRACKING_ID}"
    similar = [k for k in os.environ if "CATEG" in k.upper() or "TRACK" in k.upper()]
    return "AUTO_CATEGORIES לא קיים בכלל" + (
        f". משתנים עם שם דומה שכן קיימים: {', '.join(similar)}" if similar else ""
    )
AUTO_MIN_RATING = float(os.getenv("AUTO_MIN_RATING", "90"))   # דירוג מינימלי באחוזים
AUTO_MIN_ORDERS = int(os.getenv("AUTO_MIN_ORDERS", "100"))     # מינימום הזמנות
AUTO_MAX_PRICE  = os.getenv("AUTO_MAX_PRICE")                  # מחיר מקסימלי ב-₪ (לא חובה)
POSTED_FILE     = os.getenv("POSTED_FILE", "posted.json")      # מוצרים שכבר פורסמו (למניעת כפילויות)

# --- חבילת טיקטוק ---
# אחרי כל פרסום בערוץ, הבוט שולח בפרטי תמונות/סרטון + תסריט וכיתוב מוכנים לטיקטוק.
TIKTOK_PACK = os.getenv("TIKTOK_PACK", "true").lower() in ("1", "true", "yes", "on")
# לאן לשלוח: מזהי טלגרם מופרדים בפסיק. ברירת מחדל: ADMIN_IDS
TIKTOK_CHAT_IDS = {
    int(x) for x in os.getenv("TIKTOK_CHAT_ID", "").replace(" ", "").split(",") if x
} or ADMIN_IDS
# סרטון אנכי שהבוט מייצר מתמונות המוצר (בתוך חבילת הטיקטוק)
TIKTOK_VIDEO = os.getenv("TIKTOK_VIDEO", "true").lower() in ("1", "true", "yes", "on")

# --- צמיחת הערוץ ---
# כפתורים מתחת לכל פוסט: "לרכישה" ו"שתפו את הערוץ"
POST_BUTTONS = os.getenv("POST_BUTTONS", "true").lower() in ("1", "true", "yes", "on")
# פוסט שבועי של הדילים הכי שווים. פורמט: "יום שעה" באנגלית, למשל "fri 12:00". off = כבוי
WEEKLY_POST  = os.getenv("WEEKLY_POST", "fri 12:00").strip().lower()
HISTORY_FILE = os.getenv("HISTORY_FILE", "history.json")       # דילים שפורסמו (לפוסט השבועי)
# קישורי הצטרפות נפרדים לכל מקור, כדי לדעת מאיפה מגיעים המנויים
INVITE_SOURCES = [
    x.strip() for x in os.getenv("INVITE_SOURCES", "TikTok,Instagram,Facebook,WhatsApp").split(",")
    if x.strip()
]
LINKS_FILE = os.getenv("LINKS_FILE", "invite_links.json")

waiting_for_affiliate = {}
queue_lock = Lock()

# --- Webhook ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    # טלגרם שולח את הסוד בכותרת - דוחים בקשות שלא הגיעו מטלגרם
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        return "Forbidden", 403
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


def product_images(p: Dict) -> List[str]:
    """כל תמונות המוצר מה-API: התמונה הראשית ואחריה התמונות הנוספות."""
    images = [p.get("product_main_image_url")]
    extra = p.get("product_small_image_urls")
    if isinstance(extra, dict):
        extra = extra.get("string")
    if isinstance(extra, list):
        images.extend(extra)
    seen, result = set(), []
    for url in images:
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


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
        "images": product_images(p),
        "video": p.get("product_video_url"),
        "rating": p.get("evaluate_rate"),
        "price": p.get("target_app_sale_price") or p.get("target_sale_price"),
        "original_price": p.get("target_original_price"),
        "discount": p.get("discount"),
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
def clean_repetitions(text: str) -> str:
    """מוחק מילה או צירוף שחוזרים ברצף (למשל "רפורמה רפורמה רפורמה")."""
    # צירופים של 1-4 מילים שחוזרים פעמיים ומעלה ברצף -> פעם אחת
    for n in (4, 3, 2, 1):
        pattern = r"((?:\S+\s+){%d})\1+" % n
        text = re.sub(pattern, r"\1", text + " ").strip()
    return text


def generate_description(data: Dict[str, Optional[str]]) -> Optional[str]:
    try:
        price, original, saving, percent = price_info(data)
        if original:
            deal = (
                f"מחיר רגיל: {original:.2f} ₪\n"
                f"מחיר עכשיו: {price:.2f} ₪\n"
                f"חיסכון: {saving:.2f} ₪ ({percent}% הנחה)\n"
            )
        else:
            deal = f"מחיר: {data.get('price')} ₪\n"
        prompt = (
            "כתוב תיאור שיווקי קצר (2-3 משפטים), קולח ומושך בעברית למוצר הבא, "
            "עם אימוג'ים. הדגש מה המוצר עושה ולמה הוא שווה"
            + (" ואת ההנחה והחיסכון" if original else "")
            + ". אל תכתוב את המחירים עצמם ואל תוסיף קישור - הם מופיעים בהמשך הפוסט. "
            "תכתוב רק את התיאור, בלי כותרת.\n"
            f"שם המוצר: {data.get('title')}\n"
            + deal
            + f"דירוג: {data.get('rating')}\n"
            f"כמות הזמנות: {data.get('orders')}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            frequency_penalty=0.8,  # מונע חזרה על אותה מילה שוב ושוב
            max_tokens=200,  # קצר כדי לא לחרוג ממגבלת caption
        )
        text = clean_repetitions((response.choices[0].message.content or "").strip())
        logger.info(f"תיאור מ-OpenAI ({len(text)} תווים): {text[:80]!r}")
        return text or None
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return None


def to_float(value) -> Optional[float]:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def price_info(data: Dict[str, Optional[str]]):
    """מחזיר (מחיר נוכחי, מחיר רגיל, חיסכון, אחוז הנחה). ערכים חסרים = None."""
    price = to_float(data.get("price"))
    original = to_float(data.get("original_price"))
    if price is None:
        return None, None, None, None
    if not original:
        # אם אין מחיר מקורי אבל יש אחוז הנחה - מחשבים ממנו
        discount = parse_percent(data.get("discount"))
        if 0 < discount < 100:
            original = price / (1 - discount / 100)
    if original and original > price * 1.01:
        saving = original - price
        return price, original, saving, round(saving / original * 100)
    return price, None, None, None


def format_price(data: Dict[str, Optional[str]]) -> str:
    """שורות מחיר בנוסח שיווקי: מחיר רגיל, מחיר עכשיו, כמה חוסכים."""
    price, original, saving, percent = price_info(data)
    if price is None:
        return f"💰 מחיר: {data.get('price')} ₪"
    if original is None:
        return f"🔥 מחיר: {price:.2f} ₪ בלבד!"
    return (
        f"❌ מחיר רגיל: <s>{original:.2f} ₪</s>\n"
        f"🔥 מחיר עכשיו: <b>{price:.2f} ₪</b>\n"
        f"🤑 חוסכים {saving:.2f} ₪ ({percent}% הנחה)!"
    )


def visible_len(html_text: str) -> int:
    """אורך הטקסט כפי שטלגרם סופר אותו - בלי תגיות HTML ובלי כתובות קישורים."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def build_caption(data: Dict[str, Optional[str]], description: str) -> str:
    """בונה caption ומוודא שהטקסט הנראה לא עולה על 1024 תווים."""
    footer = (
        f"\n\n"
        f"{format_price(data)}\n"
        + (f"{html.escape(NEW_BUYER_LINE, quote=False)}\n" if NEW_BUYER_LINE else "")
        + f"⭐ דירוג: {data.get('rating')}\n"
        f"🛒 הזמנות: {data.get('orders')}\n"
        f"🔗 <a href=\"{html.escape(str(data.get('link')), quote=True)}\">לרכישה כאן</a>"
    )
    header = f"<b>{HEADLINE}</b>\n\n"
    max_desc_len = CAPTION_LIMIT - visible_len(header) - visible_len(footer) - 10  # buffer
    description = (description or "").strip() or (data.get("title") or "")
    if len(description) > max_desc_len:
        description = description[:max(max_desc_len, 0)].rstrip() + "..."
    return header + html.escape(description, quote=False) + footer


# --- תור פוסטים לפרסום אוטומטי ---
def load_queue() -> List[Dict[str, Optional[str]]]:
    try:
        with open(QUEUE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"שגיאה בקריאת התור: {e}")
        return []


def save_queue(queue: List[Dict[str, Optional[str]]]) -> None:
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE_FILE)


def enqueue_post(image: Optional[str], caption: str, data: Optional[Dict] = None) -> int:
    with queue_lock:
        queue = load_queue()
        queue.append({"image": image, "caption": caption, "data": data})
        save_queue(queue)
        return len(queue)


def public_channel_url() -> Optional[str]:
    name = (CHANNEL_USERNAME or "").lstrip("@")
    return f"https://t.me/{name}" if name and not name.lstrip("-").isdigit() else None


def post_keyboard(post: Dict) -> Optional[types.InlineKeyboardMarkup]:
    """כפתור לרכישה + כפתור לשיתוף הערוץ."""
    if not POST_BUTTONS:
        return None
    keyboard = types.InlineKeyboardMarkup()
    link = (post.get("data") or {}).get("link")
    if link and str(link).startswith("http"):
        keyboard.add(types.InlineKeyboardButton("🛒 לרכישה", url=link))
    channel = public_channel_url()
    if channel:
        share = "https://t.me/share/url?" + urllib.parse.urlencode(
            {"url": channel, "text": "🔥 ערוץ דילים מאלי אקספרס - שווה להצטרף!"}
        )
        keyboard.add(types.InlineKeyboardButton("📢 שתפו את הערוץ עם חברים", url=share))
    return keyboard if keyboard.keyboard else None


def send_post(chat_id, post: Dict[str, Optional[str]]) -> None:
    markup = post_keyboard(post)
    if post.get("image"):
        bot.send_photo(chat_id, post["image"], caption=post["caption"], parse_mode="HTML",
                       reply_markup=markup)
    else:
        bot.send_message(chat_id, post["caption"], parse_mode="HTML", reply_markup=markup)


def publish_next() -> bool:
    """מפרסם לערוץ את הפוסט הבא בתור. מחזיר True אם פורסם."""
    with queue_lock:
        queue = load_queue()
        if not queue:
            logger.info("התור ריק - אין מה לפרסם")
            return False
        post = queue[0]
        send_post(CHANNEL_USERNAME, post)  # אם נכשל - הפוסט נשאר בתור
        queue.pop(0)
        save_queue(queue)
    logger.info(f"פוסט פורסם לערוץ {CHANNEL_USERNAME}. נשארו בתור: {len(queue)}")
    if post.get("data"):
        record_history(post["data"])
        send_tiktok_pack(post["data"])
    return True


# --- מציאת מוצרים אוטומטית ---
def load_posted() -> List[str]:
    try:
        with open(POSTED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.error(f"שגיאה בקריאת {POSTED_FILE}: {e}")
        return []


def mark_posted(product_id: str) -> None:
    posted = load_posted()
    posted.append(product_id)
    tmp = POSTED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(posted[-1000:], f)  # שומר את 1000 האחרונים
    os.replace(tmp, POSTED_FILE)


def parse_percent(value) -> float:
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0


# שיטות חיפוש לפי סדר עדיפות. product.query זמינה לכל אפליקציית שותפים רגילה,
# hotproduct.query דורשת הרשאה מתקדמת. הבוט זוכר איזו שיטה עבדה.
SEARCH_METHODS = [
    "aliexpress.affiliate.product.query",
    "aliexpress.affiliate.hotproduct.query",
]
no_permission_methods = set()


def ali_search_products(keyword: str, page: int, tracking_id: str) -> List[Dict]:
    """מחפש מוצרים ב-AliExpress לפי מילת חיפוש, ממוינים לפי כמות מכירות."""
    for method in SEARCH_METHODS:
        if method in no_permission_methods:
            continue

        params: Dict[str, str] = {
            "app_key": ALI_APP_KEY,
            "method": method,
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),
            "keywords": keyword,
            "page_no": str(page),
            "page_size": "50",
            "sort": "LAST_VOLUME_DESC",
            "target_currency": "ILS",
            "target_language": "HE",
            "ship_to_country": "IL",
            "tracking_id": tracking_id,
        }
        if AUTO_MAX_PRICE:
            params["max_sale_price"] = str(int(float(AUTO_MAX_PRICE) * 100))  # ב-API המחיר באגורות
        params["sign"] = ali_sign(params, ALI_APP_SECRET)

        res = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
        res.raise_for_status()
        data = res.json()

        if "error_response" in data:
            error = data["error_response"]
            if error.get("code") == "InsufficientPermission":
                logger.warning(f"אין הרשאה ל-{method}, מנסה שיטה אחרת")
                no_permission_methods.add(method)
                continue
            logger.error(f"AliExpress search error ({method}): {error}")
            return []

        resp_result = (
            data.get(method.replace(".", "_") + "_response", {}).get("resp_result", {})
        )
        if str(resp_result.get("resp_code", "200")) != "200":
            logger.warning(f"AliExpress ({method}): {resp_result.get('resp_code')} {resp_result.get('resp_msg')}")

        container = (resp_result.get("result") or {}).get("products")
        if isinstance(container, dict):
            container = container.get("product")
        return container if isinstance(container, list) else []

    logger.error(
        "לאפליקציה אין הרשאה לאף שיטת חיפוש מוצרים: " + ", ".join(SEARCH_METHODS)
    )
    return []


def find_auto_product() -> Optional[Dict[str, Optional[str]]]:
    """מוצא מוצר טוב שעוד לא פורסם ומחזיר אותו בפורמט של build_caption."""
    posted = set(load_posted())

    # רשימת אפשרויות חיפוש: (tracking_id, מילת חיפוש)
    if AUTO_CATEGORIES:
        options = [(tid, random.choice(words)) for tid, words in AUTO_CATEGORIES]
    else:
        options = [(ALI_TRACKING_ID, k) for k in AUTO_KEYWORDS]
    random.shuffle(options)

    for tracking_id, keyword in options[:3]:  # עד 3 ניסיונות
        try:
            products = ali_search_products(keyword, random.randint(1, 3), tracking_id)
        except Exception as e:
            logger.error(f"שגיאה בחיפוש '{keyword}' ({tracking_id}): {e}")
            continue

        candidates = []
        for p in products:
            pid = str(p.get("product_id") or "")
            orders = int(p.get("lastest_volume") or 0)
            link = p.get("promotion_link")
            if (
                pid and link
                and pid not in posted
                and parse_percent(p.get("evaluate_rate")) >= AUTO_MIN_RATING
                and orders >= AUTO_MIN_ORDERS
            ):
                candidates.append(p)

        logger.info(
            f"חיפוש '{keyword}' ({tracking_id}): {len(products)} מוצרים, {len(candidates)} מתאימים"
        )
        if not candidates:
            continue

        p = random.choice(candidates[:15])  # אקראי מתוך הנמכרים ביותר
        logger.info(
            "מחירים מה-API למוצר %s: %s",
            p.get("product_id"),
            {k: v for k, v in p.items() if "price" in k or "discount" in k},
        )
        return {
            "product_id": str(p.get("product_id")),
            "title": p.get("product_title"),
            "image": p.get("product_main_image_url"),
            "images": product_images(p),
            "video": p.get("product_video_url"),
            "rating": p.get("evaluate_rate"),
            "price": p.get("target_app_sale_price") or p.get("target_sale_price"),
            "original_price": p.get("target_original_price"),
            "discount": p.get("discount"),
            "orders": p.get("lastest_volume"),
            "link": p.get("promotion_link"),
            "tracking_id": tracking_id,
        }
    return None


def publish_auto_product() -> bool:
    """מוצא מוצר, כותב לו תיאור ומפרסם לערוץ. מחזיר True אם פורסם."""
    if not ALI_TRACKING_ID and not AUTO_CATEGORIES:
        logger.warning("ALI_TRACKING_ID / AUTO_CATEGORIES לא מוגדרים - לא ניתן למצוא מוצרים אוטומטית")
        return False

    data = find_auto_product()
    if not data:
        logger.warning("לא נמצא מוצר מתאים לפרסום אוטומטי")
        return False

    description = generate_description(data) or data.get("title") or "מוצר מומלץ!"
    caption = build_caption(data, description)
    send_post(CHANNEL_USERNAME, {"image": data.get("image"), "caption": caption, "data": data})
    mark_posted(data["product_id"])
    record_history(data)
    logger.info(f"מוצר אוטומטי פורסם: {data['product_id']} ({data['tracking_id']})")
    send_tiktok_pack(data)
    return True


# --- חבילת טיקטוק ---
def channel_link() -> str:
    name = (CHANNEL_USERNAME or "").lstrip("@")
    return f"t.me/{name}" if name and not name.lstrip("-").isdigit() else "הערוץ שלי בטלגרם"


def generate_tiktok_script(data: Dict) -> Optional[str]:
    """כותב תסריט, טקסטים למסך, כיתוב והאשטגים לסרטון טיקטוק על המוצר."""
    price, original, saving, percent = price_info(data)
    deal = (
        f"מחיר רגיל {original:.0f} ₪, עכשיו {price:.0f} ₪ ({percent}% הנחה)"
        if original else f"מחיר: {data.get('price')} ₪"
    )
    prompt = (
        "אתה יוצר תוכן ויראלי לטיקטוק בעברית, בערוץ דילים מאלי אקספרס. "
        "כתוב חבילה לסרטון קצר (15-25 שניות) על המוצר הבא. "
        "המטרה: שהצופים ייכנסו לערוץ הטלגרם כדי לקבל את הקישור. "
        "אל תכתוב קישור למוצר - הקישור נמצא רק בטלגרם.\n\n"
        f"מוצר: {data.get('title')}\n{deal}\n"
        f"דירוג: {data.get('rating')}, הזמנות: {data.get('orders')}\n\n"
        "כתוב בדיוק במבנה הזה, בלי markdown ובלי כוכביות:\n"
        "🎣 הוק (2 שניות ראשונות):\n<משפט פתיחה שעוצר גלילה>\n\n"
        "🎬 תסריט:\n<3-5 שורות קצרות - מה אומרים או מראים, שורה לכל קטע>\n\n"
        "📱 טקסטים למסך:\n<3-4 טקסטים קצרים, אחד בכל שורה. "
        "האחרון: 'הקישור בטלגרם - בביו 👆'>\n\n"
        "✍️ כיתוב לפוסט:\n<1-2 משפטים + קריאה להיכנס לטלגרם בביו>\n\n"
        "#️⃣ האשטגים:\n<8-10 האשטגים בעברית ובאנגלית>\n\n"
        "📌 תגובה נעוצה:\n<משפט קצר שמפנה לטלגרם בביו>\n\n"
        "🔎 מילת חיפוש בערוץ:\n<מילה או שתיים בעברית שמתארות את המוצר>"
    )
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.8,
            frequency_penalty=0.5,
            max_tokens=700,
        )
        text = clean_repetitions((response.choices[0].message.content or "").strip())
        return text or None
    except Exception as e:
        logger.error(f"OpenAI error (tiktok): {e}")
        return None


def send_tiktok_pack(data: Dict) -> None:
    """שולח בפרטי תמונות/סרטון של המוצר + תסריט מוכן לטיקטוק. לא מפיל את הפרסום אם נכשל."""
    if not TIKTOK_PACK:
        return
    if not TIKTOK_CHAT_IDS:
        logger.warning("חבילת טיקטוק: לא הוגדרו ADMIN_IDS או TIKTOK_CHAT_ID - לא יודע למי לשלוח")
        return
    video_path = None
    try:
        script = generate_tiktok_script(data)
        if TIKTOK_VIDEO:
            try:
                from video import make_tiktok_video
                video_path = make_tiktok_video(data, price_info)
            except Exception as e:
                logger.error(f"יצירת סרטון טיקטוק נכשלה: {e}", exc_info=True)
        images = (data.get("images") or [data.get("image")])[:10]
        images = [u for u in images if u]
        title = (data.get("title") or "")[:200]

        for chat_id in TIKTOK_CHAT_IDS:
            try:
                bot.send_message(chat_id, f"🎵 חבילת טיקטוק למוצר שפורסם עכשיו:\n{title}", parse_mode=None)
                if video_path:
                    with open(video_path, "rb") as f:
                        bot.send_video(
                            chat_id, f, supports_streaming=True, width=720, height=1280,
                            caption="🎬 סרטון מוכן לטיקטוק - שמור לגלריה, העלה והוסף סאונד טרנדי באפליקציה",
                        )
                if data.get("video"):
                    try:
                        bot.send_video(chat_id, data["video"], caption="🎬 סרטון המוצר")
                    except Exception:
                        bot.send_message(chat_id, f"🎬 סרטון המוצר: {data['video']}", parse_mode=None)
                if len(images) > 1:
                    bot.send_media_group(chat_id, [types.InputMediaPhoto(u) for u in images])
                elif images:
                    bot.send_photo(chat_id, images[0])
                if script:
                    bot.send_message(
                        chat_id,
                        f"{script}\n\n🔗 בביו: {channel_link()}",
                        parse_mode=None,
                    )
            except Exception as e:
                logger.error(f"שליחת חבילת טיקטוק ל-{chat_id} נכשלה: {e}")
    except Exception as e:
        logger.error(f"חבילת טיקטוק נכשלה: {e}", exc_info=True)
    finally:
        if video_path:
            try:
                os.remove(video_path)
            except OSError:
                pass


def publish_scheduled() -> None:
    """בשעת פרסום: קודם פוסטים מהתור, ואם התור ריק - מוצר שהבוט מצא לבד."""
    if publish_next():
        return
    if AUTO_FIND:
        publish_auto_product()


# --- היסטוריה ופוסט שבועי ---
def load_json_file(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        logger.error(f"שגיאה בקריאת {path}: {e}")
        return default


def save_json_file(path: str, value) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def record_history(data: Dict) -> None:
    try:
        price, original, saving, percent = price_info(data)
        history = load_json_file(HISTORY_FILE, [])
        history.append({
            "ts": time.time(),
            "title": data.get("title"),
            "price": price,
            "percent": percent or 0,
            "orders": data.get("orders"),
            "link": data.get("link"),
        })
        save_json_file(HISTORY_FILE, history[-300:])
    except Exception as e:
        logger.error(f"שמירת היסטוריה נכשלה: {e}")


def short_hebrew_names(titles: List[str]) -> List[str]:
    """שמות קצרים בעברית למוצרים (2-4 מילים). אם OpenAI לא זמין - קיצור של השם המקורי."""
    fallback = [(t or "מוצר")[:45].rstrip() + ("…" if len(t or "") > 45 else "") for t in titles]
    try:
        prompt = (
            "תן שם קצר וברור בעברית (2-4 מילים) לכל אחד מהמוצרים הבאים. "
            "החזר JSON בלבד: מערך של מחרוזות באותו סדר.\n"
            + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        text = (response.choices[0].message.content or "").strip()
        names = json.loads(text[text.index("["): text.rindex("]") + 1])
        if isinstance(names, list) and len(names) == len(titles):
            return [str(n).strip() or fb for n, fb in zip(names, fallback)]
    except Exception as e:
        logger.warning(f"שמות קצרים: משתמש בשמות המקוריים ({e})")
    return fallback


def publish_weekly() -> bool:
    """מפרסם לערוץ את 5 הדילים עם ההנחה הגבוהה ביותר מ-7 הימים האחרונים."""
    week_ago = time.time() - 7 * 24 * 3600
    deals = [d for d in load_json_file(HISTORY_FILE, []) if d.get("ts", 0) >= week_ago and d.get("link")]
    if not deals:
        logger.info("פוסט שבועי: אין דילים מהשבוע האחרון")
        return False

    def orders(d):
        try:
            return int(float(d.get("orders") or 0))
        except ValueError:
            return 0

    top = sorted(deals, key=lambda d: (d.get("percent") or 0, orders(d)), reverse=True)[:5]
    names = short_hebrew_names([d.get("title") or "" for d in top])
    medals = ["🥇", "🥈", "🥉", "🔥", "🔥"]
    lines = ["🏆 <b>הדילים הכי שווים של השבוע</b>", ""]
    for medal, name, d in zip(medals, names, top):
        price = f" - {d['price']:.2f} ₪" if d.get("price") else ""
        discount = f" ({d['percent']}% הנחה)" if d.get("percent") else ""
        url = html.escape(str(d["link"]), quote=True)
        lines.append(f'{medal} <a href="{url}">{html.escape(name)}</a>{price}{discount}')
    lines += ["", "👇 עוד דילים כל יום כאן בערוץ - שתפו עם חברים!"]

    markup = post_keyboard({})
    bot.send_message(CHANNEL_USERNAME, "\n".join(lines), parse_mode="HTML",
                     disable_web_page_preview=True, reply_markup=markup)
    logger.info(f"פוסט שבועי פורסם ({len(top)} דילים)")
    return True


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def weekly_schedule() -> Optional[tuple]:
    """("fri 12:00") -> (4, "12:00"). None אם כבוי או לא תקין."""
    parts = WEEKLY_POST.split()
    if len(parts) == 2 and parts[0][:3] in WEEKDAYS and re.fullmatch(r"\d{1,2}:\d{2}", parts[1]):
        hh, mm = parts[1].split(":")
        return WEEKDAYS[parts[0][:3]], f"{int(hh):02d}:{mm}"
    if WEEKLY_POST not in ("off", "false", "0", "no", ""):
        logger.warning(f"WEEKLY_POST לא תקין: {WEEKLY_POST!r} (דוגמה: 'fri 12:00')")
    return None


# --- קישורי הצטרפות לפי מקור ---
def get_invite_links() -> Dict[str, str]:
    """יוצר (פעם אחת) קישור הצטרפות נפרד לכל מקור ושומר אותו."""
    links = load_json_file(LINKS_FILE, {})
    changed = False
    for source in INVITE_SOURCES:
        if source not in links:
            link = bot.create_chat_invite_link(CHANNEL_USERNAME, name=source[:32])
            links[source] = link.invite_link
            changed = True
    if changed:
        save_json_file(LINKS_FILE, links)
    return links


def scheduler_loop() -> None:
    """בודק כל 20 שניות אם הגיעה אחת משעות הפרסום."""
    last_slot = None
    last_weekly = None
    weekly = weekly_schedule()
    logger.info(f"Scheduler פעיל. שעות פרסום: {', '.join(POST_TIMES)} ({TIMEZONE})")
    if weekly:
        logger.info(f"פוסט שבועי: יום {weekly[0]} (0=שני) בשעה {weekly[1]}")
    while True:
        try:
            now = datetime.now(TIMEZONE)
            hhmm = now.strftime("%H:%M")
            slot = now.strftime("%Y-%m-%d ") + hhmm
            if hhmm in POST_TIMES and slot != last_slot:
                last_slot = slot
                publish_scheduled()
            if weekly and now.weekday() == weekly[0] and hhmm == weekly[1] and slot != last_weekly:
                last_weekly = slot
                publish_weekly()
        except Exception as e:
            logger.error(f"שגיאה בפרסום מתוזמן: {e}", exc_info=True)
        time.sleep(20)


def is_admin(message: Message) -> bool:
    return not ADMIN_IDS or message.from_user.id in ADMIN_IDS


# --- Handler קישורים ---
@bot.message_handler(
    func=lambda m: (
        isinstance(m.text, str)
        and "http" in m.text.lower()
        and m.chat.id not in waiting_for_affiliate
    )
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
        
    except Exception as e:
        logger.error(f"שגיאה ב-handle_link: {e}", exc_info=True)
        bot.reply_to(message, "❌ אירעה שגיאה בעיבוד הקישור.")
        

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
        post = {"image": data.get("image"), "caption": caption}

        # תצוגה מקדימה למשתמש
        send_post(message.chat.id, post)

        # הוספה לתור הפרסום האוטומטי
        if CHANNEL_USERNAME and is_admin(message):
            position = enqueue_post(post["image"], caption, data)
            bot.send_message(
                message.chat.id,
                f"🗓 הפוסט נוסף לתור (מקום {position}).\n"
                f"יפורסם ב-{CHANNEL_USERNAME} בשעות: {', '.join(POST_TIMES)}",
            )

    except Exception as e:
        logger.error(f"Affiliate handler error: {e}")
        bot.reply_to(message, "❌ שגיאה בהוספת קישור השותפים.")


# --- /start ---
@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    bot.reply_to(
        message,
        "היי! שלח לי קישור למוצר מאלי אקספרס ואני אחזיר לך תיאור שיווקי מוכן לפרסום.\n\n"
        "הפוסטים נכנסים לתור ומתפרסמים אוטומטית בערוץ בשעות הקבועות.\n"
        "/queue - כמה פוסטים בתור\n"
        "/postnow - לפרסם עכשיו את הפוסט הבא\n"
        "/clearqueue - לרוקן את התור\n"
        "/autopost - למצוא מוצר לבד ולפרסם אותו עכשיו\n"
        "/weekly - לפרסם עכשיו את הדילים הכי שווים של השבוע\n"
        "/links - קישורי הצטרפות נפרדים לכל מקור (TikTok, אינסטגרם...)\n"
        "/myid - המזהה שלך בטלגרם\n\n"
        "🎵 אחרי כל פרסום בערוץ תקבל כאן חבילת טיקטוק: תמונות + תסריט וכיתוב מוכנים.",
    )


# --- /myid ---
@bot.message_handler(commands=["myid"])
def myid_cmd(message: types.Message):
    user_id = message.from_user.id
    status = "✅ אתה מוגדר כמנהל." if user_id in ADMIN_IDS else (
        "⚠️ עדיין לא מוגדר כמנהל." if ADMIN_IDS else "ℹ️ ADMIN_IDS עדיין לא מוגדר ב-Railway."
    )
    bot.reply_to(
        message,
        f"🆔 המזהה שלך: <code>{user_id}</code>\n\n"
        f"{status}\n"
        "כדי לקבל את חבילת הטיקטוק, שים את המספר הזה ב-Railway במשתנה ADMIN_IDS.",
    )


# --- ניהול התור ---
@bot.message_handler(commands=["queue"])
def queue_cmd(message: types.Message):
    if not is_admin(message):
        return
    count = len(load_queue())
    bot.reply_to(
        message,
        f"📋 בתור: {count} פוסטים\n⏰ שעות פרסום: {', '.join(POST_TIMES)}",
    )


@bot.message_handler(commands=["weekly"])
def weekly_cmd(message: types.Message):
    if not is_admin(message):
        return
    if not CHANNEL_USERNAME:
        bot.reply_to(message, "❌ לא הוגדר CHANNEL_USERNAME")
        return
    try:
        if publish_weekly():
            bot.reply_to(message, "✅ הפוסט השבועי פורסם לערוץ")
        else:
            bot.reply_to(message, "אין עדיין דילים מהשבוע האחרון")
    except Exception as e:
        logger.error(f"weekly error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ הפרסום נכשל: {e}")


@bot.message_handler(commands=["links"])
def links_cmd(message: types.Message):
    if not is_admin(message):
        return
    if not CHANNEL_USERNAME:
        bot.reply_to(message, "❌ לא הוגדר CHANNEL_USERNAME")
        return
    try:
        links = get_invite_links()
    except Exception as e:
        logger.error(f"links error: {e}", exc_info=True)
        bot.reply_to(
            message,
            "❌ לא הצלחתי ליצור קישורים. ודא שלבוט יש בערוץ הרשאת ניהול "
            f"'הוספת מנויים / Invite users via link'.\n\n{e}",
            parse_mode=None,
        )
        return
    lines = ["🔗 קישורי הצטרפות לפי מקור:", ""]
    for source, url in links.items():
        lines.append(f"<b>{html.escape(source)}</b>\n{url}\n")
    lines.append(
        "שים כל קישור רק במקום שלו (למשל קישור TikTok בביו של הטיקטוק).\n"
        "📊 כמה הצטרפו מכל קישור: בערוץ ← ניהול הערוץ ← Invite Links."
    )
    bot.reply_to(message, "\n".join(lines), disable_web_page_preview=True)


@bot.message_handler(commands=["postnow"])
def postnow_cmd(message: types.Message):
    if not is_admin(message):
        return
    if not CHANNEL_USERNAME:
        bot.reply_to(message, "❌ לא הוגדר CHANNEL_USERNAME")
        return
    try:
        if publish_next():
            bot.reply_to(message, "✅ פורסם לערוץ")
        else:
            bot.reply_to(message, "התור ריק")
    except Exception as e:
        logger.error(f"postnow error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ הפרסום נכשל: {e}")


@bot.message_handler(commands=["autopost"])
def autopost_cmd(message: types.Message):
    if not is_admin(message):
        return
    if not CHANNEL_USERNAME:
        bot.reply_to(message, "❌ לא הוגדר CHANNEL_USERNAME")
        return
    if not ALI_TRACKING_ID and not AUTO_CATEGORIES:
        bot.reply_to(
            message,
            f"❌ לא הוגדרו ALI_TRACKING_ID או AUTO_CATEGORIES\n🔎 {auto_config_status()}",
        )
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if publish_auto_product():
            bot.reply_to(message, "✅ מצאתי מוצר ופרסמתי לערוץ")
        else:
            bot.reply_to(message, "😕 לא מצאתי מוצר מתאים. נסה שוב או שנה את מילות החיפוש")
    except Exception as e:
        logger.error(f"autopost error: {e}", exc_info=True)
        bot.reply_to(message, f"❌ הפרסום נכשל: {e}")


@bot.message_handler(commands=["clearqueue"])
def clearqueue_cmd(message: types.Message):
    if not is_admin(message):
        return
    with queue_lock:
        save_queue([])
    bot.reply_to(message, "🗑 התור רוקן")


# --- הרצה ---
def main():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    logger.info(f"Bot is live! Webhook: {WEBHOOK_URL}")
    logger.info(f"Auto-find: {'on' if AUTO_FIND else 'off'} | {auto_config_status()}")

    if CHANNEL_USERNAME:
        Thread(target=scheduler_loop, daemon=True).start()
    else:
        logger.warning("CHANNEL_USERNAME לא מוגדר - פרסום אוטומטי כבוי")

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )


if __name__ == "__main__":
    main()
