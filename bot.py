import os
import hmac
import hashlib
import random
from datetime import datetime, timedelta

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify
from groq import Groq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========================= CONFIG =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = "6415641863"

app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

# ========================= HTTP & DB (same) =========================
def get_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = get_session()
db_pool = None

def init_pool():
    global db_pool
    if db_pool: return
    db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL, cursor_factory=RealDictCursor)
    print("✅ Database pool initialized")

def get_db():
    if not db_pool: init_pool()
    return db_pool.getconn()

def release_db(conn):
    if db_pool and conn: db_pool.putconn(conn)

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                plan TEXT DEFAULT 'free',
                expires DATE,
                activated_at TIMESTAMP,
                usage_date DATE,
                usage_count INTEGER DEFAULT 0
            )""")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= USER MANAGEMENT (same) =========================
def activate_pro(user_id):
    expires = (datetime.utcnow() + timedelta(days=30)).date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, plan, expires, activated_at)
                VALUES (%s, 'pro', %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET plan='pro', expires=EXCLUDED.expires, activated_at=EXCLUDED.activated_at
            """, (user_id, expires, datetime.utcnow()))
        conn.commit()
        return expires.strftime("%Y-%m-%d")
    finally:
        release_db(conn)

def is_pro(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT plan, expires FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        if not row or row["plan"] != "pro" or not row["expires"]:
            return False
        return row["expires"] >= datetime.utcnow().date()
    finally:
        release_db(conn)

def get_pro_expiry(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT expires FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        return row["expires"].strftime("%Y-%m-%d") if row and row["expires"] else None
    finally:
        release_db(conn)

def check_and_increment_free_usage(user_id):
    if is_pro(user_id): return True
    today = datetime.utcnow().date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usage_date, usage_count FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        current = row["usage_count"] if row and row["usage_date"] == today else 0
        if current >= FREE_LIMIT: return False
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, usage_date, usage_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (user_id) DO UPDATE
                SET usage_date=EXCLUDED.usage_date,
                    usage_count=CASE WHEN users.usage_date=EXCLUDED.usage_date THEN users.usage_count + 1 ELSE 1 END
            """, (user_id, today))
        conn.commit()
        return True
    finally:
        release_db(conn)

def free_uses_remaining(user_id):
    today = datetime.utcnow().date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usage_date, usage_count FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        if not row or row["usage_date"] != today:
            return FREE_LIMIT
        return max(0, FREE_LIMIT - row["usage_count"])
    finally:
        release_db(conn)

# ========================= IMPROVED AI PROMPTS =========================
SYSTEM_PROMPT = """
You are TikGenius. You write TikTok content for a young Nigerian guy who films himself talking directly to the camera.

**Very Important Style:**
- Start naturally like real spoken English.
- Do NOT force "Omo", "Abeg", "Sha", "Ehn" at the beginning of every hook.
- Use light Pidgin naturally only when it fits the flow.
- Sound like a real person speaking casually to camera.
- Keep hooks short (8-15 words max).
- Make them personal and relatable.
"""

PROMPTS = {
    "hooks": """Topic: {topic}

Write 12 short, natural TikTok hooks for me speaking to camera about "{topic}".

Rules:
- Start naturally (no forced Pidgin at the beginning)
- Maximum 15 words
- Conversational spoken style
- Light Pidgin only where it sounds natural
- Has small emotion or twist

Number 1-12. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 12 natural captions for my face video about "{topic}".""",

    "pov": """Topic: {topic}

Write 8 POV ideas starting with "POV:".""",

    "hashtags": """Topic: {topic}

Give 5 sets of 6 relevant hashtags.""",

    "bio": """Topic: {topic}

Write 6 good TikTok bios.""",
}

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic):
    prompt = PROMPTS.get(mode, f"Topic: {topic}").format(topic=topic)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=1000
        )
        raw = response.choices[0].message.content.strip()
        
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        cleaned = []
        for line in lines:
            if any(x in line.lower() for x in ["here are", "sure!", "as a"]):
                continue
            cleaned.append(line)
        return "\n".join(cleaned)
    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ Try again."

# ========================= HELPERS =========================
def send_message(chat_id, text):
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                         json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

def send_typing(chat_id):
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except: pass

def create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {"telegram_id": user_id, "username": username or "", "plan": "pro"}
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    try:
        res = http_session.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=20).json()
        return res["data"]["authorization_url"] if res.get("status") else None
    except: return None

# ========================= ROUTES =========================
@app.route("/", methods=["GET"])
def home():
    return "TikGenius running ✅", 200

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    username = message.get("from", {}).get("username", "")
    first_name = message.get("from", {}).get("first_name", "Bro")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]
    topic = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        send_message(chat_id, f"""🔥 Welcome {first_name}!

/hooks [topic] — Short opening lines for your videos
/captions [topic] — Full captions
/stats — Total users (admin)

/plan
/upgrade""")

    elif command == "/stats":
        if str(user_id) != ADMIN_ID:
            send_message(chat_id, "❌ Admin only.")
            return jsonify({"ok": True})
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) as total FROM users")
                total = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) as pro FROM users WHERE plan='pro'")
                pro = cur.fetchone()["pro"]
            send_message(chat_id, f"""📊 Stats

Total Users: {total}
Pro Users: {pro}
Free Users: {total - pro}""")
        finally:
            release_db(conn)

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active until {get_pro_expiry(user_id)}")
        else:
            send_message(chat_id, f"🆓 Free - {free_uses_remaining(user_id)} uses left today\n\n/upgrade")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        send_message(chat_id, f"Pro ₦2,000/month\nPay: {link or 'Try again'}")

    elif command in {"/hooks", "/captions", "/pov", "/hashtags", "/bio"}:
        mode = command.replace("/", "")
        if not topic:
            send_message(chat_id, f"Example: /hooks motivation")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            send_message(chat_id, "Free uses finished today. /upgrade")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, "🔥 Cooking...")

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result}")

    return jsonify({"ok": True})

@app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    # Your existing webhook code
    pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
