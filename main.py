import os
import re
import hmac
import time
import hashlib
import logging
import requests
import urllib.parse
import json
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

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL  = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

CAPTION_LIMIT = 1024  # מגבלת Telegram לכיתוב תמונה

# משפט פתיחה שמופיע בראש כל פוסט
HEADLINE = "דילים ששווה לבדוק לא? 👀"

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
AUTO_MIN_RATING = float(os.getenv("AUTO_MIN_RATING", "90"))   # דירוג מינימלי באחוזים
AUTO_MIN_ORDERS = int(os.getenv("AUTO_MIN_ORDERS", "100"))     # מינימום הזמנות
AUTO_MAX_PRICE  = os.getenv("AUTO_MAX_PRICE")                  # מחיר מקסימלי ב-₪ (לא חובה)
POSTED_FILE     = os.getenv("POSTED_FILE", "posted.json")      # מוצרים שכבר פורסמו (למניעת כפילויות)

waiting_for_affiliate = {}
queue_lock = Lock()

# --- Webhook ---
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
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
        "rating": p.get("evaluate_rate"),
        "price": p.get("target_app_sale_price") or p.get("target_sale_price"),
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
def generate_description(data: Dict[str, Optional[str]]) -> Optional[str]:
    try:
        prompt = (
            "כתוב תיאור שיווקי קצר (עד 3 משפטים), קולח ומושך בעברית למוצר הבא, "
            "עם אימוג'ים וקריאה לפעולה:\n"
            f"שם: {data.get('title')}\n"
            f"מחיר: {data.get('price')} ₪\n"
            f"דירוג: {data.get('rating')}\n"
            f"כמות הזמנות: {data.get('orders')}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=200,  # קצר כדי לא לחרוג ממגבלת caption
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return None


def build_caption(data: Dict[str, Optional[str]], description: str) -> str:
    """בונה caption ומוודא שלא עולה על 1024 תווים."""
    footer = (
        f"\n\n"
        f"💰 מחיר: {data.get('price')} ₪\n"
        f"⭐ דירוג: {data.get('rating')}\n"
        f"🛒 הזמנות: {data.get('orders')}\n"
        f"🔗 <a href=\"{data.get('link')}\">לרכישה כאן</a>"
    )
    header = f"<b>{HEADLINE}</b>\n\n"
    max_desc_len = CAPTION_LIMIT - len(header) - len(footer) - 10  # 10 תווים buffer
    if len(description) > max_desc_len:
        description = description[:max_desc_len].rstrip() + "..."
    return header + description + footer


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


def enqueue_post(image: Optional[str], caption: str) -> int:
    with queue_lock:
        queue = load_queue()
        queue.append({"image": image, "caption": caption})
        save_queue(queue)
        return len(queue)


def send_post(chat_id, post: Dict[str, Optional[str]]) -> None:
    if post.get("image"):
        bot.send_photo(chat_id, post["image"], caption=post["caption"], parse_mode="HTML")
    else:
        bot.send_message(chat_id, post["caption"], parse_mode="HTML")


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


def ali_hotproducts(keyword: str, page: int) -> List[Dict]:
    """מחפש מוצרים פופולריים ב-AliExpress לפי מילת חיפוש."""
    params: Dict[str, str] = {
        "app_key": ALI_APP_KEY,
        "method": "aliexpress.affiliate.hotproduct.query",
        "sign_method": "sha256",
        "timestamp": str(int(time.time() * 1000)),
        "keywords": keyword,
        "page_no": str(page),
        "page_size": "50",
        "sort": "LAST_VOLUME_DESC",
        "target_currency": "ILS",
        "target_language": "HE",
        "ship_to_country": "IL",
        "tracking_id": ALI_TRACKING_ID,
    }
    if AUTO_MAX_PRICE:
        params["max_sale_price"] = str(int(float(AUTO_MAX_PRICE) * 100))  # ב-API המחיר באגורות
    params["sign"] = ali_sign(params, ALI_APP_SECRET)

    res = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
    res.raise_for_status()
    data = res.json()

    if "error_response" in data:
        logger.error(f"AliExpress hotproduct error: {data['error_response']}")
        return []

    container = (
        data.get("aliexpress_affiliate_hotproduct_query_response", {})
            .get("resp_result", {})
            .get("result", {})
            .get("products")
    )
    if isinstance(container, dict):
        container = container.get("product")
    return container if isinstance(container, list) else []


def find_auto_product() -> Optional[Dict[str, Optional[str]]]:
    """מוצא מוצר טוב שעוד לא פורסם ומחזיר אותו בפורמט של build_caption."""
    posted = set(load_posted())
    keywords = AUTO_KEYWORDS[:]
    random.shuffle(keywords)

    for keyword in keywords[:3]:  # עד 3 ניסיונות עם מילים שונות
        try:
            products = ali_hotproducts(keyword, page=random.randint(1, 3))
        except Exception as e:
            logger.error(f"שגיאה בחיפוש '{keyword}': {e}")
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

        logger.info(f"חיפוש '{keyword}': {len(products)} מוצרים, {len(candidates)} מתאימים")
        if not candidates:
            continue

        p = random.choice(candidates[:15])  # אקראי מתוך הנמכרים ביותר
        return {
            "product_id": str(p.get("product_id")),
            "title": p.get("product_title"),
            "image": p.get("product_main_image_url"),
            "rating": p.get("evaluate_rate"),
            "price": p.get("target_app_sale_price") or p.get("target_sale_price"),
            "orders": p.get("lastest_volume"),
            "link": p.get("promotion_link"),
        }
    return None


def publish_auto_product() -> bool:
    """מוצא מוצר, כותב לו תיאור ומפרסם לערוץ. מחזיר True אם פורסם."""
    if not ALI_TRACKING_ID:
        logger.warning("ALI_TRACKING_ID לא מוגדר - לא ניתן למצוא מוצרים אוטומטית")
        return False

    data = find_auto_product()
    if not data:
        logger.warning("לא נמצא מוצר מתאים לפרסום אוטומטי")
        return False

    description = generate_description(data) or data.get("title") or "מוצר מומלץ!"
    caption = build_caption(data, description)
    send_post(CHANNEL_USERNAME, {"image": data.get("image"), "caption": caption})
    mark_posted(data["product_id"])
    logger.info(f"מוצר אוטומטי פורסם: {data['product_id']}")
    return True


def publish_scheduled() -> None:
    """בשעת פרסום: קודם פוסטים מהתור, ואם התור ריק - מוצר שהבוט מצא לבד."""
    if publish_next():
        return
    if AUTO_FIND:
        publish_auto_product()


def scheduler_loop() -> None:
    """בודק כל 20 שניות אם הגיעה אחת משעות הפרסום."""
    last_slot = None
    logger.info(f"Scheduler פעיל. שעות פרסום: {', '.join(POST_TIMES)} ({TIMEZONE})")
    while True:
        try:
            now = datetime.now(TIMEZONE)
            hhmm = now.strftime("%H:%M")
            slot = now.strftime("%Y-%m-%d ") + hhmm
            if hhmm in POST_TIMES and slot != last_slot:
                last_slot = slot
                publish_scheduled()
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
            position = enqueue_post(post["image"], caption)
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
        "/autopost - למצוא מוצר לבד ולפרסם אותו עכשיו",
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
    if not ALI_TRACKING_ID:
        bot.reply_to(message, "❌ לא הוגדר ALI_TRACKING_ID")
        return
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if publish_auto_product():
            bot.reply_to(message, "✅ מצאתי מוצר ופרסמתי לערוץ")
        else:
            bot.reply_to(message, "😕 לא מצאתי מוצר מתאים. נסה שוב או שנה את AUTO_KEYWORDS")
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
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Bot is live! Webhook: {WEBHOOK_URL}")

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
