import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_TOKEN = os.getenv("GUNES_API_TOKEN")

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
SHOP_API = "https://api.gunes-tek.com/client/api/"

def request(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode())

def telegram(method, data):
    body = urllib.parse.urlencode(data).encode()
    return request(BOT_API + method, data=body)

def send(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    telegram("sendMessage", data)

def shop_get(path):
    return request(
        SHOP_API + path,
        headers={"api-token": API_TOKEN}
    )

def main_menu():
    return {
        "keyboard": [
            [{"text": "🎮 الألعاب"}, {"text": "📱 التطبيقات"}],
            [{"text": "💰 رصيدي"}, {"text": "📦 منتجات الشحن"}],
            [{"text": "📞 خدمة العملاء"}]
        ],
        "resize_keyboard": True
    }

def get_products():
    result = shop_get("products")
    if isinstance(result, list):
        return result
    return []

def show_products(chat_id):
    try:
        products = get_products()
        available = [p for p in products if p.get("available")]

        if not available:
            send(chat_id, "لا توجد منتجات متاحة حالياً.")
            return

        for product in available[:40]:
            name = product.get("name", "منتج")
            category = product.get("category_name", "")
            price = product.get("price", "?")
            product_id = product.get("id")

            send(
                chat_id,
                f"📦 {name}\n"
                f"📂 القسم: {category}\n"
                f"💵 السعر الأساسي: {price}\n"
                f"🆔 رقم المنتج: {product_id}"
            )

    except Exception as e:
        print("Products error:", e)
        send(chat_id, "تعذر تحميل المنتجات، حاول لاحقاً.")

def show_balance(chat_id):
    try:
        profile = shop_get("profile")
        balance = profile.get("balance", "غير معروف")
        send(chat_id, f"💰 رصيد متجر الشحن: {balance}")
    except Exception as e:
        print("Balance error:", e)
        send(chat_id, "تعذر جلب الرصيد حالياً.")

def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        send(
            chat_id,
            "🌟 أهلاً بك في سوريا ستور\n"
            "لشحن الألعاب والتطبيقات اختر الخدمة:",
            main_menu()
        )

    elif text == "📦 منتجات الشحن":
        show_products(chat_id)

    elif text == "🎮 الألعاب":
        show_products(chat_id)

    elif text == "📱 التطبيقات":
        show_products(chat_id)

    elif text == "💰 رصيدي":
        show_balance(chat_id)

    elif text == "📞 خدمة العملاء":
        send(chat_id, "تواصل مع إدارة سوريا ستور للمساعدة.")

    else:
        send(chat_id, "اختر خدمة من القائمة الرئيسية.", main_menu())

def main():
    if not BOT_TOKEN or not API_TOKEN:
        raise RuntimeError("أضف مفاتيح الربط في إعدادات الاستضافة.")

    offset = 0
    print("Syria Store Bot started")

    while True:
        try:
            result = request(
                BOT_API + "getUpdates?" +
                urllib.parse.urlencode({
                    "offset": offset,
                    "timeout": 25
                })
            )

            for update in result.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if message:
                    handle_message(message)

        except Exception as e:
            print("Bot error:", e)
            time.sleep(5)

if __name__ == "__main__":
    main()
