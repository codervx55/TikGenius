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
        1, 10, DATABASE_URL, cursor_factory=RealDictCursor
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


# ─────────────────────────────────────────────
#  AI PROMPTS
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """
You are TikGenius — a viral content engine for Nigerian TikTok creators.

You write like a real Lagos creator who has mastered the art of the TWIST — that second line or second half that flips everything and makes people screenshot, save, or tag their friend.

The pattern that makes Nigerian TikTok go viral:
- Line 1 sets up an emotion, truth, or expectation
- Line 2 FLIPS it — with dark humour, painful reality, or an unexpected punchline

Examples of the twist in action:
- "God provide for your children... until I steal? 😭"
- "I chose peace... peace didn't choose me back"
- "soft life loading... no data 😂"
- "healing era activated... then NEPA took light 😭"
- "God said be patient... bro it's been 25 years 😭"
- "I'm unbothered... I'm lying, I'm very bothered"
- "main character energy... in someone else's story"
- "the glow up is real... just not today"

VOICE RULES:
- Always use the setup + twist pattern
- Short and punchy — the whole thing under 15 words unless it's a script
- Natural Nigerian voice — slang only where it fits, not forced every line
- Never write "here are", "sure!", "of course", "as a Nigerian creator"
- Start with the EMOTION or SCENE, never the topic word itself
- Each line must feel ready to copy-paste directly into TikTok

THE GOLDEN RULE: If a Nigerian creator reads it and says "this is exactly what I wanted to say" — you did your job.
"""

PROMPTS = {

    "hooks": """Topic: {topic}

Write 10 TikTok opening hooks for a Nigerian creator posting about "{topic}".

A hook stops the scroll in under 2 seconds. The best Nigerian hooks use the SETUP + TWIST pattern — they start with something familiar then flip it with humour, pain, or irony.

Study these hook examples:
- "Nobody will tell you this... so I will 😭"
- "God said be patient... bro how long exactly?"
- "I was doing so well... then this happened 😭"
- "POV: you finally chose yourself... and immediately regretted it 😂"
- "The audacity of this life sha... I can't even be mad"
- "They said pray about it... I prayed. Still broke. 😭"
- "Soft life goals activated... account balance said no"
- "I said no more toxic situations... then checked my phone 😭"
- "This year is my year... it's been my year for 5 years 😂"
- "God really had a plan... just not the one I planned"

Write 10 ORIGINAL hooks for "{topic}" — each must:
- Use the setup + twist pattern (first part sets up, second part flips it)
- Be under 15 words total
- Make someone stop scrolling AND want to see what happens next
- Sound like a real Nigerian typed it at 1am

Number them 1-10. One per line. Nothing else.""",


    "captions": """Topic: {topic}

Write 15 TikTok captions a Nigerian creator would use for a video about "{topic}".

The SECRET to a viral Nigerian TikTok caption is the TWIST — a second line that flips the first on its head, adds dark humour, or hits unexpectedly hard.

Study these examples:
- "God provide for your children... until I steal? 😭"
- "soft life is a mindset... a mindset I can't afford 😂"
- "healing era activated... then NEPA took light 😭"
- "I chose peace... peace didn't choose me back"
- "God said be patient... bro it's been 25 years 😭"
- "the glow up is real... just not today"
- "I'm unbothered... I'm lying, I'm very bothered"
- "nobody clap for you when you're struggling... they only show up when you blow"
- "I said no more toxic people... then I looked in the mirror 😭"
- "main character energy... in someone else's story"

Write 15 captions for "{topic}" using this TWIST pattern:
- Line 1: sets up a mood, truth, or expectation
- Line 2: flips it, adds dark humour, or lands the real emotion
- Both lines together under 15 words
- Add ONE emoji at the end where it fits naturally
- Sound like a real Nigerian, not a motivational poster

Number them 1-15. Nothing else.""",


    "pov": """Topic: {topic}

Write 10 POV video concepts for a Nigerian TikTok creator posting about "{topic}".

The best Nigerian TikTok POVs use the TWIST — they set up a relatable scene then flip it with dark humour, painful truth, or an unexpected ending.

Study these examples:
- "POV: you finally cut off the toxic person... and they're doing better than you 😭"
- "POV: you chose yourself... yourself is also a mess 😂"
- "POV: God said your time is coming... it's been coming since 2019"
- "POV: you're living your soft life... with a hard account balance 😭"
- "POV: you stopped explaining yourself to people... they still have the wrong idea 😂"
- "POV: it's 2am, you're in your room, and you realize you've been the problem all along 😭"
- "POV: you prayed for patience... God said here's a situation to practice it"
- "POV: you're the main character... in a story nobody wants to watch"

Write 10 POVs for "{topic}" — each must:
- Start with "POV:"
- Set up a scene then twist it with humour, pain, or irony
- Be one or two sentences max
- Make the viewer think "this is literally me 😭"

Number them 1-10. Nothing else.""",


    "hashtags": """Topic: {topic}

Create 5 hashtag sets for a Nigerian TikTok creator posting about "{topic}".

Each set must have exactly 6 hashtags that MIX:
- 1-2 BROAD tags (big reach: #TikTok #foryoupage #fyp)
- 2-3 NICHE tags (specific to the topic and what people actually search)
- 1-2 NIGERIAN tags that Nigerian viewers use (#NigerianTikTok #LagosTikTok #Naija #NaijaCreator etc.)

Think about WHO is searching for this content and WHAT they actually type into TikTok search.

Format exactly like this — nothing else:
Set 1: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 2: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 3: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 4: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6
Set 5: #tag1 #tag2 #tag3 #tag4 #tag5 #tag6""",


    "bio": """Topic/Niche: {topic}

Write 8 TikTok bio options for a Nigerian creator in the "{topic}" niche.

The best Nigerian TikTok bios use the TWIST — they say something real then flip it with humour or personality.

Study these examples:
- "building quietly 🤫... Lagos doesn't know yet"
- "soft life goals 💅... soft life budget 😭"
- "Nigerian girl figuring it out 🇳🇬... mostly not figuring it out"
- "I document the life I'm building... and occasionally the life that's building me 😭"
- "faith, growth, no filter 🖤... mostly no filter"
- "Lagos bred. God fed. 🙏... still waiting on the feeding"
- "left the 9-5 📹... the 9-5 has not left me 😂"
- "not your average Nigerian creator 🔥... I'm below average some days"

Write 8 bios for the "{topic}" niche — each must:
- Be under 80 characters
- Use the setup + twist or personality flip where possible
- Tell people WHO you are and WHY to follow in one breath
- Sound human, funny, real — not like a LinkedIn profile

Number them 1-8. Nothing else.""",


    "script": """Topic: {topic}

Write a full TikTok video script for a Nigerian creator making a video about "{topic}".

Under 60 seconds when spoken naturally (130-150 words max).

Use this exact format:

[HOOK] — One line. Sets up the topic then TWISTS it immediately to grab attention.
[BODY] — The main content. Short punchy sentences. Real Nigerian voice. Story, rant, tips, or truth — delivered like a creator speaks on camera, not like an essay.
[PUNCHLINE] — One unforgettable line the viewer will screenshot or repeat. The hardest twist of the whole video.
[CTA] — One question or statement that forces a comment, share, or save.

Rules:
- Write how a real Nigerian SPEAKS on camera
- Every section must have the setup + twist energy
- The hook must stop the scroll in 2 seconds
- The punchline must be the kind of line people send to their group chat
- The CTA must feel natural, not forced

Write the full script for "{topic}" now. Nothing else.""",


    "trends": """Niche: {topic}

Give 8 specific TikTok video ideas that a Nigerian creator in the "{topic}" space can film RIGHT NOW and go viral with.

Each idea must use the SETUP + TWIST pattern — the hook should promise one thing and deliver something funnier, more painful, or more unexpected.

For each idea think about:
- What's working on TikTok (storytime, "things nobody tells you", POV, day-in-my-life, reaction, opinion take, tutorial, transformation)
- What Nigerian audiences save and share (hustle reality, relationship truth, faith + struggle, soft life vs real life, family pressure, Lagos life, glow ups)
- What makes someone tag their friend in the comments

Format each idea exactly like this:

Idea [number]: [Catchy title — use the twist pattern in the title itself]
Hook: [The exact first line or on-screen text — must have the setup + twist]
Why it works: [1-2 sentences on why Nigerian viewers will save or share this]

---

No intro. No outro. 8 ideas only."""
}


