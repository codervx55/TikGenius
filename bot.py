import os
import hmac
import hashlib
import random
import re
import time
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify
from groq import Groq

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE_URL       = os.getenv("DATABASE_URL")

# Pricing & Limits
PRICE_KOBO = 200000  # ₦2,000
FREE_LIMIT = 5
ADMIN_ID   = "7375528876"  # Change to your Telegram User ID

# App Setup
flask_app   = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

# Create a session with automatic retries for robustness
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

def get_session():
    session = requests.Session()
    retry = Retry(
        total=3, 
        backoff_factor=1, 
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

http_session = get_session()

# ─────────────────────────────────────────────
# DATABASE (With Connection Pooling)
# ─────────────────────────────────────────────
from psycopg2 import pool

db_pool = None

def init_pool():
    global db_pool
    try:
        db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)
        print("✅ Database pool initialized")
    except Exception as e:
        print(f"❌ Database pool failed: {e}")

def get_db():
    if not db_pool:
        init_pool()
    return db_pool.getconn()

def release_db(conn):
    if db_pool:
        db_pool.putconn(conn)

def init_db():
    """Create users table if missing."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id      BIGINT PRIMARY KEY,
                    plan         TEXT    DEFAULT 'free',
                    expires      DATE,
                    activated_at TIMESTAMP,
                    usage_date   DATE,
                    usage_count  INTEGER DEFAULT 0
                )
            """)
        conn.commit()
        print("✅ Database schema ready")
    except Exception as e:
        print(f"❌ DB Init Error: {e}")
    finally:
        release_db(conn)

# Run init on startup
init_db()

