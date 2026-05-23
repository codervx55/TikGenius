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

# ========================= PROMPTS =========================
TIKTOK_SYSTEM = """You are the best TikTok content writer for Nigerian creators. You have written hooks and captions that have gone viral millions of times. You understand deeply what makes Nigerian Gen Z stop scrolling — the emotion, the realness, the specific details of Nigerian life.

You understand their world completely: NEPA cutting light at the wrong time, hustling with no help, praying and still struggling, soft life as a goal, family pressure, relationship pain, glow ups, faith, this economy that doesn't make sense.

You write content that feels like it came from a real person — not an AI, not a motivational poster, not a primary school essay. Real. Sharp. Emotional. Human.

YOUR MOST IMPORTANT RULES:
- When a user gives you a short or simple topic, DO NOT produce short or simple output. Expand it. Dig into the emotion behind it. Think about what a Nigerian creator would actually feel and say about that topic — then write from that place.
- Every single line you write must be something a real person would actually say, post, or screenshot.
- NEVER write fragments like "Fear is holding me" or "Happiness is my goal." Write complete thoughts that land with weight.
- NEVER use "..." anywhere. Use a dash ( — ) or start a new sentence instead.
- No Pidgin unless it appears naturally. Clean modern English that Nigerian Gen Z actually uses."""

TIKTOK_PROMPTS = {

"hooks": """A Nigerian TikTok creator wants hooks about: {topic}

Write 10 hooks that are easy to read — not too long, not too short. Each hook should be 1 to 2 complete sentences that feel natural and powerful.

STUDY THESE FOR LENGTH:

"POV: You're lying in bed at night staring at the ceiling, still not where you want to be financially — but you take a deep breath and keep pushing."

"I used to explain my dreams to everyone. Not anymore."

"God didn't bring you this far to leave you here in this season."

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

"pov": """A Nigerian TikTok creator needs POV captions about: {topic}

Write 10 POVs that are easy to read — not too long, not too short. Each one should be 1-2 sentences that put the viewer right in the moment.

STUDY THESE FOR LENGTH:

"POV: You're sitting in danfo stuck in Lagos traffic, wondering why your life feels stuck while the world is moving so fast."

"POV: Your Nigerian parents are calling again asking when you'll settle down and get married, and you're tired of explaining that the world has changed."

"POV: It's 2AM and you're thinking about how far you've come even though you're still not where you want to be financially."

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

# Keep all your other original prompts exactly as they were
"captions": """A Nigerian TikTok creator needs captions about: {topic}

Before you write, think deeply. What is the REAL emotion under this topic? What would a Nigerian actually feel, experience, think at 2am about this? What specific truth would they never say out loud but instantly recognise when they read it?

Write from THAT place. Not surface level. From the gut.

WHAT MAKES A GREAT CAPTION:
A caption makes someone stop, read it twice, save it, or tag a friend. It is short but heavy. It has a setup and a twist — the second sentence says something the first sentence made you not expect. It feels personal. It feels true. It is complete.

STUDY THESE:

"Healing is not linear. Some days you are okay. Some days you are not. Both are valid."
"I used to shrink myself for people who were not even paying attention. Never again."
"God will give you the life you prayed for. Just not in the timeline you imagined. 😭"

NOW write 15 ORIGINAL captions for: {topic}

RULES:
- Every caption must have a TWIST — line 2 must surprise, deepen, or flip line 1
- 1 to 3 sentences maximum per caption
- Every sentence must be complete and meaningful
- Never use "..." — use a dash ( — ) or a full stop
- ONE emoji max per caption, only where it genuinely adds feeling
- Mix emotions — deep, funny, empowering, painfully relatable
- No numbering with dots — use: 1) 2) 3)
- Do not explain. Just write.

1)
2)
3)
4)
5)
6)
7)
8)
9)
10)
11)
12)
13)
14)
15)""",

"hashtags": """Generate 5 strategic TikTok hashtag sets for a Nigerian creator posting about: {topic}

Think about who would actually search for and watch this content. What are they typing? What communities are they in?

Each set must have exactly 7 hashtags mixing:
- 2 massive reach tags (100M+ views): #fyp #foryoupage #tiktok #viral #foryou
- 2 medium reach tags (1M-50M): topic-specific tags people actually search
- 2 niche tags (under 1M): very specific to this exact content
- 1 Nigerian tag: #nigeriantiktok #naija #lagostiktok #naijavibes #naijacreator

Format exactly like this — nothing else, no explanation:
Set 1: #tag #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag #tag""",

"bio": """Write 8 TikTok bios for a Nigerian creator in this niche: {topic}

Think about who this creator is and what would make someone follow them in 2 seconds. A great bio tells people who you are, why to follow, and shows personality — all in under 80 characters.

STUDY THESE:

"building the life I used to dream about 🤫 | tips and real talk"
"your favourite Nigerian big sister 🇳🇬 | faith, growth, no filter"

NOW write 8 ORIGINAL bios for the {topic} niche:
- Under 80 characters each
- Clear personality and content promise in every one
- Complete thoughts — never trailing off
- Never use "..." — use | or a full stop
- Mix tones: inspiring, funny, bold, warm
- Number them: 1) 2) 3)""",

"script": """Write a complete 60-second TikTok script for a Nigerian creator about: {topic}

Think deeply first. What is the most honest, specific, emotionally real angle on this topic for a Nigerian audience? What would make someone watch until the very last second?

Every word in this script must earn its place. Write how a real Nigerian creator actually talks on camera — short sentences, natural rhythm, real emotion.

Structure:

[HOOK — 0 to 3 seconds]
The first thing said or shown. Must stop the scroll immediately. One strong complete sentence. Under 15 words. This is everything.

[BODY — 4 to 45 seconds]
The main content. Short sentences. Natural speaking rhythm. Real Nigerian references where they fit naturally. Build the emotion or the point step by step. No filler. No essay writing. Talk like a human.

[PUNCHLINE — 45 to 55 seconds]
The single most memorable line of the whole video. The one they screenshot. The one they send to their best friend. One perfect complete sentence.

[CTA — 55 to 60 seconds]
One natural question or statement that makes them comment or save. Not "like and subscribe" — something that makes them genuinely want to respond.

RULES:
- 130 to 160 words total — must fit 60 seconds
- Never use "..." — use dashes or full stops
- Every sentence complete and meaningful
- The punchline must be good enough to go in someone's Instagram bio

Write the full script now for: {topic}""",

"trends": """You are a TikTok strategist who watches what goes viral for Nigerian creators every single day.

Generate 8 specific video ideas for a Nigerian creator in this space: {topic}

Think about what formats are performing right now — storytimes, "things nobody tells you", POV setups, day-in-my-life, "I tried this for 30 days", transformation reveals, honest takes, responding to comments, "what I wish I knew."

For each idea write:

Idea [N]: [Video title written like a caption that makes you want to click — one complete compelling sentence]
Hook: [The exact first line spoken or shown — stops the scroll in 2 seconds — one strong complete sentence — never use "..."]
Format: [storytime / POV / talking to camera / voiceover with clips / text on screen]
Why it will perform: [One sentence — the psychology of why Nigerian viewers will save or share this]

---

8 ideas only. No intro. No outro. Make each one feel like it came from a real strategy session."""
}

