import os
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

# ========================= HTTP & DB =========================
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

# ========================= USER MANAGEMENT =========================
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

# ========================= AI PROMPTS =========================
TIKTOK_SYSTEM_PROMPT = """
You are TikGenius. You write TikTok content for a young Nigerian guy who films himself talking directly to the camera.

Style:
- Natural spoken English first
- Light Pidgin (omo, sha, ehn, abeg, gobe) only when it fits naturally
- Sound like a real person speaking casually
- Short, relatable, personal
"""

X_SYSTEM_PROMPT = """
You are XGenius. You write Twitter/X captions for a young Nigerian guy.

Style for X:
- Proper, correct English only
- No Pidgin at all
- Punchy, bold, witty and engaging
- Great for trending topics
- Use emojis naturally
- Keep each caption under 280 characters
- Professional yet conversational tone
"""

PROMPTS = {
    "captions": {
        "tiktok": """Topic: {topic}

Write 12 natural captions for my face video about "{topic}". 
Use natural English + light Pidgin mix where it feels natural.""",

        "x": """Topic: {topic}

Write 10 strong Twitter/X captions about "{topic}".
- Use proper English only (no Pidgin)
- Make them punchy, bold and engaging
- Good for trending topics
- Keep each one under 280 characters
- Number them 1-10."""
    }
}

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic, platform="tiktok"):
    system_prompt = TIKTOK_SYSTEM_PROMPT if platform == "tiktok" else X_SYSTEM_PROMPT
    prompt = PROMPTS.get(mode, {}).get(platform, f"Topic: {topic}").format(topic=topic)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=1000
        )
        raw = response.choices[0].message.content.strip()
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        cleaned = [line for line in lines if not any(x in line.lower() for x in ["here are", "sure!", "as a"])]
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
    except Exception as e:
        print(f"Paystack Error: {e}")
        return None

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

    command = text.split()[0].lower().split("@")[0]

    if command == "/start":
        send_message(chat_id, f"""🔥 Welcome {first_name} to TikGenius 🇳🇬

**Commands:**
/hooks [topic] — TikTok hooks
/captions [topic] — TikTok captions (with Pidgin)
/captions x [topic] — X/Twitter captions (Proper English)
/plan — Check your plan
/upgrade — Go Pro""")

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active until {get_pro_expiry(user_id)}")
        else:
            send_message(chat_id, f"🆓 Free Plan\nUses left today: {free_uses_remaining(user_id)}/{FREE_LIMIT}\n\n/upgrade")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

Unlimited access
Pay here: {link or "Try again later"}""")

    elif command == "/captions":
        args = text.split(maxsplit=2)
        platform = "tiktok"
        
        if len(args) > 1 and args[1].lower() in ["x", "twitter", "tiktok"]:
            platform = "x" if args[1].lower() in ["x", "twitter"] else "tiktok"
            topic = args[2].strip() if len(args) > 2 else ""
        else:
            topic = " ".join(args[1:]).strip()

        if not topic:
            send_message(chat_id, """Usage:
/captions [topic]          → TikTok (with Pidgin)
/captions x [topic]        → X/Twitter (Proper English)

Example: /captions x fuel price increase""")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ Free uses finished.\nUpgrade: {link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, f"🔥 Generating {platform.upper()} captions...")

        result = ask_ai("captions", topic, platform)
        platform_name = "X/Twitter (Proper English)" if platform == "x" else "TikTok"
        send_message(chat_id, f"✨ {platform_name} Captions\n\n{result}")

    elif command in {"/hooks", "/pov", "/hashtags", "/bio"}:
        mode = command.replace("/", "")
        topic = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""

        if not topic:
            send_message(chat_id, f"Example: /{mode} motivation")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ Free uses finished.\nUpgrade: {link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, "🔥 Cooking...")

        result = ask_ai(mode, topic, "tiktok")
        send_message(chat_id, f"✨ TikTok Content\n\n{result}")

    return jsonify({"ok": True})

@app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