# ─────────────────────────────────────────────
# USER DATA LOGIC
# ─────────────────────────────────────────────
def activate_pro(user_id):
    expires = (datetime.utcnow() + timedelta(days=30)).date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, plan, expires, activated_at)
                VALUES (%s, 'pro', %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET plan         = 'pro',
                        expires      = EXCLUDED.expires,
                        activated_at = EXCLUDED.activated_at
            """, (user_id, expires, datetime.utcnow()))
        conn.commit()
        return expires.strftime("%Y-%m-%d")
    finally:
        release_db(conn)

def is_pro(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT plan, expires FROM users WHERE user_id = %s", (user_id,))
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
            cur.execute("SELECT expires FROM users WHERE user_id = %s", (user_id,))
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
            cur.execute(
                "SELECT usage_date, usage_count FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cur.fetchone()

        current = 0
        if row and row["usage_date"] == today:
            current = row["usage_count"]

        if current >= FREE_LIMIT:
            return False

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, usage_date, usage_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (user_id) DO UPDATE
                    SET usage_date  = EXCLUDED.usage_date,
                        usage_count = CASE
                            WHEN users.usage_date = EXCLUDED.usage_date THEN users.usage_count + 1
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
            cur.execute(
                "SELECT usage_date, usage_count FROM users WHERE user_id = %s",
                (user_id,)
            )
            row = cur.fetchone()
        if not row or row["usage_date"] != today:
            return FREE_LIMIT
        return max(0, FREE_LIMIT - row["usage_count"])
    finally:
        release_db(conn)

# ─────────────────────────────────────────────
# AI SYSTEM PROMPT (UPGRADED)
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are TikGenius, the #1 viral content engine for Nigerian TikTok.

CRITICAL RULES:
1. NO ROBOTIC LANGUAGE. Never say "Here are the hooks," "Sure, I can do that," or "Here is your list."
2. PURE NAIJA VIBE. Mix Pidgin, English, and slang naturally. Use words like: 'wahala', 'sapa', 'chop', 'abeg', 'sharp sharp', 'no be today', 'vibe', 'glow up', 'toxic', 'main character'.
3. EMOTIONAL TRUTH. The content must feel like a real human typing fast on a phone at 2 AM.
4. LENGTH CONTROL. Keep hooks under 10 words. Keep captions punchy.
5. FORMATTING. Output ONLY the list. No intros. No outros. No markdown bolding unless it's for emphasis like **this**.

If the user asks for 'hooks', 'captions', 'pov', etc., you act as a top-tier Nigerian creative director.
"""

PROMPTS = {
    "hooks": """Topic: {topic}
Write 10 viral TikTok opening hooks.
Rules:
- under 12 words each
- no full stop at the end unless it's an emoji
- lowercase feels more human
- mix emotions: funny, painful, dramatic
- output ONLY the 10 hooks numbered 1–10. No intro.""",

    "captions": """Topic: {topic}
Write 15 short Nigerian TikTok captions.
Rules:
- 1 to 8 words mostly
- mix Pidgin, English, and mixed naturally
- aesthetic, soft, unbothered, chaotic
- output ONLY the 15 captions numbered 1–15. No intro.""",

    "pov": """Topic: {topic}
Write 10 POV video ideas.
Rules:
- one sentence each
- specific and relatable
- mix Pidgin, English, and mixed
- output ONLY the 10 POVs numbered 1–10. No intro.""",

    "hashtags": """Topic: {topic}
Create 5 ready-to-copy TikTok hashtag sets.
Rules:
- exactly 6 hashtags per set
- Set 1: max reach, Set 2: niche, Set 3: Naija, Set 4: mood, Set 5: combo
- No explanation. Just the sets.
Output format:
Set 1: #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag""",

    "bio": """Topic: {topic}
Write 8 short TikTok bios.
Rules:
- under 80 characters each
- mix Pidgin, English, and mixed styles
- funny, aesthetic, unbothered
- output ONLY the 8 bios numbered 1–8. No intro.""",

    "script": """Topic: {topic}
Write a full TikTok video script (under 60s).
Format:
[HOOK] — first 3 seconds.
[BODY] — main content. Short punchy lines.
[ENDING] — mic-drop line or CTA.
Style: Talk like a Nigerian creator. Mix Pidgin and English.
Output ONLY the script. No explanation.""",

    "trends": """Topic: {topic}
Give 8 specific video ideas for Nigerian TikTok RIGHT NOW.
Format:
Idea: [one line]
Hook: [exact text]
Why it works: [one sentence]
Output ONLY the 8 ideas. No intro.""",
}

# ─────────────────────────────────────────────
# ADVANCED AI GENERATOR
# ─────────────────────────────────────────────
def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)
    
    try:
        # Step 1: First attempt
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Generate content for: {topic}\n\n{prompt}"},
            ],
            temperature=0.85,
            max_tokens=1000,
        )
        raw_output = response.choices.message.content.strip()

        # Step 2: Quality Check & Self-Correction
        # If the AI output is too short or contains robotic intros, retry with stricter instructions
        if len(raw_output.split()) < 10 or \
           any(keyword in raw_output.lower() for keyword in ["here are", "sure", "here is", "of course", "no problem"]):
            
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": "ERROR: Output was bad. REWRITE it. No intros. No 'Here are'. Just the raw content. Be funnier and more Naija."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                max_tokens=1000,
            )
            raw_output = response.choices.message.content.strip()

        # Step 3: Clean and Format
        lines = raw_output.split('\n')
        cleaned = []
        for line in lines:
            line = line.strip()
            # Remove AI preamble
            if line.lower().startswith(("here are", "sure", "here is", "of course", "no problem", "absolutely")):
                continue
            if line:
                cleaned.append(line)
        
        # Re-number if missing (Safety net)
        final_output = []
        for i, line in enumerate(cleaned):
            # Check if line starts with a number like "1. " or "1-"
            if not re.match(r'^\d+[.\-]\s', line):
                final_output.append(f"{i+1}. {line}")
            else:
                final_output.append(line)
        
        return "\n".join(final_output)

    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ My brain is buffering (API error). Try again in 10 seconds."

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
def send_message(chat_id, text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        http_session.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram send error: {e}")

def send_typing(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    try:
        http_session.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass

def simulate_typing_duration(chat_id, duration_seconds=5):
    """Send typing action multiple times to simulate long processing"""
    start_time = time.time()
    while time.time() - start_time < duration_seconds:
        send_typing(chat_id)
        time.sleep(4)

# ─────────────────────────────────────────────
# PAYMENT HELPERS
# ─────────────────────────────────────────────
def create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {
            "telegram_id": user_id,
            "username": username or "",
            "plan": "pro",
        },
    }
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    try:
        res = http_session.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload, headers=headers, timeout=20,
        ).json()
        if res.get("status"):
            return res["data"] ["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None

def verify_payment_status(reference):
    """Helper to verify payment status via API (for future /check command)"""
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get('status') and data['data'] ['status'] == 'success':
            return data['data'].get('metadata', {}).get('telegram_id')
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────
# CONSTANTS & MESSAGES
# ─────────────────────────────────────────────
LOADING_MESSAGES = {
    "hooks": [
        "🧠 Omo relax... make we think like TikTok girls small",
        "🔥 Checking wetin fit blow for Naija FYP...",
        "👀 This hook suppose touch people's chest...",
        "🎬 Cooking something dangerously relatable...",
        "⚡ Almost ready... this one go hit different",
    ],
    "captions": [
        "💅 Generating soft-life caption...",
        "😭 Adding small emotional damage...",
        "🪄 Making it look effortlessly viral...",
        "🥹 This one dey sweet already...",
    ],
    "pov": [
        "🎥 Oya imagine this scene first...",
        "🍿 Wait first... this POV fit mad",
        "👀 This one get drama small...",
        "😭 Somebody definitely go relate to this...",
    ],
    "hashtags": [
        "📊 Finding hashtags wey TikTok go like...",
        "🚀 Oya make algorithm favor you...",
        "🔥 Mixing viral tags together...",
    ],
    "bio": [
        "✨ Soft-life bio loading...",
        "📱 Creating bio wey dey attractive...",
        "🪄 This bio fit collect followers...",
    ],
    "script": [
        "🎬 Writing your script like a Lagos creator...",
        "📝 Cooking full video concept...",
        "🔥 This script go make them watch till the end...",
    ],
    "trends": [
        "📈 Scanning Naija TikTok for what's working...",
        "🔥 Finding ideas wey fit blow this week...",
        "👀 Checking wetin dey land on FYP right now...",
    ],
}

EXTRA_REPLIES = [
    "😭 nah this one too real",
    "🔥 this content dey smell viral",
    "🫠 omo this one hard small",
    "💀 people go relate die",
    "📈 this one fit touch FYP",
    "👀 your followers no go skip this one",
]

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS  = {"/script", "/trends"}
ALL_CONTENT   = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks":    "/hooks soft life lagos",
    "captions": "/captions my glow up era",
    "hashtags": "/hashtags Nigerian food",
    "pov":      "/pov you finally left a toxic situation",
    "bio":      "/bio lifestyle and fashion creator",
    "script":   "/script how I saved ₦500k in 6 months",
    "trends":   "/trends relationship content",
}

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200

