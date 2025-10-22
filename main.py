 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/main.py b/main.py
index 83909b0e29bdf4dcc295fcf5db9cc435f3af1fd6..94688e25c1a546dd5a36ca718957b069103f876b 100644
--- a/main.py
+++ b/main.py
@@ -1,171 +1,268 @@
+import html
+import logging
 import os
 import re
-import json
-import html
 import time
 import hmac
 import hashlib
 import urllib.parse
+from typing import Dict, Optional
+
+import openai
 import requests
-from bs4 import BeautifulSoup
 import telebot
+from dotenv import load_dotenv
 from flask import Flask
 from threading import Thread
-import openai
-from dotenv import load_dotenv
-import os
+from telebot.types import Message
+
+
+logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
+logger = logging.getLogger(__name__)
 
 # טען את קובץ ה־.env באופן מפורש
 load_dotenv(dotenv_path=".env", override=True)
 
-# הדפס לבדיקה
-print("Loaded vars:", {k: bool(os.getenv(k)) for k in ["BOT_TOKEN", "CHANNEL_USERNAME", "ALI_APP_KEY", "ALI_APP_SECRET", "OPENAI_API_KEY"]})
-from dotenv import load_dotenv
-import os
-import re
-import requests
 
-# טוען את קובץ .env
-load_dotenv(dotenv_path=".env", override=True)
+def validate_env_variables() -> Dict[str, str]:
+    """Load and validate required environment variables."""
 
+    required = [
+        "BOT_TOKEN",
+        "CHANNEL_USERNAME",
+        "ALI_APP_KEY",
+        "ALI_APP_SECRET",
+        "OPENAI_API_KEY",
+    ]
+    loaded: Dict[str, str] = {}
+    missing = []
 
-# ====== LOAD ENVIRONMENT VARIABLES ======
-BOT_TOKEN = os.getenv("BOT_TOKEN")
-CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
-ALI_APP_KEY = os.getenv("ALI_APP_KEY")
-ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")
-OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
+    logger.info("🔍 Checking environment variables…")
+    for var in required:
+        value = os.getenv(var)
+        if value:
+            logger.info("✅ %s loaded (%d chars)", var, len(value))
+            loaded[var] = value
+        else:
+            logger.error("❌ %s is missing!", var)
+            missing.append(var)
 
-openai.api_key = OPENAI_API_KEY
+    if missing:
+        raise EnvironmentError(
+            "Missing required environment variables: " + ", ".join(missing)
+        )
+
+    logger.info("------------------------------------------------")
+    return loaded
+
+
+ENV = validate_env_variables()
 
-# ====== DEBUG: Check Variables Loaded ======
-print("🔍 Checking environment variables...")
-for var in ["BOT_TOKEN", "CHANNEL_USERNAME", "ALI_APP_KEY", "ALI_APP_SECRET", "OPENAI_API_KEY"]:
-    value = os.getenv(var)
-    if value:
-        print(f"✅ {var} loaded ({len(value)} chars)")
-    else:
-        print(f"❌ {var} is missing!")
-print("------------------------------------------------")
+BOT_TOKEN = ENV["BOT_TOKEN"]
+CHANNEL_USERNAME = ENV["CHANNEL_USERNAME"]
+ALI_APP_KEY = ENV["ALI_APP_KEY"]
+ALI_APP_SECRET = ENV["ALI_APP_SECRET"]
+OPENAI_API_KEY = ENV["OPENAI_API_KEY"]
+
+openai.api_key = OPENAI_API_KEY
 
 # ====== INITIAL SETUP ======
 bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
 app = Flask(__name__)
 
-def keep_alive():
-    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)).start()
+
+def keep_alive() -> None:
+    Thread(
+        target=lambda: app.run(
+            host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False
+        )
+    ).start()
 
 # ====== HELPER FUNCTIONS ======
