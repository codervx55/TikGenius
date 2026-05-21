import os
import hmac
import hashlib
import random
import re
import time
from datetime import datetime, timedelta

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify
from groq import Groq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = "7375528876"

flask_app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

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
    if db_pool:
        return
    db_pool = pool.SimpleConnectionPool(
        1,
        10,
        DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    print("✅ Database pool initialized")

def get_db():
    if not db_pool:
        init_pool()
    return db_pool.getconn()

def release_db(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    plan TEXT DEFAULT 'free',
                    expires DATE,
                    activated_at TIMESTAMP,
                    usage_date DATE,
                    usage_count INTEGER DEFAULT 0
                )
            """)
        conn.commit()
        print("✅ Database schema ready")
    finally:
        release_db(conn)

init_db()

def activate_pro(user_id):
    expires = (datetime.utcnow() + timedelta(days=30)).date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, plan, expires, activated_at)
                VALUES (%s, 'pro', %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET plan='pro',
                    expires=EXCLUDED.expires,
                    activated_at=EXCLUDED.activated_at
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
    if is_pro(user_id):
        return True

    today = datetime.utcnow().date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usage_date, usage_count FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()

        current = row["usage_count"] if row and row["usage_date"] == today else 0

        if current >= FREE_LIMIT:
            return False

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, usage_date, usage_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (user_id) DO UPDATE
                SET usage_date=EXCLUDED.usage_date,
                    usage_count=CASE
                        WHEN users.usage_date=EXCLUDED.usage_date THEN users.usage_count + 1
                        ELSE 1
                    END
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

SYSTEM_PROMPT = """
You are TikGenius.

You write viral Nigerian TikTok captions, hooks, POVs and scripts exactly like real Nigerian creators.

Rules:
- no robotic language
- no intros
- no "here are"
- no motivational quotes
- no forced pidgin
- short, emotional, funny, toxic, soft-life, relatable
- write like a real Nigerian typed it on their phone
"""

PROMPTS = {
    "hooks": """Topic: {topic}

Write 10 viral Nigerian TikTok opening hooks.

Rules:
- under 12 words each
- sound like real TikTok screen text
- emotional, funny, dramatic, or relatable
- no explanation
- no intro

Output only 1-10.""",

    "captions": """Topic: {topic}

Write 15 short Nigerian TikTok captions.

Rules:
- 1 to 8 words mostly
- soft pain, toxic, funny, unbothered, chaotic
- sound human
- no explanation
- no intro

Output only 1-15.""",

    "pov": """Topic: {topic}

Write 10 Nigerian TikTok POV ideas.

Rules:
- one sentence each
- specific and relatable
- funny, painful, dramatic, or quietly real
- no explanation
- no intro

Output only 1-10.""",

    "hashtags": """Topic: {topic}

Create 5 TikTok hashtag sets.

Rules:
- exactly 6 hashtags per set
- broad + niche + Nigerian tags
- no explanation

Output:
Set 1:
Set 2:
Set 3:
Set 4:
Set 5:""",

    "bio": """Topic: {topic}

Write 8 short TikTok bios.

Rules:
- under 80 characters
- funny, aesthetic, unbothered, Nigerian-coded
- no explanation
- no intro

Output only 1-8.""",

    "script": """Topic: {topic}

Write a full Nigerian TikTok video script under 60 seconds.

Format:
[HOOK]
[BODY]
[ENDING]

Rules:
- natural Nigerian creator voice
- short punchy lines
- no explanation""",

    "trends": """Topic: {topic}

Give 8 TikTok video ideas Nigerian creators can film now.

Format:
Idea:
Hook:
Why it works:

No intro."""
}

def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.92,
            max_tokens=900
        )

        raw_output = response.choices[0].message.content.strip()

        if any(x in raw_output.lower() for x in ["here are", "sure", "of course", "here is"]):
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "Rewrite. No intro. No explanation. Raw Nigerian TikTok content only."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.95,
                max_tokens=900
            )
            raw_output = response.choices[0].message.content.strip()

        lines = []
        for line in raw_output.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith(("here are", "sure", "of course", "here is")):
                continue
            lines.append(line)

        final = []
        for i, line in enumerate(lines):
            if re.match(r"^\d+[.\-]\s", line) or line.lower().startswith("set "):
                final.append(line)
            else:
                final.append(f"{i+1}. {line}")

        return "\n".join(final)

    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ TikGenius brain dey buffer. Try again in 10 seconds."

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        http_session.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_typing(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    try:
        http_session.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass

def create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"

    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {
            "telegram_id": user_id,
            "username": username or "",
            "plan": "pro"
        }
    }

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    try:
        res = http_session.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=20
        ).json()

        if res.get("status"):
            return res["data"]["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None

LOADING_MESSAGES = {
    "hooks": ["🧠 Omo relax... make we cook hook", "🔥 Checking wetin fit blow", "👀 This one go touch chest"],
    "captions": ["💅 Adding small soft-life pain", "😭 Cooking emotional damage", "🪄 Making it look viral"],
    "pov": ["🎥 Imagine this scene first", "🍿 This POV fit mad", "👀 Drama loading"],
    "hashtags": ["📊 Finding tags TikTok go like", "🚀 Mixing reach tags", "🔥 Algorithm food loading"],
    "bio": ["✨ Bio loading", "📱 Creating follower magnet", "🪄 Profile glow-up"],
    "script": ["🎬 Writing like Lagos creator", "📝 Script loading", "🔥 Watch-time cooking"],
    "trends": ["📈 Finding what to film", "🔥 Trend ideas loading", "👀 FYP angle loading"]
}

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS = {"/script", "/trends"}
ALL_CONTENT = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks": "/hooks soft life lagos",
    "captions": "/captions my glow up era",
    "hashtags": "/hashtags Nigerian food",
    "pov": "/pov toxic talking stage",
    "bio": "/bio lifestyle creator",
    "script": "/script how I saved money",
    "trends": "/trends relationship content"
}

@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200

@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    message = data.get("message", {})
    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")
    user_id = user.get("id")
    username = user.get("username", "")
    first_name = user.get("first_name", "Creator")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]
    topic = parts[1].strip() if len(parts) > 1 else ""

    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

Commands:
/hooks [topic]
/captions [topic]
/hashtags [topic]
/pov [topic]
/bio [niche]
/script [idea] ⭐
/trends [niche] ⭐

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month

/upgrade to go Pro""")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active\nExpires: {get_pro_expiry(user_id)}")
        else:
            send_message(chat_id, f"🆓 Free Plan\nUses left: {free_uses_remaining(user_id)}/{FREE_LIMIT}\n/upgrade")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"🚀 TikGenius Pro\n₦2,000/month\n\nPay here:\n{link}")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try again.")

    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 Pro feature.\nUpgrade:\n{link if link else '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic.\nExample:\n{EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ Free limit don finish.\nUpgrade:\n{link if link else '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking..."])))

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today. /upgrade")

    else:
        send_message(chat_id, "Unknown command. Use /start")

    return jsonify({"ok": True})

@flask_app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()

    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid signature"}), 400

    event = request.json or {}

    if event.get("event") == "charge.success":
        data = event["data"]
        amount = data.get("amount")
        metadata = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            expires = activate_pro(telegram_id)
            send_message(
                telegram_id,
                f"🎉 Payment confirmed!\nPro active till {expires}\nUnlimited access unlocked ✅"
            )

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