@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data    = request.json or {}
    message = data.get("message", {})
    chat    = message.get("chat", {})
    user    = message.get("from", {})

    chat_id    = chat.get("id")
    user_id    = user.get("id")
    username   = user.get("username", "")
    first_name = user.get("first_name", "Creator")
    text       = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts   = text.split(" ", 1)
    command = parts.lower().split("@")
    topic   = parts.strip() if len(parts) > 1 else ""

    # ── /start ──
    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

Your AI TikTok content plug. Built for Nigerian creators.

Commands:
/hooks [topic]     → viral opening lines
/captions [topic]  → short captions
/hashtags [topic]  → 5 hashtag sets
/pov [topic]       → POV video ideas
/bio [niche]       → bio options
/script [idea]     → full video script ⭐
/trends [niche]    → what to film now ⭐

⭐ = Pro only

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

/upgrade to go Pro""")

    # ── /activatepro (admin only) ──
    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\n\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    # ── /plan ──
    elif command == "/plan":
        if is_pro(user_id):
            exp = get_pro_expiry(user_id)
            send_message(chat_id, f"""✅ TikGenius Pro — Active

Expires: {exp}
Usage: Unlimited

No dulling. Your page go blow 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""🆓 Free Plan

Uses left today: {remaining}/{FREE_LIMIT}
Resets at midnight UTC

Pro unlocks:
✅ Unlimited uses
✅ /script — full video scripts
✅ /trends — what to film now

/upgrade → ₦2,000/month""")

    # ── /upgrade ──
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you unlock:
✅ Unlimited hooks, captions, hashtags, POVs, bios
✅ /script — AI writes your full video script
✅ /trends — trending ideas for your niche

Pay here 👇
{link}

Activation is automatic after payment ⚡""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try /upgrade again.")

    # ── Content commands ──
    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        # Pro gate
        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            msg  = f"🔒 {command} is a Pro feature.\n\n"
            msg += f"Upgrade to unlock:\n{link}" if link else "Use /upgrade to go Pro."
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # No topic
        if not topic:
            send_message(chat_id, f"Add a topic 👇\n\nExample:\n{EXAMPLES.get(mode, command + ' [topic]')}")
            return jsonify({"ok": True})

        # Free limit
        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            msg  = "⏳ Free limit don finish for today.\n\nUpgrade for unlimited:\n"
            msg += link if link else "/upgrade"
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Generate
        send_typing(chat_id)
        loading_text = random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking something viral..."]))
        send_message(chat_id, loading_text)
        
        # Simulate processing time for long scripts
        duration = 3 if mode in ["hooks", "captions", "hashtags", "bio", "pov"] else 6
        simulate_typing_duration(chat_id, duration)

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if random.random() < 0.45:
            send_message(chat_id, random.choice(EXTRA_REPLIES))

        # Nudge near limit
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining == 0:
                link = create_payment_link(user_id, username)
                msg  = "⚡ That was your last free use today.\n\nGo unlimited:\n"
                msg += link if link else "/upgrade"
                send_message(chat_id, msg)
            elif remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today. /upgrade to go unlimited.")

    else:
        send_message(chat_id, "Unknown command. Use /start")

    return jsonify({"ok": True})

# ─────────────────────────────────────────────
# PAYSTACK WEBHOOK
# ─────────────────────────────────────────────
@flask_app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body      = request.get_data()
    # Paystack sends raw bytes in body
    if isinstance(body, bytes):
        body = body.decode('utf-8')
    
    expected  = hmac.new(
        PAYSTACK_SECRET_KEY.encode(), body.encode('utf-8'), hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid signature"}), 400

    event = request.json or {}

    if event.get("event") == "charge.success":
        data        = event["data"]
        amount      = data.get("amount")
        metadata    = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            expires = activate_pro(telegram_id)
            send_message(
                telegram_id,
                f"""🎉 Payment confirmed! Welcome to Pro 🚀

Valid until: {expires}
Unlimited access activated ✅

Unlocked:
/script [idea]  — full video script
/trends [niche] — what to film this week

Go try it 🔥
/hooks your niche"""
            )

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