-def http_get(url, timeout=20):
-    s = requests.Session()
-    s.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9,he;q=0.8"})
-    return s.get(url, timeout=timeout, allow_redirects=True)
+SESSION = requests.Session()
+SESSION.headers.update(
+    {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9,he;q=0.8"}
+)
+
+
+def http_get(url: str, timeout: int = 20) -> requests.Response:
+    """Perform a GET request with a shared session."""
+
+    return SESSION.get(url, timeout=timeout, allow_redirects=True)
+
+
+def resolve_url(url: str) -> str:
+    """Resolve potential redirects for a URL."""
 
-def resolve_url(url):
     try:
-        return http_get(url).url
-    except Exception:
+        resolved_url = http_get(url).url
+        logger.debug("Resolved %s to %s", url, resolved_url)
+        return resolved_url
+    except requests.RequestException as exc:
+        logger.warning("Failed to resolve URL %s: %s", url, exc)
         return url
 
-def extract_pid(u: str):
-    m = re.search(r'/item/(\d{6,20})\.html', u)
-    if m:
-        return m.group(1)
-    qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
-    for key in ("productId","itemId","item_id","objectId"):
-        if key in qs and qs[key]:
-            pid = re.sub(r'\D','', qs[key][0])
+
+def extract_pid(url: str) -> Optional[str]:
+    """Extract an AliExpress product id from a URL."""
+
+    match = re.search(r"/item/(\d{6,20})\.html", url)
+    if match:
+        return match.group(1)
+
+    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
+    for key in ("productId", "itemId", "item_id", "objectId"):
+        if key in query and query[key]:
+            pid = re.sub(r"\D", "", query[key][0])
             if pid:
                 return pid
     return None
 
-def ali_sign(params: dict, app_secret: str) -> str:
-    sorted_items = sorted((k, v) for k, v in params.items() if k != "sign" and v is not None)
+
+def ali_sign(params: Dict[str, Optional[str]], app_secret: str) -> str:
+    """Generate an AliExpress API signature."""
+
+    sorted_items = sorted(
+        (k, v) for k, v in params.items() if k != "sign" and v is not None
+    )
     joined = "".join(f"{k}{v}" for k, v in sorted_items)
     return hmac.new(app_secret.encode(), joined.encode(), hashlib.sha256).hexdigest().upper()
 
-def ali_productdetail(product_id: str):
+
+def ali_productdetail(product_id: str) -> Optional[Dict[str, Optional[str]]]:
+    """Fetch product details from the AliExpress affiliate API."""
+
     ts = int(time.time() * 1000)
-    params = {
+    params: Dict[str, Optional[str]] = {
         "app_key": ALI_APP_KEY,
         "method": "aliexpress.affiliate.productdetail.get",
         "sign_method": "sha256",
-        "timestamp": ts,
+        "timestamp": str(ts),
         "product_ids": product_id,
         "target_currency": "ILS",
         "target_language": "HE",
         "need_promotion_link": "false",
     }
     params["sign"] = ali_sign(params, ALI_APP_SECRET)
+
     try:
-        r = requests.post("https://api-sg.aliexpress.com/sync", data=params, timeout=15)
-        data = r.json()
-        p = data["aliexpress_affiliate_productdetail_get_response"]["result"]["products"][0]
-        return {
-            "title": p.get("product_title"),
-            "image": p.get("product_main_image_url"),
-            "rating": p.get("evaluate_rate"),
-            "price": p.get("target_sale_price"),
-            "orders": p.get("sale_count")
-        }
-    except Exception:
+        response = requests.post(
+            "https://api-sg.aliexpress.com/sync", data=params, timeout=15
+        )
+        response.raise_for_status()
+        data = response.json()
+        products = (
+            data
+            .get("aliexpress_affiliate_productdetail_get_response", {})
+            .get("result", {})
+            .get("products", [])
+        )
+        if not isinstance(products, list) or not products:
+            logger.error("No products returned for %s", product_id)
+            return None
+        product = products[0]
+    except (requests.RequestException, ValueError, KeyError) as exc:
+        logger.error("Failed to fetch product %s: %s", product_id, exc)
         return None
 
-def pull_product(url):
-    pid = extract_pid(resolve_url(url))
-    return ali_productdetail(pid)
+    return {
+        "title": product.get("product_title"),
+        "image": product.get("product_main_image_url"),
+        "rating": product.get("evaluate_rate"),
+        "price": product.get("target_sale_price"),
+        "orders": product.get("sale_count"),
+    }
+
+
+def pull_product(url: str) -> Optional[Dict[str, Optional[str]]]:
+    """Retrieve product data for a given AliExpress URL."""
+
+    product_id = extract_pid(resolve_url(url))
+    if not product_id:
+        logger.warning("Could not extract product id from %s", url)
+        return None
+    return ali_productdetail(product_id)
+
 
 # ====== AI MARKETING COPY ======
-def generate_description_ai(title):
-    prompt = f"כתוב תיאור שיווקי בעברית למוצר בשם: {title}. השתמש בסגנון מושך, קצר ואמין, עם דגש על יתרונות, בלי הגזמות."
+def generate_description_ai(title: str) -> str:
+    prompt = (
+        f"כתוב תיאור שיווקי בעברית למוצר בשם: {title}. "
+        "השתמש בסגנון מושך, קצר ואמין, עם דגש על יתרונות, בלי הגזמות."
+    )
     try:
         response = openai.ChatCompletion.create(
             model="gpt-3.5-turbo",
-            messages=[{"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים למוצרים"},
-                      {"role": "user", "content": prompt}],
+            messages=[
+                {"role": "system", "content": "אתה כותב תיאורים שיווקיים קצרים למוצרים"},
+                {"role": "user", "content": prompt},
+            ],
             max_tokens=100,
-            temperature=0.8
+            temperature=0.8,
         )
         return response.choices[0].message.content.strip()
-    except Exception:
+    except Exception as exc:  # noqa: BLE001
+        logger.warning("Falling back to static description: %s", exc)
         return f"{title} - מוצר איכותי ושימושי במיוחד 👌"
 
-def format_post(data, url):
-    desc = generate_description_ai(data['title'])
-    price = data.get("price", "לא זמין")
-    rating = data.get("rating", "לא זמין")
-    orders = data.get("orders", "לא ידוע")
-    text = f"""<b>{data['title']}</b>
 
-{desc}
+def format_post(data: Dict[str, Optional[str]], url: str) -> str:
+    title = data.get("title") or "מוצר"
+    description = generate_description_ai(title)
+    price = data.get("price") or "לא זמין"
+    rating = data.get("rating") or "לא זמין"
+    orders = data.get("orders") or "לא ידוע"
+
+    safe_title = html.escape(title)
+    safe_description = html.escape(description)
+    safe_price = html.escape(str(price))
+    safe_rating = html.escape(str(rating))
+    safe_orders = html.escape(str(orders))
+    safe_url = html.escape(url, quote=True)
+
+    text = f"""<b>{safe_title}</b>
 
-⭐ דירוג: {rating}
-💰 מחיר: {price} ₪
-📦 הזמנות: {orders}
+{safe_description}
+
+⭐ דירוג: {safe_rating}
+💰 מחיר: {safe_price} ₪
+📦 הזמנות: {safe_orders}
 
 נראה לי דיל ששווה לבדוק לא? 🤔
-🔗 <a href="{html.escape(url)}">לקנייה באלי אקספרס</a>"""
+🔗 <a href="{safe_url}">לקנייה באלי אקספרס</a>"""
     return text
 
+
 # ====== TELEGRAM HANDLER ======
-@bot.message_handler(func=lambda m: "aliexpress" in m.text.lower())
-def send_post(m):
-    link = m.text.strip()
+@bot.message_handler(func=lambda m: isinstance(m.text, str) and "aliexpress" in m.text.lower())
+def send_post(message: Message) -> None:
+    if not isinstance(message.text, str):
+        bot.reply_to(message, "❌ נדרש קישור טקסטואלי.")
+        return
+
+    link = message.text.strip()
     try:
         data = pull_product(link)
         if not data:
-            bot.reply_to(m, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
+            bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
             return
         caption = format_post(data, link)
         if data.get("image"):
             bot.send_photo(CHANNEL_USERNAME, data["image"], caption=caption)
         else:
             bot.send_message(CHANNEL_USERNAME, caption)
-        bot.reply_to(m, "✅ פורסם לערוץ בהצלחה!")
-    except Exception as e:
-        bot.reply_to(m, f"שגיאה: {e}")
+        bot.reply_to(message, "✅ פורסם לערוץ בהצלחה!")
+    except Exception as exc:  # noqa: BLE001
+        logger.exception("Failed to publish product")
+        bot.reply_to(message, f"שגיאה: {exc}")
+
 
 # ====== START ======
-keep_alive()
-print("✅ Bot is running and ready!")
-bot.infinity_polling()
+def main() -> None:
+    keep_alive()
+    logger.info("✅ Bot is running and ready!")
+    bot.infinity_polling()
+
+
+if __name__ == "__main__":
+    main()
 
EOF
)
