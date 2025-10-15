import telebot
import requests
from bs4 import BeautifulSoup

# 🔒 הכנס כאן את הטוקן שלך בין הגרשיים
BOT_TOKEN = "8335966151:AAFoWoIF_Dh9bfXPkyezhT3EhHima2VwIr0"

CHANNEL_USERNAME = "@dealsumustcheck"

bot = telebot.TeleBot(BOT_TOKEN)

def get_product_info(url):
    """
    שולף נתונים בסיסיים מדף מוצר באלי אקספרס:
    שם, תמונה, מחיר, דירוג, וכמות רכישות
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    title = soup.find("title").text.strip() if soup.find("title") else "מוצר מאלי אקספרס"
    image = None
    for img in soup.find_all("img"):
        if "jpg" in img.get("src", ""):
            image = img["src"]
            break

    price = "לא זמין"
    rating = "לא זמין"
    orders = "לא זמין"

    text = (
        f"💥 מוצר שווה מאלי אקספרס 💥\n"
        f"⭐ דירוג: {rating}\n"
        f"💰 מחיר: {price} ₪\n"
        f"📦 {orders} רכישות\n"
        f"דיל ששווה לבדוק לא? 🤔\n"
        f"🔗 [לקנייה באלי אקספרס]({url})"
    )

    return text, image

@bot.message_handler(func=lambda message: message.text and "aliexpress" in message.text)
def handle_link(message):
    url = message.text.strip()
    text, image = get_product_info(url)
    
    try:
        if image:
            bot.send_photo(CHANNEL_USERNAME, image, caption=text, parse_mode="Markdown")
        else:
            bot.send_message(CHANNEL_USERNAME, text, parse_mode="Markdown")
        bot.reply_to(message, "✅ המוצר פורסם בהצלחה בערוץ!")
    except Exception as e:
        bot.reply_to(message, f"❌ שגיאה: {e}")

@bot.message_handler(commands=["start"])
def send_welcome(message):
    bot.reply_to(message, "שלח לי קישור של מוצר מאלי אקספרס, ואני אפרסם אותו בערוץ 🔥")

print("🤖 הבוט פועל... שלח לו קישור מאלי אקספרס כדי לבדוק.")
bot.polling()
