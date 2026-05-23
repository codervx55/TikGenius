import os
import hmac
import hashlib
import random
from datetime import datetime, timedelta

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
    print("Database pool initialized")

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

# ========================= AI SYSTEM PROMPTS =========================

TIKTOK_SYSTEM_PROMPT = """
You are TikGenius. You write viral TikTok content for Nigerian creators.

You grew up in Nigeria. You know the real Nigerian internet — Nigerian TikTok, Nigerian Twitter, Nigerian WhatsApp status. You know how Nigerians talk when they are venting, flexing, heartbroken, broke, grateful, or just chaotic.

YOUR VOICE sounds like this:
- "omo this life ehn 😭"
- "God when? like genuinely when?"
- "the way I just dey laugh so I no go cry"
- "e don do for me honestly"
- "nobody prepared me for this level of stress abeg"
- "soft life no be by force but I want am sha"
- "bro I swear to God this country 😭😭"
- "my village people don wake up again"
- "e be like say God dey punish me specifically"
- "I just dey look my life like 👁️👄👁️"
- "na so e be"
- "e pain me but I go do am again"
- "the audacity ehn. the effrontery. the liver."

THE TWIST PATTERN — what makes content go viral:
Every piece must have a setup and a flip. Line 1 builds the expectation. Line 2 destroys it with humour, pain, or Nigerian chaos.
- "God provide for your children... until I steal? 😭"
- "healing era activated... then NEPA took light 😭"
- "I chose peace... peace no choose me back"
- "soft life loading... no data 😂"
- "God said be patient... e don do, how long exactly?"
- "main character energy... for another person story 😂"
- "I'm unbothered... I dey lie, I very bothered 😭"

STRICT RULES:
- Think in Nigerian, write in Nigerian
- Mix English and Pidgin naturally — the way creators actually do it, not forced
- Use "ehn", "sha", "abeg", "omo", "e don do", "na", "dey", "wetin", "wahala", "shey", "abi" where they fit
- Short. Punchy. Emotional. No padding. No filler.
- NEVER start with "here are", "sure", "of course", "as a Nigerian creator", "I'd be happy"
- NEVER sound like a motivational quote page or an AI
- Every output must be ready to copy-paste directly to TikTok

THE GOLDEN RULE: A Nigerian creator should read it and say "e be like say na me type this" — that is when you have done your job.
"""

X_SYSTEM_PROMPT = """
You are XGenius. You write viral Twitter/X content for smart, witty Nigerians.

You understand Nigerian Twitter (now X) — the culture, the jokes, the hot takes, the threads that go viral, the one-liners that get thousands of retweets. You know how Nigerian Twitter thinks.

YOUR VOICE on X sounds like this:
- Sharp, bold, no-nonsense
- Witty with a twist — say the thing everyone thinks but nobody says out loud
- Confident. Unbothered. Sometimes provocative but always smart.
- Understands Nigerian reality — NEPA, hustle, soft life dreams, relationship drama, family pressure, this economy

THE TWIST PATTERN for X:
- "They say work hard and succeed. Nobody told me hard work and success are not the same thing."
- "Nigerian parents will stress you about your future then stress you when you try to build it."
- "God has a plan for your life. Your plan and His plan are two completely different documents."
- "You are not lazy. You are exhausted. There is a difference and Nigeria made sure you never know which one."
- "Soft life is not arrogance. It is the reward for surviving this country."

STRICT RULES FOR X:
- Proper English only. No Pidgin. X audience is global + Nigerian professionals.
- Every tweet under 280 characters
- Punchy, bold, quotable — the kind people screenshot and repost
- One clear idea per tweet — no rambling
- Use emojis sparingly — only where they add punch, not decoration
- NEVER sound like a motivational poster or a life coach
- NEVER start with "here are", "sure", "of course"

THE GOLDEN RULE: Someone should read it and immediately want to quote-tweet it or send it to their group chat.
"""

# ========================= PROMPTS =========================

