import os
import hmac
import hashlib
import random
import secrets
from datetime import datetime, timedelta
from functools import wraps

from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========================= CONFIG =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = "6415641863"

app = Flask(__name__)
app.secret_key = SECRET_KEY

# ========================= HTTP =========================
def get_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

http_session = get_session()

# ========================= DB =========================
db_pool = None

def init_pool():
    global db_pool
    if db_pool: return
    db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL, cursor_factory=RealDictCursor)

def get_db():
    if not db_pool: init_pool()
    return db_pool.getconn()

def release_db(conn):
    if db_pool and conn: db_pool.putconn(conn)

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Telegram bot users table
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                plan TEXT DEFAULT 'free',
                expires DATE,
                activated_at TIMESTAMP,
                usage_date DATE,
                usage_count INTEGER DEFAULT 0,
                region TEXT DEFAULT 'global'
            )""")
            # Web app users table
            cur.execute("""CREATE TABLE IF NOT EXISTS web_users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT DEFAULT 'free',
                expires DATE,
                usage_date DATE,
                usage_count INTEGER DEFAULT 0,
                region TEXT DEFAULT 'global',
                created_at TIMESTAMP DEFAULT NOW()
            )""")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= AUTH HELPERS =========================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "Please log in"}), 401
        return f(*args, **kwargs)
    return decorated

def get_web_user(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM web_users WHERE id=%s", (user_id,))
            return cur.fetchone()
    finally:
        release_db(conn)

def is_web_pro(user_id):
    user = get_web_user(user_id)
    if not user or user["plan"] != "pro" or not user["expires"]:
        return False
    return user["expires"] >= datetime.utcnow().date()

def web_uses_remaining(user_id):
    user = get_web_user(user_id)
    today = datetime.utcnow().date()
    if not user or user["usage_date"] != today:
        return FREE_LIMIT
    return max(0, FREE_LIMIT - user["usage_count"])

def check_and_increment_web_usage(user_id):
    if is_web_pro(user_id): return True
    today = datetime.utcnow().date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT usage_date, usage_count FROM web_users WHERE id=%s", (user_id,))
            row = cur.fetchone()
        current = row["usage_count"] if row and row["usage_date"] == today else 0
        if current >= FREE_LIMIT: return False
        with conn.cursor() as cur:
            cur.execute("""UPDATE web_users SET
                usage_date=%s,
                usage_count=CASE WHEN usage_date=%s THEN usage_count+1 ELSE 1 END
                WHERE id=%s""", (today, today, user_id))
        conn.commit()
        return True
    finally:
        release_db(conn)

def activate_web_pro(user_id):
    expires = (datetime.utcnow() + timedelta(days=30)).date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET plan='pro', expires=%s WHERE id=%s", (expires, user_id))
        conn.commit()
        return expires.strftime("%Y-%m-%d")
    finally:
        release_db(conn)

# ========================= TELEGRAM USER HELPERS =========================
def activate_pro(user_id):
    expires = (datetime.utcnow() + timedelta(days=30)).date()
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO users (user_id, plan, expires, activated_at)
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
            cur.execute("""INSERT INTO users (user_id, usage_date, usage_count)
                VALUES (%s, %s, 1)
                ON CONFLICT (user_id) DO UPDATE
                SET usage_date=EXCLUDED.usage_date,
                    usage_count=CASE WHEN users.usage_date=EXCLUDED.usage_date THEN users.usage_count+1 ELSE 1 END
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

# ========================= REGION VOICES =========================
REGION_VOICES = {
    "nigeria": """You write for Nigerian creators. You understand Nigerian internet culture deeply — the hustle, NEPA, soft life dreams, family pressure, this economy, Lagos life, glow ups, faith and doubt. Your references are Nigerian. Your energy is Nigerian TikTok.""",
    "usa": """You write for American creators. You understand US TikTok culture — the slang, the trends, the aesthetic, the therapy-speak, the hustle culture critique, the main character energy, the manifestation era. Your references are American.""",
    "uk": """You write for British creators. You understand UK TikTok — the dry humour, roadman culture, the class system, council estate to success stories, British understatement, "bare" and "innit" energy. Your references are British.""",
    "caribbean": """You write for Caribbean creators — Jamaican, Trinidadian, Barbadian. You understand the culture, the patois energy, the vibes, the pride, the grind. Your references feel Caribbean.""",
    "eastafrica": """You write for East African creators — Kenyan, Ugandan, Tanzanian, Ethiopian. You understand the hustle, Nairobi life, the culture, the ambition, the faith. Your references are East African.""",
    "southafrica": """You write for South African creators. You understand SA TikTok — the slang, township energy, Joburg life, the hustle, amapiano culture, load shedding jokes. Your references are South African.""",
    "global": """You write for creators worldwide. Your content is universally relatable — you focus on emotions, experiences, and truths that cross all cultures: hustle, love, growth, struggle, success, identity."""
}

REGION_NAMES = {
    "nigeria": "🇳🇬 Nigerian",
    "usa": "🇺🇸 American",
    "uk": "🇬🇧 British",
    "caribbean": "🇯🇲 Caribbean",
    "eastafrica": "🇰🇪 East African",
    "southafrica": "🇿🇦 South African",
    "global": "🌍 Global"
}

# ========================= AI PROMPTS =========================
TIKTOK_SYSTEM = """You are TikGenius — a viral TikTok content strategist who has studied millions of viral posts globally. You know exactly what makes content blow up — the psychology, the timing, the words, the emotion.

You write content that:
- Triggers an emotion in the first 3 words
- Makes people feel seen, called out, or deeply understood
- Is simple enough for anyone to get instantly
- Has a second line that surprises, twists, or lands like a punch

{region_voice}

PUNCTUATION RULE: Never use "..." (ellipsis). Use a dash ( — ), a line break, or a full stop to create the pause. Ellipsis makes content look unfinished.

LANGUAGE: Clean modern English. Casual, real, emotional, and sharp. Never sound like a motivational poster or an AI."""

X_SYSTEM = """You are XGenius — a Twitter/X content strategist who understands virality deeply. You write for creators who want to build real influence on X.

{region_voice}

Your content is bold, quotable, and sharp. The kind people screenshot and send to their group chat. No fluff. No motivational poster energy. Real, human, and memorable.

Every tweet under 280 characters. Clean English. Proper punctuation — no ellipsis (...)."""

