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

You understand Nigerian life deeply — the hustle, the struggle, NEPA, soft life dreams, family pressure, broke seasons, glow ups, relationships, faith, and chaos. You write content that hits people in the chest.

YOUR LANGUAGE:
- Clean, simple English that everyone can understand
- Occasionally use ONE natural Nigerian word where it genuinely fits — "NEPA", "soft life", "this country", "wahala" — but never force it
- The goal is: globally understandable, Nigerian at heart

THE TWIST PATTERN — this is what makes content go viral:
Two lines. Line 1 sets up an emotion or truth. Line 2 flips it completely with humour, irony, or painful reality.
The two lines connect with a dash ( — ) or just flow naturally on a new line. NEVER use "..."

GREAT examples — study these and match this exact quality:
- "God will provide for his children — I just didn't expect to be the one stealing 😭"
- "Healing era activated. Then NEPA took the light 😭"
- "I chose peace. Peace clearly did not choose me back"
- "Soft life is a mindset — a mindset I genuinely cannot afford 😂"
- "God said be patient. It has been 25 years 😭"
- "Main character energy — in someone else's story 😂"
- "I'm unbothered. I am lying. I am very bothered 😭"
- "Nobody claps when you're struggling. They show up the moment you blow"
- "The glow up is real. Just not today"
- "I said no more toxic people. Then I looked in the mirror 😭"
- "Rest era activated. The bills did not get the memo 😭"
- "I stopped caring what people think. They're still thinking it anyway 😂"

BAD examples — never write like this:
- "Am tired... e don do" — broken English, not punchy
- "I no get strength... but suffering get" — forced Pidgin, unreadable
- "Healing era... NEPA light..." — ellipsis makes it feel unfinished and lazy

STRICT RULES:
- NEVER use "..." (ellipsis) — use a dash, a full stop, or a new line instead
- Clean English. Simple. Every word earns its place.
- Short — both lines together under 15 words
- ONE emoji where it fits naturally
- Never sound like a motivational poster, a life coach, or an AI
- Never start with "here are", "sure", "of course", "as a Nigerian creator"
- Every output must be ready to copy-paste directly to TikTok

THE GOLDEN RULE: A Nigerian creator should read it and say "this is exactly what I wanted to say."
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

A hook must stop the scroll in under 2 seconds. Use the SETUP + TWIST — start with something relatable, flip it with humour, pain, or irony.

Study these GREAT hook examples — match this quality exactly:
- "Nobody will tell you this. So I will 😭"
- "God said be patient. Bro, how long exactly?"
- "I was doing so well. Then life happened 😭"
- "The audacity of this situation. I cannot even be mad"
- "They said pray about it. I prayed. Nothing changed 😭"
- "Soft life goals activated — account balance said absolutely not"
- "I said no more stress. Then I checked my phone 😭"
- "This year is my year. It has been my year for five years 😂"
- "God really had a plan — just not the one I had in mind 😭"
- "I chose myself. Myself also has problems 😂"

Write 10 ORIGINAL hooks for "{topic}" — each must:
- Use the setup + twist (first part builds, second part flips it)
- Be under 15 words
- Clean simple English — no forced Pidgin
- Make someone stop scrolling and need to see what comes next
- Sound like a real person, not a content checklist

Number them 1-10. One per line. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 TikTok captions for a Nigerian creator posting about "{topic}".

The secret to viral Nigerian TikTok captions is the TWIST — Line 1 sets up an emotion or truth, Line 2 flips it with humour, irony, or painful reality.

Study these GREAT examples carefully — this is exactly the quality and style you must match:
- "God will provide for his children — I just didn't expect to be the one stealing 😭"
- "Healing era activated. Then NEPA took the light 😭"
- "I chose peace. Peace clearly did not choose me back"
- "Soft life is a mindset — a mindset I genuinely cannot afford 😂"
- "God said be patient. It has been 25 years 😭"
- "Main character energy — in someone else's story 😂"
- "I'm unbothered. I am lying. I am very bothered 😭"
- "Nobody claps when you're struggling. They show up the moment you blow"
- "The glow up is real. Just not today"
- "I said no more toxic people. Then I looked in the mirror 😭"
- "This year is my year. It has been my year for five years now 😂"
- "God really had a plan. Just not the one I submitted 😭"
- "I chose myself. Myself also has issues 😭"
- "I stopped caring what people think. They are still thinking it anyway 😂"
- "Rest era activated. The bills did not get the memo 😭"

Write 15 ORIGINAL captions for "{topic}" that match this exact quality — each must:
- Line 1: set up a mood, truth, or expectation clearly
- Line 2: flip it with humour, irony, or painful Nigerian reality
- Clean simple English — globally understandable, Nigerian at heart
- ONE emoji at the end where it fits naturally
- Both lines together under 15 words
- Feel ready to copy-paste directly to TikTok

Number them 1-15. Nothing else.""",

    "pov": """Topic: {topic}

Write 10 POV video concepts for a Nigerian TikTok creator posting about "{topic}".

The best POVs set up a real relatable scene then twist it with humour, painful truth, or irony.

Study these GREAT examples — match this quality exactly:
- "POV: you finally cut off the toxic person. They are doing better than you 😭"
- "POV: you chose yourself. Yourself also has issues 😂"
- "POV: God said your time is coming. It has been coming since 2019"
- "POV: you are living your soft life — with a very hard account balance 😭"
- "POV: you stopped explaining yourself to people. They still have the wrong idea 😂"
- "POV: it is 2am, you are in your room, and you realise you have been the problem all along 😭"
- "POV: you prayed for patience. God sent you a situation to practice it"
- "POV: you are the main character — in a story nobody asked for 😂"

Write 10 POVs for "{topic}" — each must:
- Start with "POV:"
- Set up a specific relatable scene then flip it
- Clean simple English — no forced Pidgin
- One or two sentences max
- Make the viewer think "this is literally me 😭"

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
    "hooks":    "/hooks I prayed for this life — God had a different version in mind",
    "captions": "/captions data finish at the worst time",
    "hashtags": "/hashtags Nigerian food recipes Lagos",
    "pov":      "/pov you finally got everything you prayed for and you're still not happy",
    "bio":      "/bio Nigerian lifestyle and soft life creator",
    "script":   "/script things I wish someone told me before I started hustling alone",
    "trends":   "/trends Nigerian money and hustle creator content",
    "threads":  "/threads x why resting in Nigeria feels like a crime"
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

Be specific — the more real your topic, the harder it hits 🔥

❌ Too vague: /captions tired
✅ Try this: /captions I work so hard but I'm still broke
✅ Try this: /hooks I prayed for this life and I'm still not happy
✅ Try this: /pov you finally made it and nobody who doubted you said sorry""")

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