TIKTOK_PROMPTS = {

    "hooks": """Topic: {topic}

Write 10 TikTok opening hooks for a Nigerian creator posting about "{topic}".

A hook must stop the scroll in under 2 seconds. Use the SETUP + TWIST — start with something relatable, flip it with Nigerian humour, pain, or chaos.

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
- Use the setup + twist (first part builds, second part flips it)
- Mix English and Pidgin naturally — not forced
- Be under 15 words
- Make someone stop scrolling and need to see what comes next

Number them 1-10. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 TikTok captions a Nigerian creator would use for a video about "{topic}".

The secret is the TWIST — line 2 flips line 1 with dark humour, painful truth, or Nigerian chaos. Must sound NIGERIAN, not translated English.

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
- Sound like someone typed it fast on their phone
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
- Mix English and Pidgin naturally
- Make the viewer think "e be like say na me this 😭"

Number them 1-10. Nothing else.""",

    "hashtags": """Topic: {topic}

Create 5 hashtag sets for a Nigerian TikTok creator posting about "{topic}".

Each set must have exactly 6 hashtags that MIX:
- 1-2 BROAD tags (big reach: #TikTok #foryoupage #fyp)
- 2-3 NICHE tags (specific to the topic and what people actually search)
- 1-2 NIGERIAN tags (#NigerianTikTok #LagosTikTok #Naija #NaijaCreator #NigerianTwitter etc.)

Think about WHO is searching for this content and WHAT they actually type.

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
- Have Nigerian personality — real, funny, a little chaotic
- Use the setup + twist where it fits
- Tell people WHO you are and WHY to follow immediately
- Sound like a human, not a company profile

Number them 1-8. Nothing else.""",

    "script": """Topic: {topic}

Write a full TikTok video script for a Nigerian creator making a video about "{topic}".

Under 60 seconds when spoken naturally (130-150 words max).

Use this exact format:

[HOOK] — One line. Nigerian voice. Setup + twist. Stops the scroll in 2 seconds.
[BODY] — Main content in short punchy sentences. Write exactly how a Nigerian creator SPEAKS on camera. Use Pidgin where it fits naturally. Real emotion, real story, real voice. No essay writing.
[PUNCHLINE] — The one line wey go make people screenshot. The hardest twist. The thing they go send to their group chat.
[CTA] — One natural question or statement that makes people comment, share, or save.

Rules:
- Think in Nigerian, write in Nigerian
- Mix English and Pidgin the way creators naturally do
- The punchline must be unforgettable
- No motivational quote energy — real, raw, Nigerian

Write the full script for "{topic}" now. Nothing else.""",

    "trends": """Niche: {topic}

Give 8 specific TikTok video ideas that a Nigerian creator in the "{topic}" space can film RIGHT NOW and go viral with.

Each idea must feel like something a real Nigerian creator in Lagos or Abuja would actually film.

Think about what Nigerian audiences save and share:
- Hustle reality vs the dream
- Relationship truth that hits
- Faith + struggle
- Soft life vs real life
- Family pressure (Nigerian parents energy)
- Glow up with receipts — before and after that feels real

Each idea title and hook must use the SETUP + TWIST pattern.

Format each idea exactly like this:

Idea [number]: [Title — with the Nigerian twist in it]
Hook: [Exact first line or on-screen text — Nigerian voice, setup + twist]
Why it works: [1-2 sentences on why Nigerian viewers go save or share this]

---

No intro. No outro. 8 ideas only."""
}