TIKTOK_PROMPTS = {
"hooks": """Write 10 TikTok hooks for a creator posting about: {topic}

A hook stops the scroll in under 2 seconds. Study these viral hooks and WHY they work:

"Nobody is coming to save you. Build yourself." — Direct, activates the ego
"The version of me from 2 years ago would not recognise me." — Curiosity + transformation
"I used to be so easy to lose. Not anymore." — Short, personal, empowering
"Tell me why I worked this hard just to still be stressed 😭" — Funny + relatable frustration
"POV: you finally got everything you asked for. You're still not satisfied." — Honest truth nobody says

Write 10 ORIGINAL hooks for: {topic}
- Trigger an emotion in the FIRST 3 WORDS
- Mix tones: inspiring, funny, painfully honest, calling out a truth
- Under 20 words each
- No ellipsis — use dashes or full stops
- Format: 1) 2) 3)""",

"captions": """Write 15 TikTok captions for a creator posting about: {topic}

Study these viral captions and their structure:

"I used to shrink myself for people who weren't even paying attention. Never again."
STRUCTURE: Past behaviour + painful truth + declaration

"God will give you the life you prayed for. Just not in the timeline you imagined. 😭"
STRUCTURE: Promise + twist on expectations

"Nobody prepared me for how lonely success would feel before it arrived."
STRUCTURE: Raw honest truth — people screenshot and send to friends

"This time last year I was crying about something that doesn't even matter anymore. Growth."
STRUCTURE: Contrast + one-word punchline

Write 15 ORIGINAL captions for: {topic}
- Every caption needs a TWIST — line 2 surprises, deepens, or flips line 1
- 1 to 3 sentences max
- No ellipsis — use dashes or full stops
- ONE emoji maximum, only where it genuinely adds feeling
- Mix emotions: deep, funny, empowering, painfully relatable
- Format: 1) 2) 3)""",

"pov": """Write 10 TikTok POV concepts for a creator posting about: {topic}

Study these viral POVs:

"POV: You finally stopped chasing people who were never running towards you."
"POV: You worked in silence for 2 years. Now everyone wants to know your secret."
"POV: You have everything you asked for and you still don't feel it yet. Give yourself time."
"POV: You are exhausted. Not lazy. Not ungrateful. Just genuinely, deeply exhausted."

Write 10 ORIGINAL POVs for: {topic}
- Each describes a SPECIFIC feeling or moment — not vague
- So specific someone reads it and thinks "how did they know"
- Mix: inspiring, honest, funny, painful, healing
- No ellipsis — use dashes or full stops
- Format: 1) POV: [scenario]""",

"hashtags": """Generate 5 strategic TikTok hashtag sets for: {topic}

Each set: exactly 7 hashtags
- 2 massive reach tags (100M+): #fyp #foryoupage #tiktok #viral
- 2 medium reach (1M-50M): topic-specific tags people actually search
- 2 niche tags (under 1M): very specific to the content
- 1 community tag relevant to the region and topic

Format:
Set 1: #tag #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag #tag""",

"bio": """Write 8 TikTok bios for a creator in this niche: {topic}

Study these bios that actually work:
"building the life I used to dream about 🤫 | tips + real talk"
"I left the 9-5. Now I film my life. 📹 | come along"
"healing out loud so you don't have to do it alone 🖤"
"I document real life, not the highlight reel 📱"

Write 8 ORIGINAL bios for {topic} niche:
- Under 80 characters each
- Clear personality + content promise
- Mix tones: inspiring, funny, bold
- No ellipsis
- Format: 1) 2) 3)""",

"script": """Write a complete TikTok video script. Topic: {topic}

[HOOK — 0 to 3 seconds]
First thing said or shown. Creates immediate emotional reaction. Under 15 words.

[BODY — 4 to 45 seconds]
How a real creator SPEAKS on camera. Short sentences. Natural rhythm. Story, truth, or value. No filler. Every sentence earns the next.

[PUNCHLINE — 45 to 55 seconds]
The single most memorable line. The one people screenshot. The gut punch.

[CTA — 55 to 60 seconds]
One natural question that makes people comment, save, or share.

Rules:
- 130 to 160 words maximum (60 seconds)
- Write how people SPEAK not how they write essays
- No ellipsis — proper punctuation only""",

"trends": """Generate 8 specific video ideas for a creator in this space: {topic}

For each idea:

Idea [N]: [Specific video title written like a caption]
Hook: [Exact first line — stops scroll in 2 seconds]
Format: [storytime / POV / talking to camera / voiceover / text on screen]
Why it will perform: [1 sentence on the psychology]

---

Base ideas on formats that actually go viral: storytimes, "things nobody tells you", POV setups, "I tried X for 30 days", transformation reveals, honest opinion takes, "what I wish I knew."
8 ideas only. No intro."""
}

X_PROMPTS = {
"captions": """Write 10 viral Twitter/X posts about: {topic}

Study these tweets that actually performed:
"Stop romanticising the struggle. Rest is not laziness. Recovery is not weakness."
"Not every chapter of your life needs an audience." — 9 words. Universal. Instantly retweeted.
"Success without peace is just a well-funded anxiety attack." — Reframes success in a new way
"The energy you protect this year will determine what you build next year."

Write 10 ORIGINAL tweets about {topic}:
- Each under 280 characters
- Bold, quotable, screenshot-worthy
- One clear idea per tweet
- Mix: honest truths, empowering, darkly funny
- No ellipsis
- Format: 1) 2) 3)""",

"hooks": """Write 10 Twitter/X thread starter hooks about: {topic}

Study effective thread openers:
"I spent 3 years building something. Nobody saw it. Then everything changed in 90 days. Here is what happened:"
"10 things nobody tells you about [topic] (but should):"
"Why everything you've been told about [topic] is making things worse:"

Write 10 ORIGINAL thread hooks for: {topic}
- Creates immediate curiosity or emotional reaction
- Makes reader feel they CANNOT scroll past
- Under 30 words each
- Format: 1) 2) 3)""",

"threads": """Write a complete Twitter/X thread about: {topic}

Tweet 1 — HOOK: [Opener that makes people click "show this thread"]
Tweet 2 — CONTEXT: [Set up the problem — make people feel it personally]
Tweet 3 — THE TRUTH: [Insight that reframes how they see the topic]
Tweet 4 — GO DEEPER: [Specific example or evidence]
Tweet 5 — THE TWIST: [Unexpected angle most people avoid]
Tweet 6 — PRACTICAL: [What to actually do with this]
Tweet 7 — CLOSE: [Most quotable line — goes in someone's bio or notes app]

Rules:
- Each tweet under 280 characters
- No filler tweets — every tweet earns the next
- The close must be independently shareable
- No ellipsis"""
}

# ========================= AI FUNCTION =========================
def ask_groq(mode, topic, platform="tiktok", region="global"):
    region_voice = REGION_VOICES.get(region, REGION_VOICES["global"])

    if platform == "x":
        system = X_SYSTEM.format(region_voice=region_voice)
        prompt_template = X_PROMPTS.get(mode, X_PROMPTS["captions"])
    else:
        system = TIKTOK_SYSTEM.format(region_voice=region_voice)
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
        "temperature": 0.9,
        "max_tokens": 1500,
        "top_p": 0.95
    }

    try:
        res = http_session.post(url, json=payload, headers=headers, timeout=30)
        data = res.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
        print(f"Groq error: {data}")
        return "Something went wrong. Please try again."
    except Exception as e:
        print(f"Groq Error: {e}")
        return "Something went wrong. Please try again."

# ========================= PAYMENT =========================
def create_payment_link(email, user_id, source="web"):
    reference = f"TG-{user_id}-{source}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": email,
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {"web_user_id": user_id if source == "web" else None,
                     "telegram_id": user_id if source == "telegram" else None,
                     "source": source}
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    try:
        res = http_session.post("https://api.paystack.co/transaction/initialize",
                               json=payload, headers=headers, timeout=20).json()
        return res["data"]["authorization_url"] if res.get("status") else None
    except Exception as e:
        print(f"Paystack Error: {e}")
        return None

