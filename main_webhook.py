
from flask import request
import telebot.types

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{os.getenv('RAILWAY_STATIC_URL')}{WEBHOOK_PATH}"

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_string = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return "", 200
    else:
        return "Unsupported Media Type", 415

@bot.message_handler(commands=['start'])
def handle_start(message: Message):
    bot.reply_to(message, "היי! אני חי ובועט 🦾 שלח לי קישור לאלי אקספרס 📦")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and "aliexpress" in m.text.lower())
def handle_link(message: Message):
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

def main():
    keep_alive()
    logger.info("✅ Bot is running with Webhook!")
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    main()
