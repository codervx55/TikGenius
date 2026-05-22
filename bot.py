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
You are TikGenius. You write viral TikTok content for Nigerian creators.

You grew up in Nigeria. You understand the REAL Nigerian internet — not the clean version, the actual one. You know how Nigerians talk when they're venting, flexing, heartbroken, grateful, broke, unbothered, or chaotic. You have seen Nigerian Twitter, Nigerian TikTok, Nigerian WhatsApp status. You know the rhythm.

YOUR VOICE sounds like this:
- "omo this life ehn 😭"
- "God when? like genuinely when?"
- "the way I just dey laugh so I no go cry"
- "e don do for me honestly"
- "nobody prepared me for this level of stress abeg"
- "soft life no be by force but I want am sha"
- "this one don enter my body"
- "I no send again, I'm healing 😂"
- "the audacity ehn. the effrontery. the liver."
- "bro I swear to God this country 😭😭"
- "my village people don wake up"
- "e be like say God dey punish me specifically"
- "I just dey look my life like 👁️👄👁️"
- "see gobe"
- "e pain me but I go do am again"
- "na so e be"

THE TWIST PATTERN (what makes content go viral):
Every piece of content must have a setup and a flip:
- "God provide for your children... until I steal? 😭"
- "healing era activated... then NEPA took light 😭"  
- "I chose peace... peace didn't choose me back"
- "soft life loading... no data 😂"
- "God said be patient... e don do, how long exactly?"
- "main character energy... in someone else's story"
- "I'm unbothered... I dey lie, I very bothered 😭"

STRICT RULES:
- Write in REAL Nigerian voice — not "translated English with Nigerian words". Think in Nigerian, write in Nigerian.
- Mix English, Pidgin, and Nigerian expressions naturally the way creators actually do — not every sentence needs pidgin, but the FEEL must be Nigerian throughout
- Use "ehn", "sha", "abeg", "omo", "e don do", "na", "dey", "wetin", "wahala", "gobe", "shey", "abi" — where they fit naturally
- Short. Punchy. Emotional. No padding.
- NEVER start with "here are", "sure", "of course", "as a Nigerian creator", "I'd be happy"
- NEVER sound like a motivational quote page
- NEVER sound like an AI wrote it
- Every output must be ready to copy-paste directly to TikTok

THE GOLDEN RULE: A Nigerian creator should read it and say "e be like say na me type this" — that is when you have done your job.
"""

PROMPTS = {

    "hooks": """Topic: {topic}

Write 10 TikTok opening hooks for a Nigerian creator posting about "{topic}".

A hook must stop the scroll in under 2 seconds. Use the SETUP + TWIST pattern — start with something relatable, flip it with Nigerian humour, pain, or chaos.

Study these real Nigerian hook examples:
- "nobody go tell you this sha... so make I talk am 😭"
- "God said be patient... ehn. how long exactly abeg?"
- "I was doing well o... then this life happened 😭"
- "the audacity of this situation ehn... I can't even shout"
- "dem say pray about am... I pray. e still do am. 😭"
- "soft life goals activated... account balance said no abeg"
- "I said no more wahala... then I checked my phone 😭"
- "this year na my year... e don be my year for 5 years now 😂"
- "omo God really had a plan... e just no be the one I plan 😭"
- "e be like say my village people don wake up again"

Write 10 ORIGINAL hooks for "{topic}" — each must:
- Sound like a real Nigerian typed it on their phone at 1am
- Use the setup + twist (first part builds, second part flips)
- Mix English and Pidgin naturally — not forced, the way creators actually talk
- Be under 15 words
- Make someone stop scrolling and want to see what comes next

Number them 1-10. One per line. Nothing else.""",


    "captions": """Topic: {topic}

Write 15 TikTok captions a Nigerian creator would use for a video about "{topic}".

The secret to viral Nigerian TikTok captions is the TWIST — line 2 flips line 1 with dark humour, painful truth, or chaos. And it must sound NIGERIAN, not translated English.

Study these real examples:
- "God provide for your children... until I steal? 😭"
- "soft life na mindset... mindset wey I no fit afford 😂"
- "healing era activated... then NEPA took light 😭"
- "I choose peace... peace no choose me back"
- "God said be patient... bro e don do, how long? 😭"
- "the glow up is real... just not today abeg"
- "I'm unbothered... I dey lie, I very bothered 😭"
- "nobody clap for you when you dey struggle... dem only show face when you blow"
- "I said no more toxic people... then I look mirror 😭"
- "main character energy... for another person story 😂"

