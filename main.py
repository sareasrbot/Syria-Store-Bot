import os
import json
import uuid
import time
import logging
import sqlite3
import urllib.request
import urllib.parse
import urllib.error
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ============================================================
# SYRIA STORE - Telegram digital goods shop
# Persistent SQLite DB + Telegram webhook + Gunes Tek API
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GUNES_TOKEN = os.getenv("GUNES_API_TOKEN", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "8214089375"))
DB_PATH = os.getenv("DB_PATH", "/var/data/syria_store.db")
SHOP_API = "https://api.gunes-tek.com/"
PAGE_SIZE = 8
MARKUP_PERCENT = Decimal(os.getenv("STORE_MARKUP_PERCENT", "10"))
SUPPORT_TEXT = os.getenv("SUPPORT_TEXT", "تواصل مع إدارة سوريا ستور عبر حساب الدعم الخاص بالمتجر.")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("syria_store")
app = Flask(__name__)

# ------------------------- DATABASE -------------------------

def db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS states(
            user_id INTEGER PRIMARY KEY,
            state TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payment_methods(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            instructions TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS topups(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
            method_id INTEGER,
            proof_file_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS wallet_ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity TEXT NOT NULL,
            customer_data TEXT NOT NULL DEFAULT '{}',
            order_uuid TEXT NOT NULL UNIQUE,
            gunes_order_id TEXT,
            price TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            response TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('store_name','سوريا ستور')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('markup_percent',?)", (str(MARKUP_PERCENT),))

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def money(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return Decimal("0")

def fmt(value):
    return f"{money(value):f}".rstrip("0").rstrip(".") or "0"

def get_setting(key, default=""):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def ensure_user(tg_user):
    uid = int(tg_user["id"])
    with db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,created_at)
                     VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name""",
                  (uid, tg_user.get("username",""), tg_user.get("first_name",""), now()))

def user_balance(uid):
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        return money(row["balance"]) if row else Decimal("0")

def set_state(uid, state, data=None):
    with db() as c:
        c.execute("""INSERT INTO states(user_id,state,data,updated_at) VALUES(?,?,?,?)
                     ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=excluded.updated_at""",
                  (uid, state, json.dumps(data or {}, ensure_ascii=False), now()))

def get_state(uid):
    with db() as c:
        row = c.execute("SELECT state,data FROM states WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return None, {}
        try:
            return row["state"], json.loads(row["data"])
        except Exception:
            return row["state"], {}

def clear_state(uid):
    with db() as c:
        c.execute("DELETE FROM states WHERE user_id=?", (uid,))

# ------------------------- HTTP / TELEGRAM -------------------------

def http_json(url, headers=None, data=None, method=None, timeout=35):
    h = {"Accept": "application/json", "User-Agent": "SyriaStoreBot/2.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log.error("HTTP %s %s", e.code, body[:1200])
        raise RuntimeError(f"HTTP {e.code}: {body[:250]}")
    except urllib.error.URLError as e:
        log.error("Network error: %s", e.reason)
        raise RuntimeError("تعذر الاتصال بالخادم")
    except json.JSONDecodeError:
        raise RuntimeError("رد غير صالح من الخادم")

def tg(method, data):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    # Telegram accepts JSON for all methods and file_id photo forwarding.
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    result = http_json(url, {"Content-Type":"application/json"}, payload, method="POST")
    if not result.get("ok"):
        log.error("Telegram %s error: %s", method, result)
    return result

def send(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": str(text), "disable_web_page_preview": True}
    if keyboard:
        data["reply_markup"] = keyboard
    return tg("sendMessage", data)

def send_photo(chat_id, file_id, caption, keyboard=None):
    data = {"chat_id": chat_id, "photo": file_id, "caption": caption[:1024]}
    if keyboard:
        data["reply_markup"] = keyboard
    return tg("sendPhoto", data)

def edit_message(chat_id, message_id, text, keyboard=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if keyboard:
        data["reply_markup"] = keyboard
    return tg("editMessageText", data)

def answer_cb(cb_id, text=""):
    try:
        tg("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:180]})
    except Exception:
        log.exception("answer callback failed")

# ------------------------- GUNES -------------------------

def gunes_get(path, params=None):
    if not GUNES_TOKEN:
        raise RuntimeError("GUNES_API_TOKEN غير مضبوط في Render")
    url = SHOP_API + path.lstrip("/")
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    return http_json(url, {"api-token": GUNES_TOKEN})

def get_products():
    result = gunes_get("client/api/products")
    if isinstance(result, list):
        products = result
    elif isinstance(result, dict):
        products = next((result[k] for k in ("data","products","result") if isinstance(result.get(k), list)), None)
        if products is None:
            raise RuntimeError("شكل قائمة المنتجات غير متوقع")
    else:
        raise RuntimeError("رد المنتجات غير صالح")
    return [p for p in products if isinstance(p, dict)]

def available(p):
    v = p.get("available")
    return v is True or str(v).lower() in ("1","true","yes")

def product_image(p):
    for key in ("image_url","image","photo_url","photo","icon_url","icon","logo_url","logo","thumbnail_url","thumbnail","picture_url","picture","category_img"):
        v = p.get(key)
        if isinstance(v, dict):
            v = v.get("url") or v.get("src")
        if isinstance(v, str) and v.strip():
            if v.startswith(("https://","http://")):
                return v
            if key == "category_img":
                return urllib.parse.urljoin(SHOP_API, v.lstrip("/"))
    return None

def sale_price(p):
    base = money(p.get("price", 0))
    markup = money(get_setting("markup_percent", str(MARKUP_PERCENT)))
    return (base * (Decimal("1") + markup / Decimal("100"))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

def product_by_id(pid):
    return next((p for p in get_products() if str(p.get("id")) == str(pid)), None)

def product_qty_valid(p, qty):
    qv = p.get("qty_values")
    if qv is None:
        return qty == 1
    if isinstance(qv, list):
        return qty in [int(x) for x in qv if str(x).isdigit()]
    if isinstance(qv, dict):
        try:
            return int(qv.get("min", 1)) <= qty <= int(qv.get("max", 999999999))
        except Exception:
            return False
    return qty == 1

def create_gunes_order(product, qty, values, order_uuid):
    # Gunes docs: GET /client/api/newOrder/{product_id}/params
    # qty and order_uuid are required. First customer field maps to playerId.
    params = {"qty": str(qty), "order_uuid": order_uuid}
    if values:
        params["playerId"] = values[0]
        # Extra product parameters are sent as param2, param3, etc.
        # If Gunes provides exact parameter keys, these can be mapped here.
        for i, value in enumerate(values[1:], start=2):
            params[f"param{i}"] = value
    path = f"client/api/newOrder/{urllib.parse.quote(str(product['id']))}/params"
    return gunes_get(path, params)

# ------------------------- MENUS -------------------------

def main_menu(uid):
    rows = [
        [{"text":"🎮 الألعاب"},{"text":"📱 التطبيقات"}],
        [{"text":"📦 جميع المنتجات"},{"text":"💰 رصيدي"}],
        [{"text":"➕ شحن رصيد"},{"text":"🧾 طلباتي"}],
        [{"text":"📞 خدمة العملاء"}]
    ]
    if uid == ADMIN_ID:
        rows.append([{"text":"🛠 لوحة الإدارة"}])
    return {"keyboard": rows, "resize_keyboard": True}

def inline(rows):
    return {"inline_keyboard": rows}

def home(uid):
    send(uid, f"🌟 أهلاً بك في {get_setting('store_name','سوريا ستور')}\\nاختر الخدمة التي تريدها:", main_menu(uid))

def admin_only(uid):
    return int(uid) == ADMIN_ID

# ------------------------- PRODUCTS -------------------------

def filtered_products(category):
    products = [p for p in get_products() if available(p)]
    if category == "game":
        products = [p for p in products if "game" in str(p.get("category_name","")).lower() or "game" in str(p.get("name","")).lower()]
    elif category == "app":
        products = [p for p in products if "app" in str(p.get("category_name","")).lower() or "app" in str(p.get("name","")).lower()]
    return products

def show_products(uid, category="all", page=0):
    try:
        products = filtered_products(category)
        if not products:
            send(uid, "لا توجد منتجات متاحة حالياً.")
            return
        pages = max(1, (len(products)+PAGE_SIZE-1)//PAGE_SIZE)
        page = max(0, min(int(page), pages-1))
        chunk = products[page*PAGE_SIZE:(page+1)*PAGE_SIZE]
        rows = []
        for p in chunk:
            rows.append([{"text":f"{str(p.get('name','منتج'))[:35]} - {fmt(sale_price(p))}$",
                          "callback_data":f"product:{p.get('id')}"}])
        nav = []
        if page: nav.append({"text":"⬅️ السابق","callback_data":f"page:{category}:{page-1}"})
        if page < pages-1: nav.append({"text":"التالي ➡️","callback_data":f"page:{category}:{page+1}"})
        if nav: rows.append(nav)
        label = {"all":"جميع المنتجات","game":"الألعاب","app":"التطبيقات"}.get(category,category)
        send(uid, f"🛍️ سوريا ستور\\n📂 {label}\\n📦 المنتجات: {len(products)}\\n📄 الصفحة {page+1}/{pages}\\n\\nاختر المنتج:", inline(rows))
    except Exception:
        log.exception("show products failed")
        send(uid, "تعذر تحميل المنتجات حالياً، حاول بعد قليل.")

def show_product(uid, pid):
    try:
        p = product_by_id(pid)
        if not p:
            send(uid, "المنتج غير موجود حالياً.")
            return
        price = sale_price(p)
        params = p.get("params") or []
        qty = p.get("qty_values")
        lines = [f"📦 {p.get('name','منتج')}", f"💵 سعر الوحدة: {fmt(price)} USD",
                 f"🆔 رقم المنتج: {pid}", f"📦 النوع: {p.get('product_type','غير محدد')}"]
        if qty is None:
            lines.append("🔢 الكمية: 1")
        elif isinstance(qty, list):
            lines.append("🔢 الكميات المتاحة: " + ", ".join(map(str,qty)))
        elif isinstance(qty, dict):
            lines.append(f"🔢 الكمية من {qty.get('min')} إلى {qty.get('max')}")
        if params:
            lines.append("📝 البيانات المطلوبة: " + "، ".join(map(str,params)))
        caption = "\\n".join(lines)
        rows = [[{"text":"🛒 شراء المنتج","callback_data":f"buy:{pid}"}],
                [{"text":"⬅️ رجوع للمنتجات","callback_data":"back:all:0"}]]
        img = product_image(p)
        if img:
            try:
                # URL may be relative to the API host; Telegram fetches the URL.
                tg("sendPhoto", {"chat_id":uid,"photo":img,"caption":caption[:1024],"reply_markup":inline(rows)})
                return
            except Exception:
                log.warning("image send failed for product %s", pid)
        send(uid, caption, inline(rows))
    except Exception:
        log.exception("show product failed")
        send(uid, "تعذر عرض تفاصيل المنتج.")

def begin_purchase(uid, pid):
    try:
        p = product_by_id(pid)
        if not p or not available(p):
            send(uid, "عذراً، المنتج غير متاح حالياً.")
            return
        params = p.get("params") or []
        qty_values = p.get("qty_values")
        state_data = {"product_id":str(pid),"step":"qty","values":[]}
        if qty_values is None:
            state_data["qty"] = 1
            if params:
                state_data["step"] = "param"
                state_data["param_index"] = 0
                set_state(uid, "purchase", state_data)
                send(uid, f"📝 {params[0]}\\nأرسل البيانات المطلوبة:")
            else:
                confirm_purchase(uid, p, 1, [])
            return
        set_state(uid, "purchase", state_data)
        if isinstance(qty_values, list):
            send(uid, "اختر الكمية المطلوبة:", inline([[{"text":str(x),"callback_data":f"qty:{pid}:{x}"}] for x in qty_values[:30]]))
        else:
            send(uid, f"أرسل الكمية المطلوبة (من {qty_values.get('min')} إلى {qty_values.get('max')}):")
    except Exception:
        log.exception("begin purchase failed")
        send(uid, "تعذر بدء الشراء.")

def ask_next_param(uid, p, data):
    params = p.get("params") or []
    idx = int(data.get("param_index",0))
    if idx < len(params):
        set_state(uid, "purchase", data)
        send(uid, f"📝 {params[idx]}\\nأرسل البيانات المطلوبة:")
    else:
        confirm_purchase(uid, p, int(data.get("qty",1)), data.get("values",[]))

def confirm_purchase(uid, p, qty, values):
    total = (sale_price(p) * Decimal(qty)).quantize(Decimal("0.0001"))
    if user_balance(uid) < total:
        send(uid, f"رصيدك غير كافٍ.\\n💰 رصيدك: {fmt(user_balance(uid))} USD\\n🧾 المطلوب: {fmt(total)} USD\\nاشحن رصيدك أولاً.", inline([[{"text":"➕ شحن رصيد","callback_data":"topup:start"}]]))
        clear_state(uid)
        return
    data = {"product_id":str(p["id"]),"qty":qty,"values":values,"total":str(total)}
    set_state(uid, "purchase_confirm", data)
    summary = f"تأكيد الطلب؟\\n📦 {p.get('name')}\\n🔢 الكمية: {qty}\\n💵 الإجمالي: {fmt(total)} USD"
    if values:
        summary += "\\n📝 البيانات: " + " | ".join(values)
    send(uid, summary, inline([[{"text":"✅ تأكيد الشراء","callback_data":"purchase:confirm"},{"text":"❌ إلغاء","callback_data":"purchase:cancel"}]]))

def execute_purchase(uid):
    state, data = get_state(uid)
    if state != "purchase_confirm":
        send(uid, "انتهت جلسة الشراء. أعد المحاولة.")
        return
    pid, qty = data["product_id"], int(data["qty"])
    total = money(data["total"])
    order_uuid = str(uuid.uuid4())
    p = product_by_id(pid)
    if not p or not available(p):
        clear_state(uid); send(uid, "المنتج لم يعد متاحاً."); return
    # Reserve/debit atomically before contacting supplier. Refund only on explicit reject.
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        bal = money(row["balance"]) if row else Decimal("0")
        if bal < total:
            clear_state(uid); send(uid, "رصيدك غير كافٍ."); return
        newbal = bal-total
        c.execute("UPDATE users SET balance=? WHERE user_id=?", (str(newbal),uid))
        c.execute("INSERT INTO wallet_ledger(user_id,amount,kind,reference,note,created_at) VALUES(?,?,?,?,?,?)",
                  (uid,str(-total),"purchase_reserve",order_uuid,str(p.get("name")),now()))
        c.execute("""INSERT INTO orders(user_id,product_id,product_name,quantity,customer_data,order_uuid,price,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (uid,pid,str(p.get("name")),str(qty),json.dumps(data.get("values",[]),ensure_ascii=False),order_uuid,str(total),"processing",now(),now()))
    clear_state(uid)
    send(uid, f"⏳ تم استلام طلبك وجاري تنفيذه.\\nرقم المتابعة: {order_uuid}\\nتم حجز {fmt(total)} USD من رصيدك.")
    try:
        response = create_gunes_order(p, qty, data.get("values",[]), order_uuid)
        payload = response.get("data", response) if isinstance(response,dict) else {}
        status = str(payload.get("status", response.get("status","wait") if isinstance(response,dict) else "wait")).lower()
        gunes_id = payload.get("order_id")
        with db() as c:
            c.execute("UPDATE orders SET gunes_order_id=?,status=?,response=?,updated_at=? WHERE order_uuid=?",
                      (gunes_id,status,json.dumps(response,ensure_ascii=False)[:6000],now(),order_uuid))
        if status == "accept":
            send(uid, f"✅ تم تنفيذ طلبك بنجاح!\\n📦 {p.get('name')}\\n🆔 رقم الطلب: {gunes_id or order_uuid}")
        elif status == "reject":
            refund_order(order_uuid, "رفض مزود الخدمة")
            send(uid, "❌ تعذر تنفيذ الطلب وتمت إعادة المبلغ إلى رصيدك.")
        else:
            send(uid, "🕒 طلبك قيد المعالجة لدى مزود الخدمة. سيبقى المبلغ محجوزاً حتى تتأكد النتيجة.")
        if admin_only(uid):
            send(ADMIN_ID, f"طلب جديد من المدير نفسه: {order_uuid} - {status}")
        else:
            send(ADMIN_ID, f"🛒 طلب شراء جديد\\nالزبون: {uid}\\nالمنتج: {p.get('name')}\\nالحالة: {status}\\nرقم Gunes: {gunes_id or 'قيد الانتظار'}")
    except Exception as e:
        log.exception("Gunes order call failed; order left pending for reconciliation")
        with db() as c:
            c.execute("UPDATE orders SET status='unknown',response=?,updated_at=? WHERE order_uuid=?",
                      (str(e)[:1500],now(),order_uuid))
        send(uid, "⚠️ لم يصلنا تأكيد نهائي من مزود الخدمة. تم إبقاء الطلب قيد المراجعة ولم نعد المبلغ تلقائياً حتى لا يتكرر الطلب أو الخصم. تواصل مع الإدارة برقم الطلب: " + order_uuid)
        send(ADMIN_ID, f"⚠️ طلب يحتاج مراجعة\\nالزبون: {uid}\\nUUID: {order_uuid}\\nراجع حالة الطلب من Gunes قبل أي استرداد.")

def refund_order(order_uuid, note):
    with db() as c:
        row = c.execute("SELECT user_id,price,status FROM orders WHERE order_uuid=?", (order_uuid,)).fetchone()
        if not row or row["status"] == "refunded":
            return False
        uid = row["user_id"]
        amount = money(row["price"])
        c.execute("UPDATE users SET balance=CAST(balance AS REAL)+? WHERE user_id=?", (float(amount),uid))
        c.execute("UPDATE orders SET status='refunded',updated_at=? WHERE order_uuid=?", (now(),order_uuid))
        c.execute("INSERT INTO wallet_ledger(user_id,amount,kind,reference,note,created_at) VALUES(?,?,?,?,?,?)",
                  (uid,str(amount),"refund",order_uuid,note,now()))
    return True

# ------------------------- TOPUPS / PAYMENTS -------------------------

def payment_methods():
    with db() as c:
        return c.execute("SELECT * FROM payment_methods WHERE enabled=1 ORDER BY id").fetchall()

def start_topup(uid):
    methods = payment_methods()
    if not methods:
        send(uid, "طرق الدفع لم تُضبط بعد. تواصل مع الإدارة.")
        return
    rows = [[{"text":m["name"],"callback_data":f"paymethod:{m['id']}"}] for m in methods]
    send(uid, "➕ شحن رصيد\\nاختر طريقة الدفع:", inline(rows))

def topup_amount(uid, method_id):
    with db() as c:
        m = c.execute("SELECT * FROM payment_methods WHERE id=? AND enabled=1",(method_id,)).fetchone()
    if not m:
        send(uid, "طريقة الدفع غير متاحة."); return
    set_state(uid,"topup_amount",{"method_id":method_id})
    send(uid, f"طريقة الدفع: {m['name']}\\n\\n{m['instructions']}\\n\\nأرسل المبلغ بالدولار الذي تريد شحنه:")

def create_topup(uid, amount):
    state, data = get_state(uid)
    if state != "topup_amount":
        send(uid,"ابدأ طلب الشحن من جديد."); return
    amt = money(amount)
    if amt <= 0 or amt > Decimal("10000"):
        send(uid,"أرسل مبلغاً صحيحاً أكبر من صفر ولا يتجاوز 10000 دولار."); return
    data["amount"] = str(amt)
    set_state(uid,"topup_proof",data)
    send(uid, f"المبلغ: {fmt(amt)} USD\\nالآن أرسل صورة إثبات الدفع كصورة (Photo).")

def save_topup_proof(uid, photo_file_id):
    state, data = get_state(uid)
    if state != "topup_proof":
        send(uid,"ابدأ طلب الشحن من زر شحن الرصيد."); return
    with db() as c:
        cur = c.execute("""INSERT INTO topups(user_id,amount,method_id,proof_file_id,status,created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (uid,data["amount"],data["method_id"],photo_file_id,"pending",now()))
        tid = cur.lastrowid
        m = c.execute("SELECT name FROM payment_methods WHERE id=?",(data["method_id"],)).fetchone()
    clear_state(uid)
    send(uid, f"✅ وصل طلب الشحن رقم #{tid}. سيتم مراجعته من الإدارة.")
    user = None
    with db() as c:
        user = c.execute("SELECT username,first_name FROM users WHERE user_id=?",(uid,)).fetchone()
    uname = ("@"+user["username"]) if user and user["username"] else (user["first_name"] if user else str(uid))
    caption = f"💳 طلب شحن جديد #{tid}\\nالزبون: {uname}\\nID: {uid}\\nالمبلغ: {fmt(data['amount'])} USD\\nالطريقة: {m['name'] if m else 'غير معروفة'}"
    keyboard = inline([[{"text":"✅ موافقة وإضافة الرصيد","callback_data":f"topup:approve:{tid}"},
                        {"text":"❌ رفض","callback_data":f"topup:reject:{tid}"}]])
    if photo_file_id:
        send_photo(ADMIN_ID,photo_file_id,caption,keyboard)
    else:
        send(ADMIN_ID,caption,keyboard)

def review_topup(admin_id, tid, approve):
    if not admin_only(admin_id):
        return
    with db() as c:
        row = c.execute("SELECT * FROM topups WHERE id=?",(tid,)).fetchone()
        if not row:
            send(admin_id,"طلب الشحن غير موجود."); return
        if row["status"] != "pending":
            send(admin_id,"تمت مراجعة هذا الطلب مسبقاً."); return
        status = "approved" if approve else "rejected"
        c.execute("UPDATE topups SET status=?,reviewed_at=?,reviewed_by=? WHERE id=? AND status='pending'",
                  (status,now(),admin_id,tid))
        if approve:
            amt = money(row["amount"])
            c.execute("UPDATE users SET balance=CAST(balance AS REAL)+? WHERE user_id=?",(float(amt),row["user_id"]))
            c.execute("INSERT INTO wallet_ledger(user_id,amount,kind,reference,note,created_at) VALUES(?,?,?,?,?,?)",
                      (row["user_id"],str(amt),"topup",f"topup:{tid}","موافقة الإدارة على الشحن",now()))
    send(row["user_id"], "✅ تمت الموافقة على طلب الشحن وإضافة المبلغ إلى رصيدك." if approve else "❌ تم رفض طلب الشحن. إذا كنت تعتقد أن هناك خطأ تواصل مع الإدارة.")
    send(admin_id, f"تمت {'الموافقة' if approve else 'رفض'} طلب الشحن #{tid}.")

# ------------------------- ADMIN -------------------------

def admin_panel(uid):
    if not admin_only(uid): return
    send(uid, "🛠 لوحة إدارة سوريا ستور", inline([
        [{"text":"💳 طرق الدفع","callback_data":"admin:payments"},{"text":"📊 الإحصائيات","callback_data":"admin:stats"}],
        [{"text":"➕ إضافة طريقة دفع","callback_data":"admin:addpayment"}],
        [{"text":"💰 تعديل رصيد زبون","callback_data":"admin:balance"}],
        [{"text":"📈 نسبة الربح","callback_data":"admin:markup"}],
        [{"text":"📋 الطلبات المعلقة","callback_data":"admin:pending"}]
    ]))
    send(uid, "أوامر الإدارة:\\n/setpayment الاسم | تعليمات الدفع\\n/disablepayment رقم الطريقة\\n/addbalance رقم_الزبون المبلغ\\n/setmarkup نسبة_الربح\\n\\nمثال:\\n/setpayment شام كاش | حوّل إلى رقم ... ثم أرسل صورة الإيصال")

def admin_stats(uid):
    with db() as c:
        users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM topups WHERE status='pending'").fetchone()["n"]
        orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    send(uid,f"📊 إحصائيات المتجر\\n👥 الزبائن: {users}\\n💳 طلبات شحن معلقة: {pending}\\n🛒 الطلبات: {orders}\\n📈 نسبة الربح: {get_setting('markup_percent','10')}%")

def admin_payments(uid):
    rows = payment_methods()
    if not rows:
        send(uid,"لا توجد طرق دفع. أضف طريقة باستخدام /setpayment الاسم | التعليمات")
        return
    out = ["💳 طرق الدفع الحالية:"]
    for m in rows:
        out.append(f"#{m['id']} — {m['name']}\\n{m['instructions']}")
    send(uid,"\\n\\n".join(out))

def admin_pending(uid):
    with db() as c:
        rows = c.execute("SELECT id,user_id,amount,created_at FROM topups WHERE status='pending' ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        send(uid,"لا توجد طلبات شحن معلقة."); return
    for r in rows:
        send(uid,f"طلب شحن #{r['id']}\\nالزبون: {r['user_id']}\\nالمبلغ: {fmt(r['amount'])} USD",
             inline([[{"text":"✅ موافقة","callback_data":f"topup:approve:{r['id']}"},
                      {"text":"❌ رفض","callback_data":f"topup:reject:{r['id']}"}]]))

def user_orders(uid):
    with db() as c:
        rows = c.execute("SELECT product_name,quantity,price,status,created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
    if not rows:
        send(uid,"ليس لديك طلبات حتى الآن."); return
    lines=["🧾 آخر طلباتك:"]
    for r in rows:
        lines.append(f"• {r['product_name']} × {r['quantity']} — {fmt(r['price'])}$ — {r['status']}")
    send(uid,"\\n".join(lines))

def wallet_history(uid):
    with db() as c:
        rows = c.execute("SELECT amount,kind,note,created_at FROM wallet_ledger WHERE user_id=? ORDER BY id DESC LIMIT 10",(uid,)).fetchall()
    lines=["📒 آخر حركات الرصيد:"]
    for r in rows:
        lines.append(f"• {r['kind']}: {fmt(r['amount'])} USD — {r['note'] or ''}")
    send(uid,"\\n".join(lines) if len(lines)>1 else "لا توجد حركات رصيد.")

# ------------------------- MESSAGE HANDLER -------------------------

def handle_message(message):
    chat = message.get("chat",{})
    sender = message.get("from",{})
    uid = int(chat.get("id",0))
    if not uid or not sender:
        return
    ensure_user(sender)
    if message.get("photo"):
        photo_id = message["photo"][-1]["file_id"]
        save_topup_proof(uid,photo_id)
        return
    text = (message.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/start"):
        clear_state(uid); home(uid); return
    if text.startswith("/cancel"):
        clear_state(uid); send(uid,"تم إلغاء العملية.",main_menu(uid)); return
    if text.startswith("/setpayment") and admin_only(uid):
        payload = text[len("/setpayment"):].strip()
        if "|" not in payload:
            send(uid,"الصيغة: /setpayment الاسم | تعليمات الدفع"); return
        name, instructions = [x.strip() for x in payload.split("|",1)]
        if not name or not instructions:
            send(uid,"اكتب اسم الطريقة وتعليماتها."); return
        with db() as c:
            c.execute("INSERT INTO payment_methods(name,instructions,enabled) VALUES(?,?,1)",(name,instructions))
        send(uid,f"✅ تمت إضافة طريقة الدفع: {name}"); return
    if text.startswith("/disablepayment") and admin_only(uid):
        try: mid=int(text.split()[1])
        except Exception: send(uid,"الصيغة: /disablepayment رقم الطريقة"); return
        with db() as c: c.execute("UPDATE payment_methods SET enabled=0 WHERE id=?",(mid,))
        send(uid,"تم تعطيل طريقة الدفع."); return
    if text.startswith("/addbalance") and admin_only(uid):
        parts=text.split()
        if len(parts)!=3:
            send(uid,"الصيغة: /addbalance رقم_الزبون المبلغ"); return
        try:
            target=int(parts[1]); amt=money(parts[2])
            if amt<=0: raise ValueError()
            with db() as c:
                c.execute("INSERT OR IGNORE INTO users(user_id,created_at) VALUES(?,?)",(target,now()))
                c.execute("UPDATE users SET balance=CAST(balance AS REAL)+? WHERE user_id=?",(float(amt),target))
                c.execute("INSERT INTO wallet_ledger(user_id,amount,kind,reference,note,created_at) VALUES(?,?,?,?,?,?)",
                          (target,str(amt),"admin_credit",None,"إضافة يدوية من الإدارة",now()))
            send(uid,"تمت إضافة الرصيد."); send(target,f"💰 أضافت الإدارة {fmt(amt)} USD إلى رصيدك.")
        except Exception: send(uid,"تعذر إضافة الرصيد. تحقق من الرقم والمبلغ.")
        return
    if text.startswith("/setmarkup") and admin_only(uid):
        try:
            pct=money(text.split()[1])
            if pct<0 or pct>300: raise ValueError()
            set_setting("markup_percent",str(pct)); send(uid,f"تم ضبط نسبة الربح إلى {fmt(pct)}%.")
        except Exception: send(uid,"الصيغة: /setmarkup 10")
        return

    state, data = get_state(uid)
    if state == "topup_amount":
        create_topup(uid,text); return
    if state == "purchase":
        p=product_by_id(data.get("product_id"))
        if not p: clear_state(uid); send(uid,"المنتج لم يعد متاحاً."); return
        if data.get("step")=="qty":
            try: qty=int(text)
            except Exception: send(uid,"أرسل كمية رقمية صحيحة."); return
            if not product_qty_valid(p,qty):
                send(uid,"الكمية غير مسموحة لهذا المنتج. تحقق من الكميات المتاحة."); return
            data["qty"]=qty
            if p.get("params"):
                data["step"]="param"; data["param_index"]=0; ask_next_param(uid,p,data)
            else:
                confirm_purchase(uid,p,qty,[])
            return
        if data.get("step")=="param":
            values=data.get("values",[])
            values.append(text[:250])
            data["values"]=values
            data["param_index"]=int(data.get("param_index",0))+1
            ask_next_param(uid,p,data)
            return
    if text == "🎮 الألعاب": show_products(uid,"game",0)
    elif text == "📱 التطبيقات": show_products(uid,"app",0)
    elif text in ("📦 جميع المنتجات","📦 منتجات الشحن"): show_products(uid,"all",0)
    elif text == "💰 رصيدي":
        send(uid,f"💰 رصيدك في سوريا ستور: {fmt(user_balance(uid))} USD")
        wallet_history(uid)
    elif text == "➕ شحن رصيد": start_topup(uid)
    elif text == "🧾 طلباتي": user_orders(uid)
    elif text == "📞 خدمة العملاء": send(uid,SUPPORT_TEXT)
    elif text == "🛠 لوحة الإدارة" and admin_only(uid): admin_panel(uid)
    else: send(uid,"اختر خدمة من القائمة أو أرسل /start.",main_menu(uid))

# ------------------------- CALLBACK HANDLER -------------------------

def handle_callback(cb):
    cbid=cb.get("id"); data=cb.get("data","")
    msg=cb.get("message",{}); uid=int(msg.get("chat",{}).get("id",0))
    if not cbid or not uid: return
    try:
        parts=data.split(":")
        if parts[0]=="page" and len(parts)==3:
            answer_cb(cbid); show_products(uid,parts[1],int(parts[2]))
        elif parts[0]=="back":
            answer_cb(cbid); show_products(uid,parts[1],int(parts[2]))
        elif parts[0]=="product" and len(parts)==2:
            answer_cb(cbid); show_product(uid,parts[1])
        elif parts[0]=="buy" and len(parts)==2:
            answer_cb(cbid); begin_purchase(uid,parts[1])
        elif parts[0]=="qty" and len(parts)==3:
            state, st=get_state(uid)
            if state!="purchase" or st.get("product_id")!=parts[1]:
                answer_cb(cbid,"انتهت جلسة الشراء."); return
            p=product_by_id(parts[1]); qty=int(parts[2])
            if not p or not product_qty_valid(p,qty):
                answer_cb(cbid,"كمية غير صالحة."); return
            st["qty"]=qty
            if p.get("params"):
                st["step"]="param"; st["param_index"]=0; ask_next_param(uid,p,st)
            else: confirm_purchase(uid,p,qty,[])
            answer_cb(cbid)
        elif parts[0]=="purchase" and parts[1]=="confirm":
            answer_cb(cbid,"جاري تنفيذ الطلب"); execute_purchase(uid)
        elif parts[0]=="purchase" and parts[1]=="cancel":
            clear_state(uid); answer_cb(cbid,"تم الإلغاء"); send(uid,"تم إلغاء الشراء.")
        elif parts[0]=="topup" and parts[1]=="start":
            answer_cb(cbid); start_topup(uid)
        elif parts[0]=="paymethod" and len(parts)==2:
            answer_cb(cbid); topup_amount(uid,int(parts[1]))
        elif parts[0]=="topup" and len(parts)==3 and parts[1] in ("approve","reject"):
            if not admin_only(uid): answer_cb(cbid,"غير مصرح"); return
            review_topup(uid,int(parts[2]),parts[1]=="approve"); answer_cb(cbid,"تمت المراجعة")
        elif parts[0]=="admin":
            if not admin_only(uid): answer_cb(cbid,"غير مصرح"); return
            answer_cb(cbid)
            if parts[1]=="payments": admin_payments(uid)
            elif parts[1]=="stats": admin_stats(uid)
            elif parts[1]=="pending": admin_pending(uid)
            elif parts[1]=="addpayment": send(uid,"أرسل: /setpayment الاسم | تعليمات الدفع")
            elif parts[1]=="balance": send(uid,"أرسل: /addbalance رقم_الزبون المبلغ")
            elif parts[1]=="markup": send(uid,"أرسل: /setmarkup 10 لضبط نسبة الربح")
        else:
            answer_cb(cbid,"طلب غير معروف")
    except Exception:
        log.exception("callback failed")
        answer_cb(cbid,"حدث خطأ، حاول مرة أخرى.")

# ------------------------- WEBHOOK / START -------------------------

@app.get("/")
def health():
    return "Syria Store Bot is running", 200

@app.post("/webhook")
def webhook():
    if WEBHOOK_SECRET:
        supplied=request.headers.get("X-Telegram-Bot-Api-Secret-Token","")
        if supplied != WEBHOOK_SECRET:
            return jsonify({"ok":False,"error":"unauthorized"}), 403
    update=request.get_json(silent=True) or {}
    try:
        if update.get("message"): handle_message(update["message"])
        elif update.get("callback_query"): handle_callback(update["callback_query"])
    except Exception:
        log.exception("webhook update failed")
    return jsonify({"ok":True})


# Gunicorn imports this module instead of executing it as __main__.
# Initialize storage and register the webhook during import as well.
try:
    init_db()
    if BOT_TOKEN and RENDER_URL:
        hook_data = {
            "url": RENDER_URL.rstrip("/") + "/webhook",
            "allowed_updates": json.dumps(["message", "callback_query"])
        }
        if WEBHOOK_SECRET:
            hook_data["secret_token"] = WEBHOOK_SECRET
        hook_result = tg("setWebhook", hook_data)
        log.info("Webhook registration during app startup: %s", hook_result)
except Exception:
    log.exception("Application startup initialization failed")


def main():
    if not BOT_TOKEN: raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not GUNES_TOKEN: raise RuntimeError("Missing GUNES_API_TOKEN")
    if not RENDER_URL: raise RuntimeError("Missing RENDER_EXTERNAL_URL")
    init_db()
    webhook_url=RENDER_URL.rstrip("/")+"/webhook"
    data={"url":webhook_url,"allowed_updates":json.dumps(["message","callback_query"])}
    if WEBHOOK_SECRET: data["secret_token"]=WEBHOOK_SECRET
    result=tg("setWebhook",data)
    log.info("Webhook setup: %s",result)
    try:
        profile=gunes_get("client/api/profile")
        log.info("Gunes connection successful: %s", isinstance(profile,dict))
        log.info("Gunes products available: %s",len(get_products()))
    except Exception:
        log.exception("Gunes startup test failed")
    port=int(os.getenv("PORT","10000"))
    app.run(host="0.0.0.0",port=port)

if __name__=="__main__":
    main()