X_PROMPTS = {

    "captions": """Topic: {topic}

Write 10 powerful Twitter/X posts for a Nigerian creator posting about "{topic}".

The secret to viral Nigerian X content is saying the thing everyone thinks but nobody says — with confidence, wit, and a twist that makes people screenshot it.

Study these examples of great Nigerian X energy:
- "They say work hard and succeed. Nobody told me hard work and success are not the same thing."
- "Nigerian parents will stress you about your future then stress you when you try to build it."
- "God has a plan for your life. Your plan and His plan are two completely different documents."
- "You are not lazy. You are exhausted. There is a difference and Nigeria made sure you never know which one."
- "Soft life is not arrogance. It is the reward for surviving this country."
- "The people who doubted you quietly are the same people who will loudly celebrate you. Watch."
- "Stop explaining yourself to people who are not paying your bills or your peace of mind."
- "This economy will humble you. Your mindset will save you. Prayer will carry you."

Write 10 ORIGINAL X posts about "{topic}" — each must:
- Be under 280 characters
- Say one clear, bold, quotable thing
- Use the setup + twist — start with the expected, flip it with truth or wit
- Sound like a smart, confident Nigerian — not a life coach, not an AI
- Be the kind of post someone screenshots and sends to their group chat
- Proper English only — no Pidgin for X

Number them 1-10. Nothing else.""",

    "hooks": """Topic: {topic}

Write 10 strong Twitter/X opening lines for a Nigerian creator posting about "{topic}".

A great X hook makes someone stop scrolling and read the rest of the thread or post. It says something bold, surprising, or uncomfortably true.

Study these examples:
- "Nobody talks about the loneliness of building something from nothing."
- "The most dangerous thing in Nigeria is having a dream and no support system."
- "There is a version of you that gave up too early. Do not become that person."
- "Nigerian parents did not raise you to be happy. They raised you to survive."
- "You will not find peace chasing things that were never meant for you."
- "The glow up is real. Nobody shows you what they sacrificed to get there."
- "Stop performing strength. It is okay to admit this is hard."

Write 10 ORIGINAL X hooks for "{topic}" — each must:
- Be under 15 words
- Say something bold, true, or surprising
- Make the reader stop and think "wait, say that again"
- Proper English, no Pidgin
- No quotation marks around the hook itself

Number them 1-10. One per line. Nothing else.""",

    "threads": """Topic: {topic}

Write a full Twitter/X thread for a Nigerian creator posting about "{topic}".

A great Nigerian X thread:
- Opens with a hook tweet that stops the scroll
- Builds the story or argument tweet by tweet
- Has a twist or revelation midway
- Ends with a punchline or call to action people will retweet

Format:
Tweet 1 (HOOK): [The opening — bold, surprising, or uncomfortably true]
Tweet 2: [Build the story or context]
Tweet 3: [Go deeper — the real truth]
Tweet 4: [The twist or revelation]
Tweet 5: [The lesson or takeaway]
Tweet 6 (CLOSE): [The punchline or CTA — make them retweet or reply]

Rules:
- Each tweet under 280 characters
- Proper English, no Pidgin
- Every tweet must earn the next one — no filler
- The close must be quotable

Write the full thread for "{topic}" now. Nothing else."""
}

# ========================= BAD INTROS =========================
BAD_INTROS = [
    "here are", "sure!", "sure,", "of course", "here is",
    "as a nigerian", "great choice", "great!", "absolutely",
    "happy to", "i'd be happy", "let me", "below are",
    "i'll write", "i will write", "these are", "i've written"
]

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic, platform="tiktok"):
    if platform == "x":
        system = X_SYSTEM_PROMPT
        prompt_template = X_PROMPTS.get(mode, X_PROMPTS["captions"])
    else:
        system = TIKTOK_SYSTEM_PROMPT
        prompt_template = TIKTOK_PROMPTS.get(mode, TIKTOK_PROMPTS["captions"])

    prompt = prompt_template.format(topic=topic)

    def call_groq(sys_prompt, user_prompt, temp=0.92):
        return groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temp,
            max_tokens=1200
        ).choices[0].message.content.strip()

    try:
        raw = call_groq(system, prompt)

        first_line = raw.split("\n")[0].lower()
        if any(bad in first_line for bad in BAD_INTROS):
            raw = call_groq(
                "Rewrite. No intro. No explanation. No preamble. Start immediately with number 1.",
                prompt,
                temp=0.95
            )

        lines = []
        for line in raw.split("\n"):
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
        return "⚠️ Brain dey buffer. Try again in 10 seconds."

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

# ========================= LOADING MESSAGES =========================
LOADING = {
    "hooks": [
        "🧠 Cooking hooks wey go stop the scroll...",
        "🔥 Finding the angle wey go make them watch...",
        "👀 This one go touch chest, hold on"
    ],
    "captions": [
        "💅 Adding the twist that makes people screenshot...",
        "😭 Cooking emotional damage with a punchline...",
        "🪄 Making it sound like you typed it at 1am"
    ],
    "pov": [
        "🎥 Setting up the scene... and the twist 👀",
        "🍿 This POV fit mad, give me a sec",
        "👀 Drama loading... 🇳🇬"
    ],
    "hashtags": [
        "📊 Finding tags wey TikTok algorithm go love",
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
        "🔥 Trend ideas loading for your niche",
        "👀 FYP angle loading... 🇳🇬"
    ],
    "threads": [
        "🧵 Building the thread that go blow...",
        "✍️ Writing something people go screenshot...",
        "🔥 X thread loading... this one go get retweets"
    ]
}