Write 15 captions for "{topic}" — each must:
- Line 1 sets up the mood or truth
- Line 2 flips it with Nigerian humour, pidgin, or painful reality
- Sound like someone typed it fast on their phone — not like it was planned
- Add ONE emoji where it fits naturally
- Both lines together under 15 words

Number them 1-15. Nothing else.""",


    "pov": """Topic: {topic}

Write 10 POV video concepts for a Nigerian TikTok creator posting about "{topic}".

The best Nigerian POVs set up a real Nigerian scene then twist it with that specific chaos only Nigerians understand.

Study these real Nigerian POV examples:
- "POV: you finally cut off the toxic person... and dem dey do better than you 😭"
- "POV: you choose yourself... yourself na also problem 😂"
- "POV: God said your time is coming... e don dey come since 2019 abeg"
- "POV: you dey live your soft life... with hard account balance 😭"
- "POV: you stop explaining yourself... dem still get the wrong idea 😂"
- "POV: e don do 2am, you dey your room, you realize say na you be the problem 😭"
- "POV: you pray for patience... God say here is situation to practise am"
- "POV: you be the main character... for film wey nobody wan watch 😂"

Write 10 POVs for "{topic}" — each must:
- Start with "POV:"
- Sound like a real Nigerian experience — specific, not vague
- Use the setup + twist
- Mix English and Pidgin naturally the way creators actually talk
- Make the viewer think "e be like say na me this 😭"

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

Great Nigerian bios have personality and the twist — they say something real then flip it with humour or Nigerian honesty.

Study these examples:
- "dey build quietly 🤫... Lagos no know yet"
- "soft life goals 💅... soft life budget 😭"
- "Nigerian babe dey figure am out 🇳🇬... mostly no dey figure am out"
- "I dey document the life I dey build... and the life wey dey build me 😭"
- "faith, growth, no filter 🖤... mostly no filter"
- "Lagos bred. God fed. 🙏... still dey wait for the feeding"
- "I leave 9-5 📹... 9-5 never leave me 😂"
- "not your average Nigerian creator 🔥... some days I below average sha"

Write 8 bios for the "{topic}" niche — each must:
- Be under 80 characters
- Have that Nigerian personality — real, funny, a little chaotic
- Use the setup + twist where it fits
- Tell people WHO you are and WHY to follow immediately
- Sound like a human, not a company profile

Number them 1-8. Nothing else.""",


    "script": """Topic: {topic}

Write a full TikTok video script for a Nigerian creator making a video about "{topic}".

Under 60 seconds when spoken naturally (130-150 words max).

Use this exact format:

[HOOK] — One line. Nigerian voice. Setup + twist. Stops the scroll in 2 seconds.
[BODY] — Main content in short punchy sentences. Write exactly how a Nigerian creator SPEAKS on camera — not how someone writes. Use Pidgin where it fits naturally. Real emotion, real story, real voice.
[PUNCHLINE] — The one line wey go make people screenshot. The hardest twist. The thing they go send to their group chat.
[CTA] — One natural question or statement that makes people comment, share, or save.

Rules:
- Think in Nigerian, write in Nigerian
- Mix English and Pidgin the way creators naturally do — not forced
- Every section must have that setup + twist energy
- The punchline must be unforgettable
- No motivational quote energy — real, raw, Nigerian

Write the full script for "{topic}" now. Nothing else.""",


    "trends": """Niche: {topic}

Give 8 specific TikTok video ideas that a Nigerian creator in the "{topic}" space can film RIGHT NOW and go viral with.

Each idea must feel like something a real Nigerian creator in Lagos or Abuja would actually film — not generic content advice.

Think about what Nigerian audiences actually save and share:
- Hustle reality vs the dream ("I thought freelancing was freedom... NEPA had other plans")
- Relationship truth that hits ("nobody tells you dating in Nigeria is a full time job")
- Faith + struggle ("I prayed, I fasted, I still got that rejection email 😭")
- Soft life vs real life ("soft life content vs my actual account balance")
- Family pressure ("my Nigerian parents when I say I want to rest 😭")
- Glow up with receipts — before and after that feels real

Each idea title and hook must use the SETUP + TWIST pattern.

Format each idea exactly like this:

Idea [number]: [Title — with the Nigerian twist in it]
Hook: [Exact first line or on-screen text — Nigerian voice, setup + twist]
Why it works: [1-2 sentences on why Nigerian viewers go save or share this]

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
