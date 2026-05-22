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

# ========================= HTTP SESSION =========================
def get_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = get_session()
db_pool = None

# ========================= DATABASE =========================
def init_pool():
    global db_pool
    if db_pool:
        return
    db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL, cursor_factory=RealDictCursor)
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

# ========================= AI PROMPTS =========================
SYSTEM_PROMPT = """
You are TikGenius — the best Nigerian TikTok content ghostwriter.

You understand real Nigerian internet language. You mix **English and Pidgin naturally** like actual Nigerian creators on TikTok and Twitter.

**Language Style (Very Important):**
- Use mostly clear English with natural Pidgin sprinkles (omo, ehn, sha, abeg, wetin, gobe, e don do, etc.)
- Do NOT overuse Pidgin in every sentence. Make it flow naturally.
- Example of good balance: "Omo, this life is not easy sha... but God abeg provide"
- Sound like a real young Nigerian typing — emotional, relatable, funny, not forced Pidgin.

THE VIRAL FORMULA:
Every content must have **Setup + Twist** — relatable truth followed by painful/funny Nigerian reality.

STRICT RULES:
- Never sound like full Pidgin or broken English.
- Never start with "Here are", "Sure", "As a Nigerian", etc.
- Make it feel like "Na me write this one" for Nigerian creators.
- Keep it emotional and screenshot-worthy.
"""

PROMPTS = {
    "hooks": """Topic: {topic}

Write 12 powerful TikTok hooks about "{topic}".

Use natural mix of English + light Pidgin. Make them emotional and scroll-stopping with setup + twist.

Number 1-12. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 viral TikTok captions for "{topic}".

Rules:
- Mostly English with natural Pidgin touches
- Line 1: Setup (relatable)
- Line 2: Twist (pain/humor/reality)
- Add one emoji where it fits

Example good style:
"God abeg provide for me... but my village people don collect the alert 😭"
"Soft life is calling... but my account balance said not yet sha"

Number them 1-15. Only the captions.""",

    "pov": """Topic: {topic}

Write 10 relatable POV ideas for "{topic}".

Start each with "POV:". Use natural English + Pidgin mix. Make them feel very Nigerian.

Number 1-10.""",

    "hashtags": """Topic: {topic}

Create 5 strong hashtag sets (exactly 6 each) for "{topic}".

Format:
Set 1: #tag1 #tag2 ...""",

    "bio": """Topic/Niche: {topic}

Write 8 fire TikTok bios. Natural English + Pidgin mix. Under 75 characters each.

Number 1-8.""",

    "script": """Topic: {topic}

Write a full TikTok script about "{topic}".

Use this format:

[HOOK] — Strong scroll-stopper with twist

[BODY] — 4-6 short natural sentences (speak like real Nigerian on camera)

[PUNCHLINE] — One hard-hitting line

[CTA] — Call to action

Natural English + Pidgin mix.""",

    "trends": """Niche: {topic}

Give 8 fresh viral TikTok video ideas for "{topic}".

Format:
Idea 1: [Title with twist]
Hook: [First line]
Why it works: [Short reason]"""
}

BAD_INTROS = ["here are", "sure!", "of course", "as a nigerian", "i will", "let me", "below are"]

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)

    def call_groq(system, user_prompt, temp=0.88):
        return groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temp,
            max_tokens=1500
        ).choices[0].message.content.strip()

    try:
        raw_output = call_groq(SYSTEM_PROMPT, prompt)

        lines = [line.strip() for line in raw_output.split("\n") if line.strip()]
        cleaned = []
        for line in lines:
            if any(bad in line.lower() for bad in BAD_INTROS):
                continue
            cleaned.append(line)

        final = "\n".join(cleaned).strip()

        if len(final) < 100:
            final = call_groq(SYSTEM_PROMPT, prompt, temp=0.92)

        return final

    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ TikGenius brain dey buffer. Try again."

# ========================= TELEGRAM HELPERS =========================
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

# ========================= PAYSTACK =========================
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
            json=payload, headers=headers, timeout=20
        ).json()
        if res.get("status"):
            return res["data"]["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None

# ========================= LOADING MESSAGES =========================
LOADING_MESSAGES = {
    "hooks": ["🧠 Cooking strong hooks...", "🔥 Making them scroll-stopping..."],
    "captions": ["💅 Adding the perfect twist..."],
    "pov": ["🎥 POV ideas loading..."],
    "hashtags": ["📊 Hashtags wey go blow..."],
    "bio": ["✨ Fire bios incoming..."],
    "script": ["🎬 Full script cooking..."],
    "trends": ["📈 Fresh ideas dey load..."]
}

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS = {"/script", "/trends"}
ALL_CONTENT = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks": "/hooks motivation",
    "captions": "/captions soft life",
    "hashtags": "/hashtags Nigerian food",
    "pov": "/pov toxic relationship",
    "bio": "/bio content creator",
    "script": "/script how I started making money",
    "trends": "/trends motivation"
}

# ========================= ROUTES =========================
@app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200

@app.route("/telegram-webhook", methods=["POST"])
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
        send_message(chat_id, f"""🔥 Welcome to TikGenius {first_name} 🇳🇬

I create viral Nigerian TikTok content with natural English + Pidgin mix.

Commands:
/hooks [topic]
/captions [topic]
/pov [topic]
/hashtags [topic]
/bio [niche]
/script [idea] ⭐ Pro
/trends [niche] ⭐ Pro

Free: {FREE_LIMIT} uses/day | Pro: ₦2,000/month

Use /plan or /upgrade""")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target)
            send_message(chat_id, f"✅ Pro activated for {target} till {expires}")
        else:
            send_message(chat_id, "❌ Admin only.")

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ You are on Pro\nExpires: {get_pro_expiry(user_id)}")
        else:
            send_message(chat_id, f"🆓 Free Plan\nUses left: {free_uses_remaining(user_id)}/{FREE_LIMIT}\n\nUpgrade → /upgrade")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 Go Pro for ₦2,000/month

Unlimited access + scripts & trends

Pay here: {link}""")
        else:
            send_message(chat_id, "⚠️ Failed to generate link. Try again.")

    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 This is Pro only.\nUpgrade here: {link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic.\nExample: {EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ Free uses finished.\nUpgrade: {link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking..."])))

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free uses left today.\nGo Pro → /upgrade")

    else:
        send_message(chat_id, "Unknown command. Send /start")

    return jsonify({"ok": True})

@app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()

    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid signature"}), 400

    event = request.json or {}
    if event.get("event") == "charge.success":
        metadata = event["data"].get("metadata", {})
        telegram_id = metadata.get("telegram_id")
        if telegram_id:
            expires = activate_pro(int(telegram_id))
            send_message(int(telegram_id), f"🎉 Pro activated successfully!\nExpires: {expires}\nTry /script now.")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
