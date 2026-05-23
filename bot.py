import os
import hmac
import hashlib
import random
from datetime import datetime, timedelta

from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify
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
    print("DB pool ready")

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

# ========================= PROMPT IMPROVEMENTS =========================
TIKTOK_SYSTEM = """You are the best TikTok content writer for Nigerian creators... [Your original system prompt remains the same]"""

TIKTOK_PROMPTS = {
    "hooks": """A Nigerian TikTok creator wants hooks about: {topic}

Write 10 short, powerful, scroll-stopping hooks.

RULES:
- Maximum 1-2 short sentences per hook
- Must be readable in 4-8 seconds
- First 3-6 words must grab attention
- Use specific Nigerian emotions and details
- Never use "..." — use dash (—) instead
- Write like a real Nigerian creator, not a quote page

EXAMPLES OF PERFECT LENGTH:
"POV: It's 2AM, you're staring at the ceiling, still not where you want to be financially — but you choose to keep pushing."
"I used to explain my dreams to everyone. Not anymore."
"God didn't bring me this far to leave me stuck here."

Now write 10 ORIGINAL hooks:

1)
2)
3)
4)
5)
6)
7)
8)
9)
10)""",

    "pov": """A Nigerian TikTok creator needs POVs about: {topic}

Write 10 short, highly relatable POVs.

RULES:
- Each POV: 1-2 short sentences maximum
- Make it feel like "this is literally me"
- Be specific but concise
- Target length: 6-10 seconds to read

GOOD EXAMPLES:
"POV: You're lying in bed at night staring at the ceiling, still not where you want to be financially — but you take a deep breath and keep going."

"POV: Your Nigerian parents are calling again asking when you'll settle down and you're tired of explaining that things have changed."

Now write 10 ORIGINAL POVs:

1) POV:
2) POV:
3) POV:
4) POV:
5) POV:
6) POV:
7) POV:
8) POV:
9) POV:
10) POV:""",

    # Keep your other prompts (captions, hashtags, bio, etc.) as they are
    "captions": """...""",   # ← your original captions prompt
    "hashtags": """...""",
    "bio": """...""",
    "script": """...""",
    "trends": """...""",
}

# ========================= AI FUNCTION =========================
def ask_claude(mode, topic, platform="tiktok"):
    if platform == "x":
        system = X_SYSTEM
        prompt_template = X_PROMPTS.get(mode, X_PROMPTS["captions"])
    else:
        system = TIKTOK_SYSTEM
        prompt_template = TIKTOK_PROMPTS.get(mode, TIKTOK_PROMPTS["captions"])

    prompt = prompt_template.format(topic=topic)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.85,
        "max_tokens": 1400          # Reduced for tighter outputs
    }

    try:
        res = http_session.post(url, json=payload, headers=headers, timeout=30)
        data = res.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Groq Error: {e}")
        return "Something went wrong. Please try again."

# ========================= HELPER TO TRIM LONG OUTPUTS =========================
def trim_if_too_long(text, max_length=3800):
    if len(text) > max_length:
        return text[:max_length] + "\n\n✂️ Output was too long. Want me to make it shorter?"
    return text

# ========================= HELPERS =========================
def send_message(chat_id, text):
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10
        )
    except Exception as e:
        print(f"Telegram error: {e}")

def send_typing(chat_id):
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"}, timeout=5
        )
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
        res = http_session.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload, headers=headers, timeout=20
        ).json()
        return res["data"]["authorization_url"] if res.get("status") else None
    except Exception as e:
        print(f"Paystack Error: {e}")
        return None

# ========================= CONTENT CONFIG =========================
# ... (Keep your LOADING, EXAMPLES, TIKTOK_COMMANDS, X_COMMANDS as they are)

# ========================= ROUTES =========================
# ... (Keep your existing routes exactly the same until the content generation part)

    # ── TikTok commands ──
    elif command in TIKTOK_COMMANDS:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 This is a Pro feature.\n\nUpgrade for ₦2,000/month to unlock:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add your topic after the command.\n\nExample: {EXAMPLES.get(mode, f'/{mode} your topic here')}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You have used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro for unlimited access:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it"])))
        
        result = ask_claude(mode, topic, "tiktok")
        final_text = f"✨ TikGenius\n\n{trim_if_too_long(result)}"
        
        send_message(chat_id, final_text)

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today.\n\nGo Pro for ₦2,000/month — unlimited everything → /upgrade")

    # (Same logic for X commands...)

    else:
        send_message(chat_id, "Unknown command. Use /start to see everything.")

    return jsonify({"ok": True})

# ... (Keep the rest of your code: paystack webhook, etc.)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