BAD_INTROS = [
    "here are", "sure!", "sure,", "of course", "here is",
    "as a nigerian", "great choice", "great!", "absolutely",
    "happy to", "i'd be happy", "let me", "below are",
    "i'll write", "i will write", "these are"
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

        first_line = raw_output.split("\n")[0].lower()
        if any(bad in first_line for bad in BAD_INTROS):
            raw_output = call_groq(
                "Rewrite. No intro. No explanation. No preamble. Raw Nigerian TikTok content only. Start immediately with number 1.",
                prompt,
                temp=0.95
            )

        lines = []
        for line in raw_output.split("\n"):
            line = line.strip()
            if not line:
                lines.append("")
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
            json=payload, headers=headers, timeout=20
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
        "💅 Adding the twist that makes people screenshot...",
        "😭 Cooking emotional damage with a punchline...",
        "🪄 Making it look like you typed it at 1am"
    ],
    "pov": [
        "🎥 Setting up the scene... and the twist 👀",
        "🍿 This POV fit mad, give me a sec",
        "👀 Drama loading... 🇳🇬"
    ],
    "hashtags": [
        "📊 Finding tags TikTok algorithm go love",
        "🚀 Mixing reach tags with Nigerian tags",
        "🔥 Algorithm food loading..."
    ],
    "bio": [
        "✨ Bio loading... with the twist that gets follows",
        "📱 Creating follower magnet for your profile",
        "🪄 Profile glow-up in progress"
    ],
    "script": [
        "🎬 Writing the hook, the body, and the punchline...",
        "📝 Script loading... this one go make them watch till end",
        "🔥 60-second banger cooking 🍳"
    ],
    "trends": [
        "📈 Finding exactly what you should film this week",
        "🔥 Trend ideas with twists loading for your niche",
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

I write viral Nigerian TikTok content with that setup + twist energy that makes people screenshot and share.

Commands:
/hooks [topic] — scroll-stopping opening lines
/captions [topic] — captions with the punchline twist
/hashtags [topic] — reach the right audience
/pov [topic] — POV ideas that feel painfully real
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
