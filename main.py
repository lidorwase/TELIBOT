
from flask import Flask, request
import telebot
from telebot.types import Update
import os
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Your bot token
BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

app = Flask(__name__)

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        try:
            json_string = request.get_data().decode("utf-8")
            update = Update.de_json(json_string)
            bot.process_new_updates([update])
        except Exception as e:
            logger.exception("Webhook processing failed")
            return "Internal Server Error", 500
        return "", 200
    else:
        return "Unsupported Media Type", 415

@bot.message_handler(func=lambda m: isinstance(m.text, str) and (
    "aliexpress" in m.text.lower() or "s.click.aliexpress.com" in m.text.lower()))
def handle_link(message: Message):
    print(f"📩 קיבלתי קישור: {message.text}")  # חשוב להדפסה

    link = message.text.strip()
    data = pull_product(link)
    if not data:
        bot.reply_to(message, "❌ לא הצלחתי לשלוף את פרטי המוצר.")
        return

    text = f"{data['title']}\nמחיר: {data['price']}\nדירוג: {data['rating']}\nהזמנות: {data['orders']}"
    
    if data.get("image"):
        bot.send_photo(CHANNEL_USERNAME, data["image"], caption=text)
    else:
        bot.send_message(CHANNEL_USERNAME, text)

    bot.reply_to(message, "✅ פורסם לערוץ בהצלחה!")

    bot.reply_to(message, "היי! אני חי ובועט 🦾 שלח לי קישור לאלי אקספרס 📦")

def main():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Webhook set to: %s", WEBHOOK_URL)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)

if __name__ == "__main__":
    main()
