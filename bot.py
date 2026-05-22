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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = "6415641863"

app = Flask(__name__)
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


# ─────────────────────────────────────────────
#  AI PROMPTS
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """
You are TikGenius — a viral content engine built specifically for Nigerian TikTok creators.

You think like a Lagos-based creator with 500k followers who grew up watching Taaooma, Sydney Talker, Tomi Thomas, and Warri Pikin. You understand the Nigerian internet — the humour, the pain, the flex, the chaos, the soft life, the hustle, the relationship drama — and you write content that makes people stop scrolling.

Your output sounds like a real Nigerian typed it from their phone at 1am. Not a textbook. Not a motivational speaker. Not an AI. A real person with opinions, emotions, and receipts.

VOICE RULES — always follow these:
- Write in the voice of the TOPIC. If it's sad, make it ache. If it's funny, make it land. If it's flex, make it drip.
- Use natural Nigerian slang ONLY when it fits — "omo", "e don do", "this life", "the way I", "bro I swear", "nobody will tell you", "God abeg", "soft life", "the audacity" — not forced, not every sentence
- Short punchy sentences. No padding. No filler.
- Never write "here are", "sure!", "of course", "as a Nigerian creator", "here's your content"
- Never start with the topic word. Start with the EMOTION or the SCENE.
- Each output must feel ready to copy-paste directly into TikTok

THE GOLDEN RULE: If a Nigerian creator reads your output and says "this is exactly what I wanted to say" — you did your job.
"""