def tg_create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {"telegram_id": user_id, "username": username or "", "source": "telegram"}
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    try:
        res = http_session.post("https://api.paystack.co/transaction/initialize",
                               json=payload, headers=headers, timeout=20).json()
        return res["data"]["authorization_url"] if res.get("status") else None
    except Exception as e:
        print(f"Paystack Error: {e}")
        return None

# ========================= WEB ROUTES =========================

@app.route("/")
def index():
    return HOME_HTML

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")
    return DASHBOARD_HTML

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    region = data.get("region", "global")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM web_users WHERE email=%s", (email,))
            if cur.fetchone():
                return jsonify({"error": "Email already registered"}), 400
            cur.execute("""INSERT INTO web_users (email, password_hash, region)
                VALUES (%s, %s, %s) RETURNING id""",
                (email, generate_password_hash(password), region))
            user_id = cur.fetchone()["id"]
        conn.commit()
        session["user_id"] = user_id
        session["email"] = email
        return jsonify({"success": True, "redirect": "/dashboard"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        release_db(conn)

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM web_users WHERE email=%s", (email,))
            user = cur.fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify({"error": "Invalid email or password"}), 401
        session["user_id"] = user["id"]
        session["email"] = user["email"]
        return jsonify({"success": True, "redirect": "/dashboard"})
    finally:
        release_db(conn)

@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route("/api/me")
@login_required
def me():
    user = get_web_user(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
    pro = is_web_pro(session["user_id"])
    return jsonify({
        "email": user["email"],
        "plan": "pro" if pro else "free",
        "expires": user["expires"].strftime("%Y-%m-%d") if user["expires"] else None,
        "region": user["region"],
        "uses_remaining": FREE_LIMIT if pro else web_uses_remaining(session["user_id"]),
        "unlimited": pro
    })

@app.route("/api/generate", methods=["POST"])
@login_required
def generate():
    data = request.json or {}
    mode = data.get("mode", "captions")
    topic = data.get("topic", "").strip()
    platform = data.get("platform", "tiktok")

    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    if len(topic.split()) < 3:
        return jsonify({"error": "Be more specific — add more detail to your topic"}), 400

    user_id = session["user_id"]
    if not check_and_increment_web_usage(user_id):
        return jsonify({"error": "You have used all 5 free generations today. Upgrade to Pro for unlimited access."}), 429

    user = get_web_user(user_id)
    region = user["region"] if user else "global"
    result = ask_groq(mode, topic, platform, region)

    return jsonify({
        "result": result,
        "uses_remaining": web_uses_remaining(user_id) if not is_web_pro(user_id) else None,
        "unlimited": is_web_pro(user_id)
    })

@app.route("/api/upgrade", methods=["POST"])
@login_required
def upgrade():
    user = get_web_user(session["user_id"])
    link = create_payment_link(user["email"], session["user_id"], "web")
    if link:
        return jsonify({"url": link})
    return jsonify({"error": "Could not create payment link"}), 500

@app.route("/api/set-region", methods=["POST"])
@login_required
def set_region():
    region = request.json.get("region", "global")
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET region=%s WHERE id=%s", (region, session["user_id"]))
        conn.commit()
        return jsonify({"success": True})
    finally:
        release_db(conn)

# ========================= PAYSTACK WEBHOOK =========================
@app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid"}), 400

    event = request.json or {}
    if event.get("event") == "charge.success":
        data = event["data"]
        amount = data.get("amount")
        metadata = data.get("metadata", {})
        source = metadata.get("source", "telegram")

        if amount == PRICE_KOBO:
            if source == "web":
                web_user_id = metadata.get("web_user_id")
                if web_user_id:
                    activate_web_pro(web_user_id)
            else:
                telegram_id = metadata.get("telegram_id")
                if telegram_id:
                    expires = activate_pro(telegram_id)
                    send_telegram_message(telegram_id,
                        f"Payment confirmed. Welcome to Pro.\n\nAccess active till {expires}\n\nEverything unlocked. Try /script, /trends, or /xthread now.")

    return jsonify({"status": "ok"}), 200

# ========================= TELEGRAM BOT =========================
LOADING = {
    "hooks":    ["🧠 Writing hooks that stop the scroll...", "🔥 Finding the angle that makes them watch..."],
    "captions": ["💅 Writing captions people will screenshot...", "😭 Cooking the twist that makes it land..."],
    "pov":      ["🎥 Building the POV they will tag friends in...", "🍿 Setting up the scene and the twist..."],
    "hashtags": ["📊 Building your hashtag strategy...", "🚀 Mixing reach tags with niche tags..."],
    "bio":      ["✨ Writing bios that get the follow...", "📱 Building your profile hook..."],
    "script":   ["🎬 Writing hook, body, punchline...", "📝 Building a script that holds attention..."],
    "trends":   ["📈 Analysing what is working right now...", "🔥 Building trend ideas for your niche..."],
    "threads":  ["🧵 Building the thread that goes viral...", "✍️ Writing something people will quote tweet..."]
}

EXAMPLES = {
    "hooks":    "/hooks I prayed for this life and I am still not happy",
    "captions": "/captions I work so hard but I am still broke",
    "hashtags": "/hashtags lifestyle and soft life content creator",
    "pov":      "/pov you finally made it and nobody who doubted you said sorry",
    "bio":      "/bio lifestyle and soft life content creator",
    "script":   "/script things nobody tells you before you start working for yourself",
    "trends":   "/trends money mindset and hustle content",
    "threads":  "/threads x why resting feels like a crime"
}

TIKTOK_COMMANDS = {"/hooks", "/captions", "/pov", "/hashtags", "/bio", "/script", "/trends"}
X_COMMANDS = {"/xtweets", "/xhooks", "/xthread"}
PRO_COMMANDS = {"/script", "/trends", "/xthread"}

REGION_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "🇳🇬 Nigerian", "callback_data": "region_nigeria"},
         {"text": "🇺🇸 American", "callback_data": "region_usa"}],
        [{"text": "🇬🇧 British", "callback_data": "region_uk"},
         {"text": "🇯🇲 Caribbean", "callback_data": "region_caribbean"}],
        [{"text": "🇰🇪 East African", "callback_data": "region_eastafrica"},
         {"text": "🇿🇦 South African", "callback_data": "region_southafrica"}],
        [{"text": "🌍 Global / General", "callback_data": "region_global"}]
    ]
}

def send_telegram_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                         json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_typing(chat_id):
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction",
                         json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except: pass

