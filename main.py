import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error

from collections import defaultdict
from flask import Flask, request, jsonify

# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_TOKEN = os.getenv("GUNES_API_TOKEN")
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
SHOP_API = "https://api.gunes-tek.com/client/api/"

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("syria_store")

# =========================
# HTTP REQUEST
# =========================

def request_json(url, headers=None, data=None):
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "SyriaStoreBot/1.0"
    }

    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(
        url,
        headers=request_headers,
        data=data
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)

    except urllib.error.HTTPError as error:
        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        log.error(
            "HTTP ERROR %s: %s",
            error.code,
            body[:1500]
        )

        raise RuntimeError(
            f"HTTP {error.code}: {body[:300]}"
        )

    except urllib.error.URLError as error:
        log.error("CONNECTION ERROR: %s", error.reason)
        raise RuntimeError("Connection to server failed")

    except json.JSONDecodeError:
        log.exception("Invalid JSON response")
        raise RuntimeError("Invalid JSON response")


# =========================
# TELEGRAM
# =========================

def telegram(method, data):
    body = urllib.parse.urlencode(data).encode()

    result = request_json(
        BOT_API + method,
        headers={
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data=body
    )

    if not result.get("ok"):
        log.error("Telegram API error: %s", result)

    return result


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


# =========================
# GUNES API
# =========================

def shop_get(path):
    if not API_TOKEN:
        raise RuntimeError("GUNES_API_TOKEN is missing")

    return request_json(
        SHOP_API + path,
        headers={
            "api-token": API_TOKEN
        }
    )


def get_products():
    result = shop_get("products")

    if isinstance(result, list):
        products = result

    elif isinstance(result, dict):
        products = None

        for key in ("data", "products", "result"):
            if isinstance(result.get(key), list):
                products = result[key]
                break

        if products is None:
            log.error(
                "Unexpected Gunes response: %s",
                repr(result)[:2000]
            )
            raise RuntimeError("Unexpected products response")

    else:
        raise RuntimeError("Invalid products response")

    log.info("Gunes products received: %s", len(products))

    return [
        p for p in products
        if isinstance(p, dict)
    ]


def is_available(product):
    value = product.get("available")

    return value is True or str(value).lower() in (
        "1", "true", "yes"
    )


# =========================
# MAIN MENU
# =========================

def main_menu():
    return {
        "keyboard": [
            [
                {"text": "🎮 الألعاب"},
                {"text": "📱 التطبيقات"}
            ],
            [
                {"text": "📦 جميع المنتجات"},
                {"text": "💰 رصيدي"}
            ],
            [
                {"text": "📞 خدمة العملاء"}
            ]
        ],
        "resize_keyboard": True
    }


# =========================
# PRODUCTS
# =========================

def show_products(chat_id, category_filter=None):
    try:
        products = get_products()

        available = [
            p for p in products
            if is_available(p)
        ]

        if category_filter:
            available = [
                p for p in available
                if category_filter.lower() in
                str(p.get("category_name", "")).lower()
                or category_filter.lower() in
                str(p.get("name", "")).lower()
            ]

        log.info(
            "Products total=%s available=%s",
            len(products),
            len(available)
        )

        if not available:
            send(chat_id, "لا توجد منتجات متاحة في هذا القسم حالياً.")
            return

        # Group by category
        groups = defaultdict(list)

        for product in available:
            category = product.get(
                "category_name",
                "منتجات أخرى"
            )
            groups[str(category)].append(product)

        send(
            chat_id,
            f"📦 المنتجات المتاحة: {len(available)}\n"
            f"عدد الأقسام: {len(groups)}"
        )

        sent = 0

        for category, items in groups.items():
            send(
                chat_id,
                f"📂 القسم: {category}\n"
                f"عدد المنتجات: {len(items)}"
            )

            for product in items:
                if sent >= 40:
                    send(
                        chat_id,
                        "تم عرض أول 40 منتجاً. "
                        "يمكنك طلب بقية المنتجات لاحقاً."
                    )
                    return

                name = product.get("name", "منتج")
                price = product.get("price", "?")
                product_id = product.get("id")
                qty = product.get("qty_values")
                product_type = product.get(
                    "product_type",
                    "غير محدد"
                )

                message = (
                    f"📦 {name}\n"
                    f"💵 السعر: {price}\n"
                    f"🆔 رقم المنتج: {product_id}\n"
                    f"🔢 نوع المنتج: {product_type}"
                )

                if qty is not None:
                    message += (
                        "\n🔢 الكميات: "
                        + str(qty)
                    )
                else:
                    message += "\n🔢 الكمية المطلوبة: 1"

                send(chat_id, message)
                sent += 1

    except Exception:
        log.exception("Products loading failed")

        send(
            chat_id,
            "تعذر تحميل المنتجات حالياً.\n"
            "تم تسجيل تفاصيل المشكلة للفحص."
        )


# =========================
# BALANCE
# =========================

def show_balance(chat_id):
    try:
        profile = shop_get("profile")

        if not isinstance(profile, dict):
            raise RuntimeError("Invalid profile response")

        balance = profile.get("balance", "غير معروف")

        send(
            chat_id,
            f"💰 رصيد متجر الشحن\n\n"
            f"الرصيد: {balance}"
        )

    except Exception:
        log.exception("Balance loading failed")

        send(
            chat_id,
            "تعذر جلب الرصيد حالياً."
        )


# =========================
# MESSAGE HANDLER
# =========================

def handle_message(message):
    if "chat" not in message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        send(
            chat_id,
            "🌟 أهلاً بك في سوريا ستور\n\n"
            "اختر الخدمة التي تريدها:",
            main_menu()
        )

    elif text in (
        "📦 جميع المنتجات",
        "📦 منتجات الشحن"
    ):
        show_products(chat_id)

    elif text == "🎮 الألعاب":
        show_products(chat_id, "game")

    elif text == "📱 التطبيقات":
        show_products(chat_id, "app")

    elif text == "💰 رصيدي":
        show_balance(chat_id)

    elif text == "📞 خدمة العملاء":
        send(
            chat_id,
            "📞 خدمة العملاء\n\n"
            "تواصل مع إدارة سوريا ستور للمساعدة."
        )

    else:
        send(
            chat_id,
            "اختر خدمة من القائمة:",
            main_menu()
        )


# =========================
# WEBHOOK
# =========================

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

        except Exception:
            log.exception("Message handling failed")

    return jsonify({"ok": True})


# =========================
# START
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    if not API_TOKEN:
        raise RuntimeError("Missing GUNES_API_TOKEN")

    if not RENDER_URL:
        raise RuntimeError("Missing RENDER_EXTERNAL_URL")

    webhook_url = RENDER_URL.rstrip("/") + "/webhook"

    result = telegram(
        "setWebhook",
        {"url": webhook_url}
    )

    log.info("Webhook setup: %s", result)

    # Test Gunes connection on startup
    try:
        profile = shop_get("profile")
        log.info(
            "Gunes profile test successful: %s",
            isinstance(profile, dict)
        )
    except Exception:
        log.exception("Gunes startup connection test failed")

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )


if __name__ == "__main__":
    main()