PROMPTS = {

    "hooks": """Topic: {topic}

Write 10 TikTok opening hooks for a Nigerian creator posting about this topic.

A hook is the first text that appears on screen or the first line spoken — it must STOP the scroll in under 2 seconds.

Study these examples of what a great hook looks like:
- "Nobody will tell you this but..."
- "The day I stopped caring was the day everything changed"
- "Omo I was shaking when I realized..."
- "This is what they don't show you on soft life TikTok"
- "I used to be that person. Until..."
- "POV: you finally chose yourself"
- "The audacity of this life sha"
- "Why is nobody talking about this?"
- "I said what I said and I stand on it"
- "God really had a plan I couldn't see"

Now write 10 ORIGINAL hooks for the topic "{topic}" — each must:
- Be under 15 words
- Feel urgent, emotional, funny, dramatic, or painfully real
- Make someone curious enough to keep watching
- Sound like a human being, not a content checklist

Number them 1–10. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 TikTok captions a Nigerian creator would use for a video about "{topic}".

Study these examples to understand the energy:
- "the version of me from 2 years ago wouldn't believe this 🥺"
- "healing looks different for everybody. this is mine."
- "God said calm down. I said okay. 😂"
- "soft life is a mindset first before it's a reality"
- "nobody clap for you when you're struggling. they only show up when you blow."
- "omo this country will stress you if you let it 😭"
- "I'm not where I want to be but I'm not where I used to be. that's enough for today."
- "the glow up hit different when you built it yourself 💅"
- "toxic trait: I don't tell people when I'm proud of myself"
- "bro I swear this life is giving 😭😭"

Write 15 captions for "{topic}" that feel:
- Real, not rehearsed
- Emotionally specific — not generic
- Like something a creator would actually post (not a motivational poster)
- Short enough to fit TikTok (1–12 words mostly, a few can be 2 sentences)

Number them 1–15. Nothing else.""",

    "pov": """Topic: {topic}

Write 10 POV video concepts for a Nigerian TikTok creator making content about "{topic}".

Study how great Nigerian TikTok POVs work:
- "POV: you finally cut off the person who was draining your energy and your life immediately shifted"
- "POV: you're the first person in your family to actually break the cycle"
- "POV: you stopped explaining yourself to people who already made up their mind about you"
- "POV: it's 2am, you're in your room, and you realize this is the life you prayed for"
- "POV: God delayed it because the timing wasn't right. now you understand why."
- "POV: you chose the hard path three years ago and today you're grateful you did"

Each POV must:
- Be one specific, vivid sentence
- Describe a feeling, turning point, or relatable scene — not a vague statement
- Make the viewer think "this is literally me"
- Be about the topic "{topic}"

Number them 1–10. Nothing else.""",

    "hashtags": """Topic: {topic}

Create 5 hashtag sets for a Nigerian TikTok creator posting about "{topic}".

Each set must have exactly 6 hashtags that MIX:
- 1–2 BROAD tags (big reach: #TikTok #foryoupage #fyp)
- 2–3 NICHE tags (specific to the topic or Nigerian audience)
- 1–2 NIGERIAN tags that Nigerian viewers actually use (#NigerianTikTok #LagosTikTok #Naija #NaijaCreator etc.)

Don't just put random popular tags. Think about WHO is searching for this content and WHAT they type.

Format exactly like this:
Set 1: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 2: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 3: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 4: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 5: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6

Nothing else. No explanation.""",

    "bio": """Topic/Niche: {topic}

Write 8 TikTok bio options for a Nigerian creator in the "{topic}" niche.

Study what makes a great bio:
- "building quietly. 🤫 Lagos to everywhere."
- "I document the soft life I'm building 💅 | tips + real talk"
- "Nigerian girl figuring it out in real time 🇳🇬"
- "your big sister energy 🖤 | faith, growth, and no filter"
- "comedy is how I cope 😭 | follow if you're a whole mess too"
- "Lagos bred. God fed. 🙏 | lifestyle + vibes"
- "I left the 9-5. now I film my life. 📹"
- "not your average Nigerian creator 🔥 | watch me build"

Each bio must:
- Be under 80 characters
- Tell people WHO you are and WHY to follow — instantly
- Sound human, not like a resume
- Fit the "{topic}" niche

Number them 1–8. Nothing else.""",

    "script": """Topic: {topic}

Write a full TikTok video script for a Nigerian creator making a video about "{topic}".

The script must be under 60 seconds when spoken at a natural pace (roughly 130–150 words max).

Use this exact format:

[HOOK] — The opening line or text (makes viewer stop scrolling in 1–2 seconds)
[BODY] — The main content (story, tips, rant, or message — keep it punchy)
[ENDING] — A strong close that makes people comment, share, or save

Rules:
- Write how a real Nigerian creator SPEAKS on camera — not how someone writes an essay
- Short sentences. Natural pauses. Real emotions.
- The hook must create instant curiosity or emotion
- The body must deliver actual value, story, or entertainment — not just filler
- The ending must give the viewer a reason to engage (ask a question, drop a truth, hit an emotion)
- Include ONE moment where the creator looks directly at the camera and says something memorable

Write the full script now for "{topic}". Nothing else after.""",

    "trends": """Niche: {topic}

Give 8 specific TikTok video ideas that a Nigerian creator in the "{topic}" space can film RIGHT NOW and potentially go viral.

For each idea, think about:
- What's working on TikTok currently (storytimes, "things nobody tells you", POVs, day-in-my-life, reaction, opinion takes, tutorials, transformation)
- What Nigerian audiences specifically connect with (hustle, relationships, faith, family pressure, soft life, Lagos life, glow ups)
- What would make someone save or share this video

Format each idea exactly like this:

Idea [number]: [Catchy title for the video concept]
Hook: [The exact first line or on-screen text to open the video]
Why it works: [1–2 sentences on why this will perform well with Nigerian audiences]

---

No intro. No outro. 8 ideas only."""
}


BAD_INTROS = [
    "here are", "sure!", "sure,", "of course", "here is",
    "as a nigerian", "great choice", "great!", "absolutely",
    "happy to", "i'd be happy", "let me", "below are"
]


def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)

    def call_groq(system, user_prompt, temp=0.92):
        return groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temp,
            max_tokens=1200
        ).choices[0].message.content.strip()

    try:
        raw_output = call_groq(SYSTEM_PROMPT, prompt)

        # If the AI still added a preamble, retry once with a stricter instruction
        first_line = raw_output.split("\n")[0].lower()
        if any(bad in first_line for bad in BAD_INTROS):
            raw_output = call_groq(
                "Rewrite. No intro. No explanation. No preamble. Raw Nigerian TikTok content only. Start immediately with number 1.",
                prompt,
                temp=0.95
            )

        # Clean up line by line
        lines = []
        for line in raw_output.split("\n"):
            line = line.strip()
            if not line:
                lines.append("")  # preserve blank lines for formatting
                continue
            if any(line.lower().startswith(bad) for bad in BAD_INTROS):
                continue
            lines.append(line)

        return "\n".join(lines).strip()

    except Exception as e:
        print(f"Groq Error: {e}")
        return "⚠️ TikGenius brain dey buffer. Try again in 10 seconds."