def get_user_region(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT region FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        return row["region"] if row and row["region"] else "global"
    finally:
        release_db(conn)

def set_user_region(user_id, region):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO users (user_id, region)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET region=EXCLUDED.region
            """, (user_id, region))
        conn.commit()
    finally:
        release_db(conn)

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}

    # Handle callback queries (region selection buttons)
    if "callback_query" in data:
        cb = data["callback_query"]
        user_id = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        cb_data = cb.get("data", "")

        if cb_data.startswith("region_"):
            region = cb_data.replace("region_", "")
            set_user_region(user_id, region)
            region_name = REGION_NAMES.get(region, "Global")
            send_telegram_message(chat_id,
                f"✅ Region set to {region_name}\n\nYour content will now be written in that voice.\n\nTry it now:\n{EXAMPLES.get('captions')}")
            try:
                http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                 json={"callback_query_id": cb["id"]}, timeout=5)
            except: pass

        return jsonify({"ok": True})

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

    if command == "/start":
        send_telegram_message(chat_id,
            f"✨ Welcome {first_name} — you just found TikGenius 🔥\n\n"
            f"I write viral content for creators worldwide.\n\n"
            f"First — pick your content style so I write in your voice:")
        send_telegram_message(chat_id,
            "Choose your region:", reply_markup=REGION_KEYBOARD)

    elif command == "/region":
        send_telegram_message(chat_id,
            "Choose your content region:", reply_markup=REGION_KEYBOARD)

    elif command == "/commands":
        send_telegram_message(chat_id,
            f"━━━ TIKTOK ━━━\n"
            f"/hooks [topic]\n"
            f"/captions [topic]\n"
            f"/pov [topic]\n"
            f"/hashtags [topic]\n"
            f"/bio [niche]\n"
            f"/script [idea] ⭐ Pro\n\n"
            f"━━━ TWITTER / X ━━━\n"
            f"/xtweets [topic]\n"
            f"/xhooks [topic]\n"
            f"/xthread [topic] ⭐ Pro\n\n"
            f"━━━ OTHER ━━━\n"
            f"/trends [niche] ⭐ Pro\n"
            f"/region — change your content region\n"
            f"/plan — check your plan\n"
            f"/upgrade — go Pro\n\n"
            f"Free: {FREE_LIMIT} uses/day\n"
            f"Pro: ₦2,000/month — unlimited\n\n"
            f"Be specific with your topic:\n"
            f"❌ /captions tired\n"
            f"✅ /captions I work so hard but I am still broke")

    elif command == "/plan":
        region = get_user_region(user_id)
        region_name = REGION_NAMES.get(region, "Global")
        if is_pro(user_id):
            send_telegram_message(chat_id,
                f"✅ Pro Active — expires {get_pro_expiry(user_id)}\n"
                f"Region: {region_name}\n\nUnlimited access to everything.")
        else:
            remaining = free_uses_remaining(user_id)
            send_telegram_message(chat_id,
                f"🆓 Free Plan — {remaining}/{FREE_LIMIT} uses left today\n"
                f"Region: {region_name}\n\n"
                f"Upgrade to Pro for ₦2,000/month → /upgrade")

    elif command == "/upgrade":
        link = tg_create_payment_link(user_id, username)
        send_telegram_message(chat_id,
            f"🚀 TikGenius Pro — ₦2,000/month\n\n"
            f"✅ Unlimited hooks, captions, POVs, hashtags, bios\n"
            f"✅ Full video scripts (/script)\n"
            f"✅ Trend ideas (/trends)\n"
            f"✅ Full X threads (/xthread)\n"
            f"✅ All regions supported\n"
            f"✅ No daily limits ever\n\n"
            f"Pay here:\n{link or 'Try again in a moment'}\n\n"
            f"Activation is automatic after payment ✅")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_telegram_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_telegram_message(chat_id, "❌ Not allowed.")

    elif command == "/stats":
        if str(user_id) != ADMIN_ID:
            send_telegram_message(chat_id, "❌ Not allowed.")
            return jsonify({"ok": True})
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM users")
                tg_total = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS pro FROM users WHERE plan='pro' AND expires >= CURRENT_DATE")
                tg_pro = cur.fetchone()["pro"]
                cur.execute("SELECT COUNT(*) AS total FROM web_users")
                web_total = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS pro FROM web_users WHERE plan='pro' AND expires >= CURRENT_DATE")
                web_pro = cur.fetchone()["pro"]
            send_telegram_message(chat_id,
                f"📊 TikGenius Stats\n\n"
                f"TELEGRAM\n"
                f"👥 Users: {tg_total}\n"
                f"💎 Pro: {tg_pro}\n\n"
                f"WEBSITE\n"
                f"👥 Users: {web_total}\n"
                f"💎 Pro: {web_pro}")
        finally:
            release_db(conn)

    elif command in TIKTOK_COMMANDS:
        mode = command.replace("/", "")

        if command in PRO_COMMANDS and not is_pro(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id,
                f"🔒 Pro feature.\n\nUpgrade for ₦2,000/month:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_telegram_message(chat_id,
                f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if len(topic.split()) < 3:
            send_telegram_message(chat_id,
                f"Be more specific for better results.\n\nTry: {EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id,
                f"⏳ {FREE_LIMIT} free uses used for today.\n\nUpgrade to Pro:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_telegram_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it..."])))
        region = get_user_region(user_id)
        result = ask_groq(mode, topic, "tiktok", region)
        send_telegram_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_telegram_message(chat_id,
                    f"💡 {remaining} free use(s) left today.\n\nGo Pro → /upgrade")

    elif command in X_COMMANDS:
        mode_map = {"/xtweets": "captions", "/xhooks": "hooks", "/xthread": "threads"}
        mode = mode_map[command]

        if command == "/xthread" and not is_pro(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id,
                f"🔒 X Threads is Pro.\n\nUpgrade:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_telegram_message(chat_id,
                f"Add a topic.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if len(topic.split()) < 3:
            send_telegram_message(chat_id,
                f"Be more specific.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id,
                f"⏳ Free uses finished.\n\nUpgrade:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_telegram_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it..."])))
        region = get_user_region(user_id)
        result = ask_groq(mode, topic, "x", region)
        send_telegram_message(chat_id, f"✨ XGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_telegram_message(chat_id,
                    f"💡 {remaining} free use(s) left today. Go Pro → /upgrade")

    else:
        send_telegram_message(chat_id, "Unknown command. Use /commands to see everything.")

    return jsonify({"ok": True})

# ========================= HTML PAGES =========================
HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius — Go Viral. In Your Voice.</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#080810;
  --surface:#0f0f1a;
  --card:#141428;
  --border:#1e1e3a;
  --purple:#7c3aed;
  --purple-light:#a855f7;
  --pink:#ec4899;
  --text:#f0f0ff;
  --muted:#6b6b8a;
}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh;overflow-x:hidden}
h1,h2,h3,h4{font-family:'Syne',sans-serif}

/* NAV */
nav{display:flex;justify-content:space-between;align-items:center;padding:1.2rem 2rem;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;background:rgba(8,8,16,0.9);backdrop-filter:blur(12px)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:1.3rem;background:linear-gradient(135deg,var(--purple-light),var(--pink));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav-btns{display:flex;gap:0.75rem}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text);padding:0.5rem 1.2rem;border-radius:8px;cursor:pointer;font-family:'DM Sans',sans-serif;font-size:0.9rem;transition:all 0.2s}
.btn-ghost:hover{border-color:var(--purple);color:var(--purple-light)}
.btn-primary{background:linear-gradient(135deg,var(--purple),var(--pink));border:none;color:white;padding:0.5rem 1.4rem;border-radius:8px;cursor:pointer;font-family:'DM Sans',sans-serif;font-size:0.9rem;font-weight:500;transition:opacity 0.2s}
.btn-primary:hover{opacity:0.9}

/* HERO */
.hero{text-align:center;padding:6rem 2rem 4rem;max-width:800px;margin:0 auto}
.hero-badge{display:inline-block;background:rgba(124,58,237,0.15);border:1px solid rgba(124,58,237,0.3);color:var(--purple-light);padding:0.4rem 1rem;border-radius:100px;font-size:0.85rem;margin-bottom:2rem}
.hero h1{font-size:clamp(2.5rem,6vw,4.5rem);font-weight:800;line-height:1.1;margin-bottom:1.5rem}
.hero h1 span{background:linear-gradient(135deg,var(--purple-light),var(--pink));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--muted);font-size:1.15rem;line-height:1.7;max-width:540px;margin:0 auto 2.5rem}
.hero-btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn-large{padding:0.9rem 2rem;border-radius:10px;font-size:1rem;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all 0.2s}
.btn-large.primary{background:linear-gradient(135deg,var(--purple),var(--pink));border:none;color:white}
.btn-large.primary:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(124,58,237,0.4)}
.btn-large.ghost{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-large.ghost:hover{border-color:var(--purple-light)}

/* GLOW */
.glow{position:absolute;width:600px;height:600px;border-radius:50%;background:radial-gradient(circle,rgba(124,58,237,0.12) 0%,transparent 70%);top:-200px;left:50%;transform:translateX(-50%);pointer-events:none}

/* EXAMPLES */
.section{padding:5rem 2rem;max-width:1100px;margin:0 auto}
.section-label{text-align:center;color:var(--purple-light);font-size:0.85rem;font-weight:600;letter-spacing:2px;text-transform:uppercase;margin-bottom:1rem}
.section h2{text-align:center;font-size:clamp(1.8rem,4vw,2.8rem);font-weight:800;margin-bottom:1rem}
.section p.sub{text-align:center;color:var(--muted);max-width:500px;margin:0 auto 3rem}

.examples-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem}
.example-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:1.5rem;transition:border-color 0.2s}
.example-card:hover{border-color:var(--purple)}
.example-card .tag{display:inline-block;background:rgba(124,58,237,0.15);color:var(--purple-light);padding:0.25rem 0.75rem;border-radius:100px;font-size:0.75rem;margin-bottom:1rem}
.example-card .prompt{color:var(--muted);font-size:0.85rem;margin-bottom:1rem;font-style:italic}
.example-card .output{color:var(--text);font-size:0.95rem;line-height:1.6}
.example-card .output p{margin-bottom:0.5rem;padding-left:0.75rem;border-left:2px solid var(--purple)}

/* HOW IT WORKS */
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:2rem;margin-top:3rem}
.step{text-align:center}
.step-num{width:48px;height:48px;border-radius:50%;background:linear-gradient(135deg,var(--purple),var(--pink));display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:1.1rem;margin:0 auto 1rem}
.step h3{font-size:1.1rem;margin-bottom:0.5rem}
.step p{color:var(--muted);font-size:0.9rem;line-height:1.6}

/* REGIONS */
.regions{display:flex;flex-wrap:wrap;gap:0.75rem;justify-content:center;margin-top:2rem}
.region-tag{background:var(--card);border:1px solid var(--border);padding:0.5rem 1.2rem;border-radius:100px;font-size:0.9rem}

/* PRICING */
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2rem;margin-top:3rem;max-width:700px;margin-left:auto;margin-right:auto}
.price-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:2rem}
.price-card.featured{border-color:var(--purple);position:relative}
.price-card.featured::before{content:'MOST POPULAR';position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--purple),var(--pink));color:white;font-size:0.7rem;font-weight:700;padding:0.25rem 1rem;border-radius:100px;font-family:'Syne',sans-serif;letter-spacing:1px}
.price-label{color:var(--muted);font-size:0.85rem;margin-bottom:0.5rem}
.price-amount{font-family:'Syne',sans-serif;font-size:2.5rem;font-weight:800;margin-bottom:0.25rem}
.price-period{color:var(--muted);font-size:0.85rem;margin-bottom:1.5rem}
.price-features{list-style:none;margin-bottom:2rem}
.price-features li{padding:0.5rem 0;border-bottom:1px solid var(--border);font-size:0.9rem;color:var(--muted)}
.price-features li span{color:var(--text)}
.price-features li::before{content:'✓ ';color:var(--purple-light)}

/* MODAL */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);backdrop-filter:blur(8px);z-index:1000;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:2.5rem;width:90%;max-width:420px}
.modal h2{font-size:1.5rem;margin-bottom:0.5rem}
.modal p{color:var(--muted);font-size:0.9rem;margin-bottom:1.5rem}
.tabs{display:flex;gap:0.5rem;margin-bottom:1.5rem;background:var(--surface);padding:0.25rem;border-radius:8px}
.tab{flex:1;padding:0.6rem;text-align:center;border-radius:6px;cursor:pointer;font-size:0.9rem;transition:all 0.2s;border:none;background:transparent;color:var(--muted);font-family:'DM Sans',sans-serif}
.tab.active{background:var(--purple);color:white}
.form-group{margin-bottom:1rem}
.form-group label{display:block;font-size:0.85rem;color:var(--muted);margin-bottom:0.4rem}
.form-group input, .form-group select{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:0.75rem 1rem;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:0.95rem;outline:none;transition:border-color 0.2s}
.form-group input:focus, .form-group select:focus{border-color:var(--purple)}
.form-error{color:#f87171;font-size:0.85rem;margin-top:0.5rem;display:none}
.btn-full{width:100%;padding:0.85rem;border-radius:8px;font-size:1rem;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;margin-top:0.5rem}

/* FOOTER */
footer{border-top:1px solid var(--border);padding:2rem;text-align:center;color:var(--muted);font-size:0.85rem}

/* RESPONSIVE */
@media(max-width:600px){
  nav{padding:1rem}
  .hero{padding:4rem 1.5rem 3rem}
  .section{padding:3rem 1.5rem}
}
</style>
</head>
<body>

<div class="glow"></div>

<nav>
  <div class="logo">TikGenius</div>
  <div class="nav-btns">
    <button class="btn-ghost" onclick="openModal('login')">Log in</button>
    <button class="btn-primary" onclick="openModal('signup')">Get Started Free</button>
  </div>
</nav>

<section style="position:relative">
  <div class="hero">
    <div class="hero-badge">✦ AI-Powered Content for Creators</div>
    <h1>Go viral.<br><span>In your voice.</span></h1>
    <p>TikGenius writes your TikTok captions, hooks, POVs, scripts, and Twitter threads — in the cultural voice that actually resonates with your audience.</p>
    <div class="hero-btns">
      <button class="btn-large primary" onclick="openModal('signup')">Start Free — No Card Needed</button>
      <a href="https://t.me/TikGeniusBot" target="_blank"><button class="btn-large ghost">Open in Telegram</button></a>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-label">Real Output</div>
  <h2>Content that actually hits</h2>
  <p class="sub">See what TikGenius writes — ready to copy and post</p>
  <div class="examples-grid">
    <div class="example-card">
      <div class="tag">TikTok Captions</div>
      <div class="prompt">Topic: "I work so hard but I'm still broke"</div>
      <div class="output">
        <p>I used to think hard work guaranteed results. Nobody told me about the gap in between.</p>
        <p>Working hard in silence because not everyone needs to see the process. The results will speak.</p>
        <p>Nobody prepared me for how lonely the building phase would feel. 😭</p>
      </div>
    </div>
    <div class="example-card">
      <div class="tag">TikTok Hooks</div>
      <div class="prompt">Topic: "I prayed for this life and I'm still not happy"</div>
      <div class="output">
        <p>God answered every prayer. I still found something to worry about.</p>
        <p>Tell me why I got everything I asked for and I'm still not satisfied 😭</p>
        <p>POV: you built the life you dreamed about. The dream forgot to mention the anxiety.</p>
      </div>
    </div>
    <div class="example-card">
      <div class="tag">Twitter / X</div>
      <div class="prompt">Topic: "Why resting feels like a crime"</div>
      <div class="output">
        <p>You are not lazy. You are exhausted. There is a difference and nobody let you learn it.</p>
        <p>Rest is not a reward for finishing everything. Nothing is ever finished. Rest anyway.</p>
        <p>Success without peace is just a well-funded anxiety attack.</p>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-label">How It Works</div>
  <h2>Three steps to viral content</h2>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <h3>Pick your region</h3>
      <p>Choose your cultural voice — Nigerian, American, British, Caribbean, East African, South African, or Global.</p>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <h3>Type your topic</h3>
      <p>Describe what your video or post is about. The more specific, the better the output.</p>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <h3>Copy and post</h3>
      <p>Get captions, hooks, POVs, scripts, or threads instantly — ready to paste directly into TikTok or Twitter.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-label">Global</div>
  <h2>Your culture. Your voice.</h2>
  <p class="sub">TikGenius writes in the cultural voice that resonates with your audience</p>
  <div class="regions">
    <span class="region-tag">🇳🇬 Nigerian</span>
    <span class="region-tag">🇺🇸 American</span>
    <span class="region-tag">🇬🇧 British</span>
    <span class="region-tag">🇯🇲 Caribbean</span>
    <span class="region-tag">🇰🇪 East African</span>
    <span class="region-tag">🇿🇦 South African</span>
    <span class="region-tag">🌍 Global</span>
  </div>
</section>

<section class="section">
  <div class="section-label">Pricing</div>
  <h2>Simple pricing</h2>
  <div class="pricing-grid">
    <div class="price-card">
      <div class="price-label">Free Forever</div>
      <div class="price-amount">₦0</div>
      <div class="price-period">5 generations per day</div>
      <ul class="price-features">
        <li><span>Hooks, captions, POVs</span></li>
        <li><span>Hashtag sets</span></li>
        <li><span>Bios</span></li>
        <li><span>All 7 regions</span></li>
        <li><span>TikTok + Twitter/X</span></li>
      </ul>
      <button class="btn-primary btn-full" onclick="openModal('signup')">Start Free</button>
    </div>
    <div class="price-card featured">
      <div class="price-label">Pro</div>
      <div class="price-amount">₦2,000</div>
      <div class="price-period">per month — unlimited everything</div>
      <ul class="price-features">
        <li><span>Everything in Free</span></li>
        <li><span>Full 60-second scripts</span></li>
        <li><span>Full X threads</span></li>
        <li><span>Trend ideas for your niche</span></li>
        <li><span>No daily limits ever</span></li>
      </ul>
      <button class="btn-primary btn-full" onclick="openModal('signup')">Get Pro</button>
    </div>
  </div>
</section>

<footer>
  <div class="logo" style="margin-bottom:1rem">TikGenius</div>
  <p>Built for creators worldwide. Available on web and Telegram.</p>
</footer>

<!-- AUTH MODAL -->
<div class="modal-overlay" id="authModal">
  <div class="modal">
    <h2 id="modalTitle">Create your account</h2>
    <p id="modalSub">Start generating viral content for free</p>
    <div class="tabs">
      <button class="tab active" id="signupTab" onclick="switchTab('signup')">Sign Up</button>
      <button class="tab" id="loginTab" onclick="switchTab('login')">Log In</button>
    </div>

    <div id="signupForm">
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="signupEmail" placeholder="you@example.com">
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="signupPassword" placeholder="Min 6 characters">
      </div>
      <div class="form-group">
        <label>Your Content Region</label>
        <select id="signupRegion">
          <option value="nigeria">🇳🇬 Nigerian / West African</option>
          <option value="usa">🇺🇸 American</option>
          <option value="uk">🇬🇧 British</option>
          <option value="caribbean">🇯🇲 Caribbean</option>
          <option value="eastafrica">🇰🇪 East African</option>
          <option value="southafrica">🇿🇦 South African</option>
          <option value="global" selected>🌍 Global / General</option>
        </select>
      </div>
      <div class="form-error" id="signupError"></div>
      <button class="btn-primary btn-full" onclick="doSignup()">Create Account</button>
    </div>

    <div id="loginForm" style="display:none">
      <div class="form-group">
        <label>Email</label>
        <input type="email" id="loginEmail" placeholder="you@example.com">
      </div>
      <div class="form-group">
        <label>Password</label>
        <input type="password" id="loginPassword" placeholder="Your password">
      </div>
      <div class="form-error" id="loginError"></div>
      <button class="btn-primary btn-full" onclick="doLogin()">Log In</button>
    </div>

    <div style="text-align:center;margin-top:1rem">
      <button onclick="closeModal()" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.85rem">Cancel</button>
    </div>
  </div>
</div>

<script>
function openModal(tab) {
  document.getElementById('authModal').classList.add('active');
  switchTab(tab);
}
function closeModal() {
  document.getElementById('authModal').classList.remove('active');
}
function switchTab(tab) {
  document.getElementById('signupForm').style.display = tab === 'signup' ? 'block' : 'none';
  document.getElementById('loginForm').style.display = tab === 'login' ? 'block' : 'none';
  document.getElementById('signupTab').classList.toggle('active', tab === 'signup');
  document.getElementById('loginTab').classList.toggle('active', tab === 'login');
  document.getElementById('modalTitle').textContent = tab === 'signup' ? 'Create your account' : 'Welcome back';
  document.getElementById('modalSub').textContent = tab === 'signup' ? 'Start generating viral content for free' : 'Log in to your TikGenius account';
}
async function doSignup() {
  const email = document.getElementById('signupEmail').value;
  const password = document.getElementById('signupPassword').value;
  const region = document.getElementById('signupRegion').value;
  const err = document.getElementById('signupError');
  err.style.display = 'none';
  const res = await fetch('/api/signup', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, password, region})
  });
  const data = await res.json();
  if (data.error) { err.textContent = data.error; err.style.display = 'block'; return; }
  window.location.href = data.redirect;
}
async function doLogin() {
  const email = document.getElementById('loginEmail').value;
  const password = document.getElementById('loginPassword').value;
  const err = document.getElementById('loginError');
  err.style.display = 'none';
  const res = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({email, password})
  });
  const data = await res.json();
  if (data.error) { err.textContent = data.error; err.style.display = 'block'; return; }
  window.location.href = data.redirect;
}
document.getElementById('authModal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius — Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#080810;--surface:#0f0f1a;--card:#141428;--border:#1e1e3a;
  --purple:#7c3aed;--purple-light:#a855f7;--pink:#ec4899;
  --text:#f0f0ff;--muted:#6b6b8a;--green:#22c55e;
}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;min-height:100vh}
h1,h2,h3{font-family:'Syne',sans-serif}

/* LAYOUT */
.app{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
@media(max-width:768px){.app{grid-template-columns:1fr}.sidebar{display:none}}

/* SIDEBAR */
.sidebar{background:var(--surface);border-right:1px solid var(--border);padding:1.5rem;display:flex;flex-direction:column}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:1.3rem;background:linear-gradient(135deg,var(--purple-light),var(--pink));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:2rem}
.user-info{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:1rem;margin-bottom:2rem}
.user-email{font-size:0.85rem;color:var(--muted);margin-bottom:0.25rem;word-break:break-all}
.user-plan{display:inline-block;padding:0.2rem 0.75rem;border-radius:100px;font-size:0.75rem;font-weight:600}
.plan-free{background:rgba(107,107,138,0.2);color:var(--muted)}
.plan-pro{background:rgba(124,58,237,0.2);color:var(--purple-light)}
.uses-bar{margin-top:0.75rem}
.uses-label{font-size:0.75rem;color:var(--muted);margin-bottom:0.25rem}
.bar{height:4px;background:var(--border);border-radius:100px;overflow:hidden}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--purple),var(--pink));transition:width 0.3s}

.nav-label{font-size:0.7rem;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-bottom:0.5rem}
.nav-item{display:flex;align-items:center;gap:0.75rem;padding:0.7rem 0.75rem;border-radius:8px;cursor:pointer;font-size:0.9rem;color:var(--muted);transition:all 0.2s;margin-bottom:0.25rem;border:none;background:none;width:100%;text-align:left}
.nav-item:hover{background:var(--card);color:var(--text)}
.nav-item.active{background:rgba(124,58,237,0.15);color:var(--purple-light)}
.nav-item .icon{font-size:1rem;width:20px;text-align:center}

.sidebar-bottom{margin-top:auto}
.upgrade-card{background:linear-gradient(135deg,rgba(124,58,237,0.2),rgba(236,72,153,0.15));border:1px solid rgba(124,58,237,0.3);border-radius:12px;padding:1rem;margin-bottom:1rem}
.upgrade-card h4{font-size:0.9rem;margin-bottom:0.25rem}
.upgrade-card p{font-size:0.75rem;color:var(--muted);margin-bottom:0.75rem}
.btn-upgrade{width:100%;background:linear-gradient(135deg,var(--purple),var(--pink));border:none;color:white;padding:0.6rem;border-radius:8px;font-size:0.85rem;cursor:pointer;font-family:'DM Sans',sans-serif;font-weight:500}

/* MAIN */
.main{padding:2rem;overflow-y:auto}
.main-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:2rem}
.main-header h1{font-size:1.5rem}
.platform-switch{display:flex;gap:0.5rem;background:var(--surface);padding:0.25rem;border-radius:8px}
.platform-btn{padding:0.5rem 1rem;border-radius:6px;border:none;background:transparent;color:var(--muted);cursor:pointer;font-family:'DM Sans',sans-serif;font-size:0.85rem;transition:all 0.2s}
.platform-btn.active{background:var(--purple);color:white}

/* GENERATE AREA */
.generate-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:1.5rem;margin-bottom:1.5rem}
.generate-card h3{font-size:1rem;margin-bottom:1rem;color:var(--muted)}
.topic-input{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:1rem 1.2rem;border-radius:10px;font-family:'DM Sans',sans-serif;font-size:1rem;outline:none;transition:border-color 0.2s;resize:none}
.topic-input:focus{border-color:var(--purple)}
.tip{font-size:0.8rem;color:var(--muted);margin-top:0.5rem}

.modes{display:flex;flex-wrap:wrap;gap:0.5rem;margin:1rem 0}
.mode-btn{padding:0.5rem 1rem;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-family:'DM Sans',sans-serif;font-size:0.85rem;transition:all 0.2s}
.mode-btn:hover{border-color:var(--purple-light);color:var(--text)}
.mode-btn.active{background:rgba(124,58,237,0.15);border-color:var(--purple);color:var(--purple-light)}
.mode-btn.pro-mode{position:relative}
.mode-btn.pro-mode::after{content:'PRO';position:absolute;top:-6px;right:-4px;background:linear-gradient(135deg,var(--purple),var(--pink));color:white;font-size:0.55rem;padding:0.1rem 0.3rem;border-radius:4px;font-weight:700}

.generate-btn{background:linear-gradient(135deg,var(--purple),var(--pink));border:none;color:white;padding:0.85rem 2rem;border-radius:10px;font-size:1rem;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;transition:opacity 0.2s;display:flex;align-items:center;gap:0.5rem}
.generate-btn:hover{opacity:0.9}
.generate-btn:disabled{opacity:0.5;cursor:not-allowed}

/* OUTPUT */
.output-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:1.5rem;display:none}
.output-card.visible{display:block}
.output-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
.output-header h3{font-size:1rem}
.copy-btn{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:0.4rem 1rem;border-radius:8px;cursor:pointer;font-size:0.85rem;font-family:'DM Sans',sans-serif;transition:all 0.2s}
.copy-btn:hover{border-color:var(--purple-light)}
.output-text{white-space:pre-wrap;line-height:1.8;font-size:0.95rem;color:var(--text)}
.loading{display:flex;align-items:center;gap:0.5rem;color:var(--muted);font-size:0.9rem}
.spinner{width:16px;height:16px;border:2px solid var(--border);border-top-color:var(--purple-light);border-radius:50%;animation:spin 0.6s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* REGION SELECT */
.region-select{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:0.5rem 0.75rem;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:0.85rem;outline:none;cursor:pointer}

/* ERROR */
.error-msg{background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);color:#f87171;padding:0.75rem 1rem;border-radius:8px;font-size:0.9rem;margin-top:0.75rem;display:none}
</style>
</head>
<body>

<div class="app">
  <!-- SIDEBAR -->
  <div class="sidebar">
    <div class="logo">TikGenius</div>

    <div class="user-info">
      <div class="user-email" id="userEmail">Loading...</div>
      <span class="user-plan plan-free" id="userPlan">Free</span>
      <div class="uses-bar" id="usesBar">
        <div class="uses-label" id="usesLabel">5/5 uses left today</div>
        <div class="bar"><div class="bar-fill" id="barFill" style="width:100%"></div></div>
      </div>
    </div>

    <div class="nav-label">Tools</div>
    <button class="nav-item active" onclick="setMode('captions')" id="nav-captions">
      <span class="icon">✍️</span> Captions
    </button>
    <button class="nav-item" onclick="setMode('hooks')" id="nav-hooks">
      <span class="icon">🎣</span> Hooks
    </button>
    <button class="nav-item" onclick="setMode('pov')" id="nav-pov">
      <span class="icon">🎥</span> POV Ideas
    </button>
    <button class="nav-item" onclick="setMode('hashtags')" id="nav-hashtags">
      <span class="icon">📊</span> Hashtags
    </button>
    <button class="nav-item" onclick="setMode('bio')" id="nav-bio">
      <span class="icon">👤</span> Bio
    </button>
    <button class="nav-item" onclick="setMode('script')" id="nav-script">
      <span class="icon">📝</span> Script ⭐
    </button>
    <button class="nav-item" onclick="setMode('trends')" id="nav-trends">
      <span class="icon">📈</span> Trends ⭐
    </button>

    <div class="nav-label" style="margin-top:1rem">Twitter / X</div>
    <button class="nav-item" onclick="setMode('captions','x')" id="nav-xtweets">
      <span class="icon">𝕏</span> Tweets
    </button>
    <button class="nav-item" onclick="setMode('hooks','x')" id="nav-xhooks">
      <span class="icon">🧲</span> Thread Hooks
    </button>
    <button class="nav-item" onclick="setMode('threads','x')" id="nav-xthread">
      <span class="icon">🧵</span> Full Thread ⭐
    </button>

    <div class="sidebar-bottom">
      <div class="upgrade-card" id="upgradeCard">
        <h4>Go Pro</h4>
        <p>Unlimited generations, scripts, threads, and trends.</p>
        <button class="btn-upgrade" onclick="doUpgrade()">Upgrade — ₦2,000/mo</button>
      </div>
      <button class="nav-item" onclick="doLogout()" style="color:var(--muted)">
        <span class="icon">↩</span> Log out
      </button>
    </div>
  </div>

  <!-- MAIN -->
  <div class="main">
    <div class="main-header">
      <h1 id="modeTitle">Captions</h1>
      <div style="display:flex;gap:0.75rem;align-items:center">
        <select class="region-select" id="regionSelect" onchange="changeRegion()">
          <option value="nigeria">🇳🇬 Nigerian</option>
          <option value="usa">🇺🇸 American</option>
          <option value="uk">🇬🇧 British</option>
          <option value="caribbean">🇯🇲 Caribbean</option>
          <option value="eastafrica">🇰🇪 East African</option>
          <option value="southafrica">🇿🇦 South African</option>
          <option value="global" selected>🌍 Global</option>
        </select>
        <div class="platform-switch">
          <button class="platform-btn active" id="tiktokBtn" onclick="setPlatform('tiktok')">TikTok</button>
          <button class="platform-btn" id="xBtn" onclick="setPlatform('x')">Twitter / X</button>
        </div>
      </div>
    </div>

    <div class="generate-card">
      <h3>What is your video or post about?</h3>
      <textarea class="topic-input" id="topicInput" rows="3" placeholder="Be specific — the more detail you give, the better the output.

Example: I work so hard but I am still broke
Example: I finally left the toxic relationship and I feel guilty"></textarea>
      <div class="tip">💡 Minimum 3 words — specific topics get better results</div>
      <div class="error-msg" id="errorMsg"></div>
      <div style="margin-top:1rem">
        <button class="generate-btn" id="generateBtn" onclick="generate()">
          <span>✨</span> Generate
        </button>
      </div>
    </div>

    <div class="output-card" id="outputCard">
      <div class="output-header">
        <h3 id="outputTitle">Your Content</h3>
        <button class="copy-btn" onclick="copyOutput()">Copy All</button>
      </div>
      <div class="output-text" id="outputText"></div>
    </div>
  </div>
</div>

<script>
let currentMode = 'captions';
let currentPlatform = 'tiktok';
let userData = {};

const modeTitles = {
  captions: 'Captions', hooks: 'Hooks', pov: 'POV Ideas',
  hashtags: 'Hashtags', bio: 'Bio', script: 'Video Script', trends: 'Trend Ideas',
  threads: 'X Thread'
};

async function loadUser() {
  const res = await fetch('/api/me');
  if (res.status === 401) { window.location.href = '/'; return; }
  userData = await res.json();

  document.getElementById('userEmail').textContent = userData.email;
  document.getElementById('userPlan').textContent = userData.plan === 'pro' ? '⭐ Pro' : 'Free';
  document.getElementById('userPlan').className = 'user-plan ' + (userData.plan === 'pro' ? 'plan-pro' : 'plan-free');
  document.getElementById('regionSelect').value = userData.region || 'global';

  if (userData.unlimited) {
    document.getElementById('usesBar').style.display = 'none';
    document.getElementById('upgradeCard').style.display = 'none';
  } else {
    const rem = userData.uses_remaining;
    document.getElementById('usesLabel').textContent = rem + '/5 uses left today';
    document.getElementById('barFill').style.width = (rem / 5 * 100) + '%';
  }
}

function setMode(mode, platform) {
  currentMode = mode;
  if (platform) { currentPlatform = platform; updatePlatformUI(); }

  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const navId = platform === 'x' ? 
    (mode === 'threads' ? 'nav-xthread' : mode === 'hooks' ? 'nav-xhooks' : 'nav-xtweets') :
    'nav-' + mode;
  const el = document.getElementById(navId);
  if (el) el.classList.add('active');

  document.getElementById('modeTitle').textContent = modeTitles[mode] || mode;
  document.getElementById('outputCard').classList.remove('visible');
}

function setPlatform(p) {
  currentPlatform = p;
  updatePlatformUI();
}

function updatePlatformUI() {
  document.getElementById('tiktokBtn').classList.toggle('active', currentPlatform === 'tiktok');
  document.getElementById('xBtn').classList.toggle('active', currentPlatform === 'x');
}

async function changeRegion() {
  const region = document.getElementById('regionSelect').value;
  await fetch('/api/set-region', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({region})
  });
}

async function generate() {
  const topic = document.getElementById('topicInput').value.trim();
  const btn = document.getElementById('generateBtn');
  const errEl = document.getElementById('errorMsg');
  const outputCard = document.getElementById('outputCard');
  const outputText = document.getElementById('outputText');

  errEl.style.display = 'none';

  if (!topic) { showError('Please enter a topic'); return; }
  if (topic.split(' ').length < 3) { showError('Be more specific — add more detail to your topic'); return; }

  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Generating...';
  outputCard.classList.remove('visible');

  const res = await fetch('/api/generate', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({mode: currentMode, topic, platform: currentPlatform})
  });

  const data = await res.json();
  btn.disabled = false;
  btn.innerHTML = '<span>✨</span> Generate';

  if (data.error) { showError(data.error); return; }

  outputText.textContent = data.result;
  document.getElementById('outputTitle').textContent = modeTitles[currentMode] + ' — ready to post';
  outputCard.classList.add('visible');
  outputCard.scrollIntoView({behavior: 'smooth', block: 'nearest'});

  if (!userData.unlimited && data.uses_remaining !== undefined) {
    document.getElementById('usesLabel').textContent = data.uses_remaining + '/5 uses left today';
    document.getElementById('barFill').style.width = (data.uses_remaining / 5 * 100) + '%';
  }
}

function showError(msg) {
  const el = document.getElementById('errorMsg');
  el.textContent = msg;
  el.style.display = 'block';
}

function copyOutput() {
  const text = document.getElementById('outputText').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy All', 2000);
  });
}

async function doUpgrade() {
  const res = await fetch('/api/upgrade', {method:'POST'});
  const data = await res.json();
  if (data.url) window.location.href = data.url;
}

async function doLogout() {
  await fetch('/api/logout', {method:'POST'});
  window.location.href = '/';
}

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') generate();
});

loadUser();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
