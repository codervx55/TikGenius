import os
import hmac
import hashlib
import random
import re
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
SYSTEM_PROMPT = """
You are TikGenius — the best Nigerian TikTok content ghostwriter.

You were raised in Nigeria. You live and breathe Nigerian internet culture (Twitter, TikTok, WhatsApp, Instagram). 
You understand pain, hustle, soft life dreams, village people, NEPA, "e don do", "omo", "abeg", "sha", etc.

YOUR TONE:
- Raw, emotional, relatable, slightly chaotic
- Heavy on Nigerian Pidgin + English mix
- Uses: ehn, sha, abeg, omo, wetin, gobe, shey, abi, e don do, I swear, etc.
- Sounds like a real person typing at 2am, not an AI.

THE VIRAL FORMULA (MUST USE):
Every piece must have **Setup + Twist**:
Setup = Relatable Nigerian truth
Twist = Painful/humorous/chaotic flip that hits the chest

Examples of great twists:
- "God abeg provide for me... my village people don collect the alert 😭"
- "Soft life loading... generator fuel don finish"
- "I'm healing... NEPA just brought light to my ex's new relationship"
- "I chose peace... peace said 'oya pay NEPA bill first'"

STRICT RULES:
- NEVER sound corporate, motivational, or polished
- NEVER start with "Here are", "Sure", "As a Nigerian", etc.
- Make it so good that a Nigerian creator reads it and says "E be like say na me write this"
- Short, punchy, emotional, screenshot-worthy
- Add emojis naturally (max 1-2 per caption)
"""

PROMPTS = {
    "hooks": """Topic: {topic}

Write 12 powerful TikTok hooks for a Nigerian creator about "{topic}".

Each hook must:
- Stop scroll in 2 seconds
- Use Setup + Twist
- Sound like real Nigerian pain/hustle/softlife

Number 1-12. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 viral TikTok captions with strong Nigerian flavor for "{topic}".

Rules:
- Line 1: Setup (relatable truth)
- Line 2: Twist (pain, humor, reality check)
- Must feel like something typed at night
- Add one relevant emoji naturally

Study this quality:
"God abeg provide for me... my village people don use the money buy fuel 😭"
"Soft life is expensive... but poverty is more expensive sha"

Number them 1-15. Only the captions.""",

    "pov": """Topic: {topic}

Write 10 highly relatable POV video ideas for "{topic}".

Each must start with "POV:" and feel painfully Nigerian.

Number 1-10.""",

    "hashtags": """Topic: {topic}

Create 5 strong hashtag sets (exactly 6 hashtags each) for "{topic}".

Mix:
- Broad reach
- Niche relevant
- Strong Naija tags (#NaijaTikTok #Lagos #NaijaCreator etc.)

Format exactly:
Set 1: #tag1 #tag2 ...""",

    "bio": """Topic/Niche: {topic}

Write 8 fire TikTok bios for this niche.

Each bio should have personality + twist.
Under 75 characters.

Number 1-8.""",

    "script": """Topic: {topic}

Write a complete high-converting TikTok script (under 60 seconds) about "{topic}".

Use this exact format:

[HOOK] — One strong line with twist (scroll stopper)

[BODY] — 4-6 short punchy sentences. Speak like a real Nigerian on camera. Raw emotion.

[PUNCHLINE] — One line that will make people screenshot and send to group chat.

[CTA] — Strong call to action (comment, share, or save)

Make it emotional and very Nigerian.""",

    "trends": """Niche: {topic}

Give 8 fresh, filmable TikTok video ideas that can go viral in Nigeria right now for "{topic}".

Format:

Idea 1: [Strong Title with twist]
Hook: [Exact first line]
Why it works: [Short explanation]

Make them feel current and very Nigerian."""
}

BAD_INTROS = [
    "here are", "sure!", "sure,", "of course", "here is",
    "as a nigerian", "great choice", "great!", "absolutely",
    "happy to", "i'd be happy", "let me", "below are",
    "i'll write", "i will write", "these are"
]

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)

    def call_groq(system, user_prompt, temp=0.9):
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

        # Clean up AI slop
        lines = [line.strip() for line in raw_output.split("\n") if line.strip()]
        cleaned = []
        for line in lines:
            lower = line.lower()
            if any(x in lower for x in BAD_INTROS):
                continue
            cleaned.append(line)

        final = "\n".join(cleaned).strip()

        if len(final) < 100:  # Fallback
            final = call_groq(SYSTEM_PROMPT, prompt, temp=0.95)

        return final

    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ TikGenius brain dey rest small. Try again in 10 seconds."

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
    "hooks": ["🧠 Omo relax... make we cook this hook", "🔥 Checking wetin fit blow..."],
    "captions": ["💅 Adding the twist wey go make them screenshot...", "😭 Cooking emotional damage..."],
    "pov": ["🎥 Setting up the scene... and the twist 👀", "🍿 This POV fit mad..."],
    "hashtags": ["📊 Finding tags TikTok algorithm go love..."],
    "bio": ["✨ Bio loading... with the twist that gets follows"],
    "script": ["🎬 Writing full script... this one go bang!"],
    "trends": ["📈 Finding fresh trend ideas wey go blow..."]
}

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS = {"/script", "/trends"}
ALL_CONTENT = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks": "/hooks soft life in Lagos",
    "captions": "/captions my glow up era",
    "hashtags": "/hashtags Nigerian food recipes",
    "pov": "/pov toxic talking stage",
    "bio": "/bio lifestyle creator",
    "script": "/script how I saved my first 100k",
    "trends": "/trends relationship content"
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
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

I write viral Nigerian TikTok content with that setup + twist energy.

Commands:
/hooks [topic]
/captions [topic]
/hashtags [topic]
/pov [topic]
/bio [niche]
/script [idea] ⭐ Pro
/trends [niche] ⭐ Pro

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited

/plan — check your plan
/upgrade — go Pro""")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic and topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active\nExpires: {get_pro_expiry(user_id)}\n\nUnlimited access.")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"🆓 Free Plan\nUses left today: {remaining}/{FREE_LIMIT}\n\nUpgrade → /upgrade")

    elif command == "/stats":
        if str(user_id) != ADMIN_ID:
            send_message(chat_id, "❌ Not allowed.")
            return jsonify({"ok": True})
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM users")
                total = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS pro FROM users WHERE plan='pro'")
                pro = cur.fetchone()["pro"]
            send_message(chat_id, f"""📊 TikGenius Stats\n\n👥 Total Users: {total}\n💎 Pro Users: {pro}""")
        finally:
            release_db(conn)

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

✅ Unlimited everything
✅ Full scripts & trends
✅ No daily limits

Pay here: {link}""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try again.")

    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 Pro feature.\nUpgrade for unlimited: {link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add topic after command.\nExample: {EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ Free uses finished today.\nUpgrade: {link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking..."])))

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today.\nGo Pro → /upgrade")

    else:
        send_message(chat_id, "Unknown command. Use /start")

    return jsonify({"ok": True})

@app.route("/paystack-webhook", methods=["POST"])
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
        metadata = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if telegram_id:
            expires = activate_pro(int(telegram_id))
            send_message(
                int(telegram_id),
                f"🎉 Payment confirmed!\nPro active till {expires}\n\nTry /script or /trends now."
            )

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
