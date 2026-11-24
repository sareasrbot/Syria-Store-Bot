import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error

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

PAGE_SIZE = 10


# =========================
# HTTP
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
        body = error.read().decode("utf-8", errors="replace")
        log.error("HTTP ERROR %s: %s", error.code, body[:1500])
        raise RuntimeError(f"HTTP {error.code}: {body[:300]}")

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
        "text": str(text)
    }

    if keyboard:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    return telegram("sendMessage", data)


def send_photo(chat_id, photo_url, caption):
    data = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1024]
    }

    return telegram("sendPhoto", data)


def answer_callback(callback_id, text=""):
    return telegram("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text
    })


# =========================
# GUNES API
# =========================

def shop_get(path):
    if not API_TOKEN:
        raise RuntimeError("GUNES_API_TOKEN is missing")

    return request_json(
        SHOP_API + path,
        headers={"api-token": API_TOKEN}
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
            log.error("Unexpected Gunes response: %s", repr(result)[:2000])
            raise RuntimeError("Unexpected products response")

    else:
        raise RuntimeError("Invalid products response")

    log.info("Gunes products received: %s", len(products))

    # فحص أسماء حقول الصور في سجل Render
    if products:
        log.info("PRODUCT FIELDS: %s", list(products[0].keys()))

    return [p for p in products if isinstance(p, dict)]


def is_available(product):
    value = product.get("available")
    return value is True or str(value).lower() in ("1", "true", "yes")


# =========================
# IMAGE DETECTION
# =========================

def get_product_image(product):
    image_keys = (
        "image",
        "image_url",
        "photo",
        "photo_url",
        "icon",
        "icon_url",
        "logo",
        "logo_url",
        "thumbnail",
        "thumbnail_url",
        "picture",
        "picture_url"
    )

    for key in image_keys:
        value = product.get(key)

        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value

        if isinstance(value, dict):
            for subkey in ("url", "src", "link"):
                url = value.get(subkey)
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    return url

    return None


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
# PRODUCT FILTER
# =========================

def filter_products(category):
    products = [
        p for p in get_products()
        if is_available(p)
    ]

    if category == "game":
        products = [
            p for p in products
            if "game" in str(p.get("category_name", "")).lower()
            or "game" in str(p.get("name", "")).lower()
        ]

    elif category == "app":
        products = [
            p for p in products
            if "app" in str(p.get("category_name", "")).lower()
            or "app" in str(p.get("name", "")).lower()
        ]

    return products


# =========================
# PRODUCT PAGES
# =========================

def show_products(chat_id, category="all", page=0):
    try:
        products = filter_products(category)

        if not products:
            send(chat_id, "لا توجد منتجات متاحة حالياً.")
            return

        total = len(products)
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

        if page < 0:
            page = 0

        if page >= total_pages:
            page = total_pages - 1

        start = page * PAGE_SIZE
        current = products[start:start + PAGE_SIZE]

        keyboard = []

        for product in current:
            name = str(product.get("name", "منتج"))
            price = str(product.get("price", "?"))
            product_id = product.get("id")

            if product_id is None:
                continue

            button_text = f"{name} - {price}$"

            keyboard.append([
                {
                    "text": button_text[:60],
                    "callback_data": f"pr|{product_id}"
                }
            ])

        navigation = []

        if page > 0:
            navigation.append({
                "text": "⬅️ السابق",
                "callback_data": f"pg|{category}|{page - 1}"
            })

        if page < total_pages - 1:
            navigation.append({
                "text": "التالي ➡️",
                "callback_data": f"pg|{category}|{page + 1}"
            })

        if navigation:
            keyboard.append(navigation)

        markup = {
            "inline_keyboard": keyboard
        }

        category_names = {
            "all": "جميع المنتجات",
            "game": "الألعاب",
            "app": "التطبيقات"
        }

        send(
            chat_id,
            f"🛍️ سوريا ستور\n"
            f"📂 القسم: {category_names.get(category, category)}\n"
            f"📦 عدد المنتجات: {total}\n"
            f"📄 الصفحة: {page + 1} من {total_pages}\n\n"
            f"اختر المنتج الذي تريده:",
            markup
        )

    except Exception:
        log.exception("Products loading failed")
        send(chat_id, "تعذر تحميل المنتجات حالياً.")


# =========================
# PRODUCT DETAILS
# =========================

def show_product_details(chat_id, product_id):
    try:
        products = get_products()

        product = next(
            (
                p for p in products
                if str(p.get("id")) == str(product_id)
            ),
            None
        )

        if not product:
            send(chat_id, "لم يتم العثور على هذا المنتج.")
            return

        name = product.get("name", "منتج")
        price = product.get("price", "?")
        qty = product.get("qty_values")
        product_type = product.get("product_type", "غير محدد")

        message = (
            f"📦 {name}\n"
            f"💵 السعر: {price}\n"
            f"🆔 رقم المنتج: {product_id}\n"
            f"🔢 نوع المنتج: {product_type}"
        )

        if qty is not None:
            message += f"\n🔢 الكميات: {qty}"
        else:
            message += "\n🔢 الكمية المطلوبة: 1"

        image_url = get_product_image(product)

        if image_url:
            try:
                send_photo(chat_id, image_url, message)
                return
            except Exception:
                log.exception("Product image sending failed")

        send(chat_id, message)

    except Exception:
        log.exception("Product details failed")
        send(chat_id, "تعذر عرض تفاصيل المنتج حالياً.")


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
            f"💰 رصيد متجر الشحن\n\nالرصيد: {balance}"
        )

    except Exception:
        log.exception("Balance loading failed")
        send(chat_id, "تعذر جلب الرصيد حالياً.")


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
            "🌟 أهلاً بك في سوريا ستور\n\nاختر الخدمة التي تريدها:",
            main_menu()
        )

    elif text in ("📦 جميع المنتجات", "📦 منتجات الشحن"):
        show_products(chat_id, "all", 0)

    elif text == "🎮 الألعاب":
        show_products(chat_id, "game", 0)

    elif text == "📱 التطبيقات":
        show_products(chat_id, "app", 0)

    elif text == "💰 رصيدي":
        show_balance(chat_id)

    elif text == "📞 خدمة العملاء":
        send(
            chat_id,
            "📞 خدمة العملاء\n\n"
            "تواصل مع إدارة سوريا ستور للمساعدة."
        )

    else:
        send(chat_id, "اختر خدمة من القائمة:", main_menu())


# =========================
# CALLBACK HANDLER
# =========================

def handle_callback(callback):
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not callback_id or not chat_id:
        return

    try:
        parts = data.split("|")

        if parts[0] == "pg" and len(parts) == 3:
            category = parts[1]
            page = int(parts[2])

            answer_callback(callback_id)
            show_products(chat_id, category, page)

        elif parts[0] == "pr" and len(parts) == 2:
            product_id = parts[1]

            answer_callback(callback_id, "جاري تحميل المنتج...")
            show_product_details(chat_id, product_id)

        else:
            answer_callback(callback_id, "طلب غير معروف")

    except Exception:
        log.exception("Callback handling failed")
        answer_callback(callback_id, "حدث خطأ، حاول مرة أخرى.")


# =========================
# WEBHOOK
# =========================

@app.route("/", methods=["GET"])
def home():
    return "Syria Store Bot is running", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    try:
        if update.get("message"):
            handle_message(update["message"])

        elif update.get("callback_query"):
            handle_callback(update["callback_query"])

    except Exception:
        log.exception("Webhook handling failed")

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
        {
            "url": webhook_url,
            "allowed_updates": json.dumps(
                ["message", "callback_query"]
            )
        }
    )

    log.info("Webhook setup: %s", result)

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