EXAMPLES = {
    "hooks":    "/hooks when you're broke but still acting unbothered",
    "captions": "/captions data finish at the worst time",
    "hashtags": "/hashtags Nigerian food recipes Lagos",
    "pov":      "/pov your Nigerian parents when you fail one exam",
    "bio":      "/bio Nigerian lifestyle and soft life creator",
    "script":   "/script how I saved my first 100k earning in Nigeria",
    "trends":   "/trends Nigerian relationship and dating content",
    "threads":  "/threads x hustle culture in Nigeria is a lie"
}

TIKTOK_COMMANDS = {"/hooks", "/captions", "/pov", "/hashtags", "/bio", "/script", "/trends"}
X_COMMANDS = {"/xtweets", "/xhooks", "/xthread"}
PRO_COMMANDS = {"/script", "/trends", "/xthread"}

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
    first_name = message.get("from", {}).get("first_name", "Creator")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts = text.split(maxsplit=1)
    command = parts[0].lower().split("@")[0]
    topic = parts[1].strip() if len(parts) > 1 else ""

    # ── /start ──
    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

I write viral content for Nigerian creators — TikTok AND Twitter/X.
The kind wey people screenshot, save, and tag their friends.

━━━ TIKTOK ━━━
/hooks [topic]
/captions [topic]
/pov [topic]
/hashtags [topic]
/bio [niche]
/script [idea] ⭐ Pro

━━━ TWITTER / X ━━━
/xtweets [topic]
/xhooks [topic]
/xthread [topic] ⭐ Pro

━━━ OTHER ━━━
/trends [niche] ⭐ Pro
/plan — check your plan
/upgrade — go Pro

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

Be specific with your topic for better results 🔥
❌ Bad: /captions data
✅ Good: /captions data finish when I needed it most""")

    # ── /plan ──
    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active\nExpires: {get_pro_expiry(user_id)}\n\nUnlimited access to everything.")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"🆓 Free Plan\nUses left today: {remaining}/{FREE_LIMIT}\n\nUpgrade to Pro for ₦2,000/month → /upgrade")

    # ── /upgrade ──
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you get:
✅ Unlimited TikTok hooks, captions, POVs, hashtags, bios
✅ Full video scripts (/script)
✅ Trend ideas (/trends)
✅ Twitter/X threads (/xthread)
✅ No daily limits ever

Pay here:
{link or "Try again in a moment"}

Activation is automatic after payment ✅""")

    # ── /activatepro ──
    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    # ── /stats ──
    elif command == "/stats":
        if str(user_id) != ADMIN_ID:
            send_message(chat_id, "❌ Not allowed.")
            return jsonify({"ok": True})
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM users")
                total = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS pro FROM users WHERE plan='pro' AND expires >= CURRENT_DATE")
                pro = cur.fetchone()["pro"]
                cur.execute("SELECT COUNT(*) AS free FROM users WHERE plan='free' OR plan IS NULL")
                free = cur.fetchone()["free"]
            send_message(chat_id, f"📊 TikGenius Stats\n\n👥 Total Users: {total}\n💎 Pro Users: {pro}\n🆓 Free Users: {free}")
        finally:
            release_db(conn)

    # ── TikTok commands ──
    elif command in TIKTOK_COMMANDS:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 This is a Pro feature.\n\nUpgrade for ₦2,000/month:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get(mode, f'/{mode} your topic here')}")
            return jsonify({"ok": True})

        if len(topic.split()) == 1:
            send_message(chat_id, f"⚠️ Topic too short — add more detail for better results.\n\n❌ /{mode} {topic}\n✅ /{mode} {topic} [add the feeling or situation]\n\nExample:\n{EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You've used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro for unlimited access:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Cooking..."])))
        result = ask_ai(mode, topic, "tiktok")
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today.\n\nGo Pro for ₦2,000/month → /upgrade")

    # ── Twitter/X commands ──
    elif command in X_COMMANDS:
        mode_map = {"/xtweets": "captions", "/xhooks": "hooks", "/xthread": "threads"}
        mode = mode_map[command]

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 This is a Pro feature.\n\nUpgrade for ₦2,000/month:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if len(topic.split()) == 1:
            send_message(chat_id, f"⚠️ Topic too short — add more detail for better results.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You've used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Cooking..."])))
        result = ask_ai(mode, topic, "x")
        send_message(chat_id, f"✨ XGenius\n\n{result[:3800]}")

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
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
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
            send_message(telegram_id,
                f"🎉 Payment confirmed! Welcome to Pro.\n\nAccess active till {expires}\n\nEverything unlocked ✅\n\nTry /script, /trends, or /xthread now.")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