# Keep your X_SYSTEM and X_PROMPTS exactly as in the original code you sent
X_SYSTEM = """You are the best Twitter/X content writer for Nigerian creators. You have written tweets and threads that have been retweeted thousands of times and ended up in people's bios and notes apps.

You understand Nigerian Twitter deeply — the wit, the hot takes, the threads that make people say "everybody needs to see this", the one-liners that travel.

When a user gives you a short or simple topic, do NOT produce short or simple output. Think about the real emotion, the real Nigerian experience, the specific truth underneath that topic — and write from there.

Every tweet you write must be something a real person would actually retweet, quote tweet, or screenshot for themselves.

RULES:
- Never use "..." anywhere. Use a dash ( — ) or a new sentence.
- Every sentence must be complete and meaningful.
- No Pidgin. Clean sharp English.
- No motivational poster energy. Real, human, quotable."""

X_PROMPTS = {
    # Paste your original X_PROMPTS here (they were in the first code)
    "captions": """...""",  # your original
    "hooks": """...""",
    "threads": """..."""
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
        "temperature": 0.88,
        "max_tokens": 1500
    }

    try:
        res = http_session.post(url, json=payload, headers=headers, timeout=30)
        data = res.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Groq Error: {e}")
        return "Something went wrong. Please try again."

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
LOADING = {
    "hooks":    ["🧠 Finding the hook that stops the scroll", "🔥 Writing something they will not skip", "👀 This one go hit different, hold on"],
    "captions": ["💅 Writing captions people will screenshot", "😭 Cooking the twist that makes it land", "🪄 Making every word count"],
    "pov":      ["🎥 Building the POV they will tag their friends in", "🍿 Setting the scene right", "👀 This POV go touch chest, give me a sec"],
    "hashtags": ["📊 Building your hashtag strategy", "🚀 Mixing reach tags with niche tags", "🔥 Algorithm food loading"],
    "bio":      ["✨ Writing bios that earn the follow", "📱 Building your profile hook", "🪄 Making your bio do the work"],
    "script":   ["🎬 Writing hook, body, punchline", "📝 Building a script that holds attention till the end", "🔥 This script go make them watch every second"],
    "trends":   ["📈 Analysing what is working right now", "🔥 Building trend ideas for your niche", "👀 Finding your next viral angle"],
    "threads":  ["🧵 Building the thread that goes viral", "✍️ Writing something people will quote tweet", "🔥 X thread loading"]
}

EXAMPLES = {
    "hooks":    "/hooks wanting to be happy",
    "captions": "/captions still broke after working so hard",
    "hashtags": "/hashtags Nigerian lifestyle soft life",
    "pov":      "/pov finally making it after everyone doubted you",
    "bio":      "/bio Nigerian lifestyle creator",
    "script":   "/script things nobody tells you about being broke",
    "trends":   "/trends Nigerian money mindset hustle",
    "threads":  "/threads why rest feels like a sin in Nigeria"
}

TIKTOK_COMMANDS = {"/hooks", "/captions", "/pov", "/hashtags", "/bio", "/script", "/trends"}
X_COMMANDS = {"/xtweets", "/xhooks", "/xthread"}
PRO_COMMANDS = {"/script", "/trends", "/xthread"}

# ========================= ROUTES =========================
# Paste the rest of your original route code (telegram_webhook, paystack_webhook, etc.) here exactly as you had it.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
