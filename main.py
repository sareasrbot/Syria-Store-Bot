import os
import json
import urllib.request
import urllib.parse
import urllib.error
from flask import Flask, request, jsonify

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_TOKEN = os.getenv("GUNES_API_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

app = Flask(__name__)

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
SHOP_API = "https://api.gunes-tek.com/client/api/"


def request_json(url, headers=None, data=None):
    req = urllib.request.Request(
        url,
        headers=headers or {},
        data=data
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print("HTTP ERROR:", error.code, body[:2000])
        raise RuntimeError(
            f"HTTP {error.code}: {body[:500]}"
        )

    except urllib.error.URLError as error:
        print("URL ERROR:", repr(error))
        raise


def telegram(method, data):
    body = urllib.parse.urlencode(data).encode()
    return request_json(BOT_API + method, data=body)


def send(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    return telegram("sendMessage", data)


def shop_get(path):
    if not API_TOKEN:
        raise RuntimeError("GUNES_API_TOKEN is missing")

    return request_json(
        SHOP_API + path,
        headers={
            "api-token": API_TOKEN,
            "Accept": "application/json"
        }
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

    print(
        "GUNES PRODUCTS RESPONSE:",
        repr(result)[:3000]
    )

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        for key in ("data", "products", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return value

        raise RuntimeError(
            "Unexpected products response: "
            + repr(result)[:1000]
        )

    raise RuntimeError(
        "Unexpected products response type: "
        + str(type(result))
    )


def show_products(chat_id):
    try:
        products = get_products()

        available = [
            p for p in products
            if isinstance(p, dict)
            and p.get("available") is True
        ]

        print(
            "PRODUCTS TOTAL:",
            len(products),
            "AVAILABLE:",
            len(available)
        )

        if not available:
            send(
                chat_id,
                "لا توجد منتجات متاحة حالياً."
            )
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
                f"💵 السعر: {price}\n"
                f"🆔 رقم المنتج: {product_id}"
            )

    except Exception as error:
        print("Products error:", repr(error))

        send(
            chat_id,
            "تعذر تحميل المنتجات حالياً.\n"
            "يرجى المحاولة لاحقاً."
        )


def show_balance(chat_id):
    try:
        profile = shop_get("profile")

        if not isinstance(profile, dict):
            raise RuntimeError(
                "Unexpected profile response"
            )

        balance = profile.get(
            "balance",
            "غير معروف"
        )

        send(
            chat_id,
            f"💰 رصيد متجر الشحن: {balance}"
        )

    except Exception as error:
        print("Balance error:", repr(error))

        send(
            chat_id,
            "تعذر جلب الرصيد حالياً."
        )


def handle_message(message):
    if "chat" not in message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        send(
            chat_id,
            "🌟 أهلاً بك في سوريا ستور\n"
            "لشحن الألعاب والتطبيقات اختر الخدمة:",
            main_menu()
        )

    elif text in (
        "📦 منتجات الشحن",
        "🎮 الألعاب",
        "📱 التطبيقات"
    ):
        show_products(chat_id)

    elif text == "💰 رصيدي":
        show_balance(chat_id)

    elif text == "📞 خدمة العملاء":
        send(
            chat_id,
            "تواصل مع إدارة سوريا ستور للمساعدة."
        )

    else:
        send(
            chat_id,
            "اختر خدمة من القائمة الرئيسية.",
            main_menu()
        )


@app.route("/", methods=["GET"])
def home():
    return "Syria Store Bot is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message")

    if message:
        try:
            handle_message(message)

        except Exception as error:
            print("Message error:", repr(error))

    return jsonify({"ok": True})


def main():
    if not BOT_TOKEN or not API_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or GUNES_API_TOKEN"
        )

    if not RENDER_URL:
        raise RuntimeError(
            "Missing RENDER_EXTERNAL_URL"
        )

    webhook_url = (
        RENDER_URL.rstrip("/") + "/webhook"
    )

    result = telegram(
        "setWebhook",
        {"url": webhook_url}
    )

    print("Webhook setup:", result)

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    main()