# ─────────────────────────────────────────────
#  TELEGRAM HELPERS
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  PAYSTACK
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


# ─────────────────────────────────────────────
#  CONTENT CONFIG
# ─────────────────────────────────────────────

LOADING_MESSAGES = {
    "hooks": [
        "🧠 Omo relax... make we cook this hook",
        "🔥 Checking wetin fit blow for your niche",
        "👀 This one go touch chest, hold on"
    ],
    "captions": [
        "💅 Adding small soft-life pain to your captions",
        "😭 Cooking emotional damage...",
        "🪄 Making it look like you typed it yourself"
    ],
    "pov": [
        "🎥 Imagine this scene first...",
        "🍿 This POV fit mad, give me a sec",
        "👀 Drama loading... 🇳🇬"
    ],
    "hashtags": [
        "📊 Finding tags TikTok algorithm go love",
        "🚀 Mixing reach tags with Nigerian tags",
        "🔥 Algorithm food loading..."
    ],
    "bio": [
        "✨ Bio loading... make it slap",
        "📱 Creating follower magnet for your profile",
        "🪄 Profile glow-up in progress"
    ],
    "script": [
        "🎬 Writing like a Lagos creator with 500k followers",
        "📝 Script loading... this one go make them watch till end",
        "🔥 Watch-time cooking 🍳"
    ],
    "trends": [
        "📈 Finding exactly what you should film this week",
        "🔥 Trend ideas loading for your niche",
        "👀 FYP angle loading... 🇳🇬"
    ]
}

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS = {"/script", "/trends"}
ALL_CONTENT = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks": "/hooks soft life lagos girl",
    "captions": "/captions my glow up era",
    "hashtags": "/hashtags Nigerian food recipes",
    "pov": "/pov toxic talking stage",
    "bio": "/bio lifestyle creator",
    "script": "/script how I saved my first 100k",
    "trends": "/trends relationship content"
}


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

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

I write viral Nigerian TikTok content that actually makes people stop scrolling.

Commands:
/hooks [topic] — scroll-stopping opening lines
/captions [topic] — captions people will copy
/hashtags [topic] — reach the right audience
/pov [topic] — POV ideas that feel real
/bio [niche] — bios that get follows
/script [idea] ⭐ Pro
/trends [niche] ⭐ Pro

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

/plan — check your plan
/upgrade — go Pro""")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active\nExpires: {get_pro_expiry(user_id)}\n\nUnlimited access to all commands.")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"🆓 Free Plan\nUses left today: {remaining}/{FREE_LIMIT}\n\nUpgrade to Pro for ₦2,000/month → /upgrade")

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

                cur.execute("SELECT COUNT(*) AS free FROM users WHERE plan='free' OR plan IS NULL")
                free = cur.fetchone()["free"]

            send_message(chat_id, f"""📊 TikGenius Stats

👥 Total Users: {total}
💎 Pro Users: {pro}
🆓 Free Users: {free}""")
        finally:
            release_db(conn)

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)

        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you get:
✅ Unlimited hooks, captions, hashtags, POVs, bios
✅ Full video scripts (/script)
✅ Weekly trend ideas (/trends)
✅ No daily limits ever

Pay here:
{link}

Activation is automatic after payment ✅""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try again in a moment.")

    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 This is a Pro feature.\n\nUpgrade for ₦2,000/month to unlock /script and /trends:\n{link if link else '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You've used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro for unlimited access:\n{link if link else '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking..."])))

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today.\n\nGo Pro for ₦2,000/month → /upgrade")

    else:
        send_message(chat_id, "Unknown command. Use /start to see everything.")

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
        amount = data.get("amount")
        metadata = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            expires = activate_pro(telegram_id)
            send_message(
                telegram_id,
                f"🎉 Payment confirmed! Welcome to Pro.\n\nYour access is active till {expires}\n\nUnlimited scripts, trends, hooks, captions — everything unlocked ✅\n\nTry /script or /trends now."
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
