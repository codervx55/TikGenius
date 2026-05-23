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
    """Telegram bot is disabled. TikGenius now runs website-only."""
    return jsonify({"ok": True, "message": "Telegram bot disabled. Use the website."})

# ========================= HTML PAGES =========================
HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius — Go Viral. In Your Voice.</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#05070a;
  --surface:#0b1117;
  --card:#101820;
  --border:#1d2a35;
  --purple:#14b8a6;
  --purple-light:#38bdf8;
  --pink:#f59e0b;
  --text:#f8fafc;
  --muted:#8a99a8;
}
body{background:radial-gradient(circle at 50% -10%,rgba(20,184,166,.13),transparent 38%),var(--bg);color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;min-height:100vh;overflow-x:hidden;font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
h1,h2,h3,h4{font-family:'Space Grotesk',sans-serif;letter-spacing:-.035em}

/* NAV */
nav{display:flex;justify-content:space-between;align-items:center;padding:.9rem 1.2rem;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;background:rgba(5,7,10,0.88);backdrop-filter:blur(14px)}
.logo{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:1.12rem;background:linear-gradient(135deg,var(--text),var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.nav-btns{display:flex;gap:0.75rem}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text);padding:0.5rem 1.2rem;border-radius:8px;cursor:pointer;font-family:'Plus Jakarta Sans',sans-serif;font-size:0.9rem;transition:all 0.2s}
.btn-ghost:hover{border-color:var(--purple);color:var(--purple-light)}
.btn-primary{background:linear-gradient(135deg,var(--purple),var(--purple-light));border:none;color:#031013;padding:0.55rem 1.15rem;border-radius:10px;cursor:pointer;font-family:'Plus Jakarta Sans',sans-serif;font-size:0.88rem;font-weight:700;transition:opacity 0.2s}
.btn-primary:hover{opacity:0.9}

/* HERO */
.hero{text-align:center;padding:4.5rem 1.25rem 3rem;max-width:760px;margin:0 auto}
.hero-badge{display:inline-block;background:rgba(124,58,237,0.15);border:1px solid rgba(124,58,237,0.3);color:var(--purple-light);padding:0.4rem 1rem;border-radius:100px;font-size:0.85rem;margin-bottom:2rem}
.hero h1{font-size:clamp(2.15rem,9vw,4.2rem);font-weight:700;line-height:1.04;margin-bottom:1.15rem}
.hero h1 span{background:linear-gradient(135deg,var(--purple-light),var(--purple));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--muted);font-size:1.15rem;line-height:1.7;max-width:540px;margin:0 auto 2.5rem}
.hero-btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn-large{padding:0.9rem 2rem;border-radius:10px;font-size:1rem;font-weight:500;cursor:pointer;font-family:'Plus Jakarta Sans',sans-serif;transition:all 0.2s}
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
.step-num{width:48px;height:48px;border-radius:50%;background:linear-gradient(135deg,var(--purple),var(--pink));display:flex;align-items:center;justify-content:center;font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.1rem;margin:0 auto 1rem}
.step h3{font-size:1.1rem;margin-bottom:0.5rem}
.step p{color:var(--muted);font-size:0.9rem;line-height:1.6}

/* REGIONS */
.regions{display:flex;flex-wrap:wrap;gap:0.75rem;justify-content:center;margin-top:2rem}
.region-tag{background:var(--card);border:1px solid var(--border);padding:0.5rem 1.2rem;border-radius:100px;font-size:0.9rem}

/* PRICING */
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:2rem;margin-top:3rem;max-width:700px;margin-left:auto;margin-right:auto}
.price-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:2rem}
.price-card.featured{border-color:var(--purple);position:relative}
.price-card.featured::before{content:'MOST POPULAR';position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--purple),var(--pink));color:white;font-size:0.7rem;font-weight:700;padding:0.25rem 1rem;border-radius:100px;font-family:'Space Grotesk',sans-serif;letter-spacing:1px}
.price-label{color:var(--muted);font-size:0.85rem;margin-bottom:0.5rem}
.price-amount{font-family:'Space Grotesk',sans-serif;font-size:2.5rem;font-weight:800;margin-bottom:0.25rem}
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
.tab{flex:1;padding:0.6rem;text-align:center;border-radius:6px;cursor:pointer;font-size:0.9rem;transition:all 0.2s;border:none;background:transparent;color:var(--muted);font-family:'Plus Jakarta Sans',sans-serif}
.tab.active{background:var(--purple);color:white}
.form-group{margin-bottom:1rem}
.form-group label{display:block;font-size:0.85rem;color:var(--muted);margin-bottom:0.4rem}
.form-group input, .form-group select{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:0.75rem 1rem;border-radius:8px;font-family:'Plus Jakarta Sans',sans-serif;font-size:0.95rem;outline:none;transition:border-color 0.2s}
.form-group input:focus, .form-group select:focus{border-color:var(--purple)}
.form-error{color:#f87171;font-size:0.85rem;margin-top:0.5rem;display:none}
.btn-full{width:100%;padding:0.85rem;border-radius:8px;font-size:1rem;font-weight:500;cursor:pointer;font-family:'Plus Jakarta Sans',sans-serif;margin-top:0.5rem}

/* FOOTER */
footer{border-top:1px solid var(--border);padding:2rem;text-align:center;color:var(--muted);font-size:0.85rem}

/* RESPONSIVE */
@media(max-width:600px){
  nav{padding:.75rem 1rem}
  .logo{font-size:1rem}
  .nav-btns{gap:.5rem}
  .btn-ghost,.btn-primary{padding:.48rem .82rem;font-size:.82rem;border-radius:9px}
  .hero{padding:3.15rem 1rem 2.3rem}
  .hero-badge{font-size:.75rem;margin-bottom:1.1rem;padding:.32rem .78rem}
  .hero h1{font-size:2.05rem;line-height:1.04;margin-bottom:1rem}
  .hero p{font-size:.98rem;line-height:1.65}
  .section{padding:2.45rem 1rem}
  .section h2{font-size:1.75rem;line-height:1.12}
  .card,.example-card,.step{border-radius:18px}
}
</style>
</head>
<body>

<div class="glow"></div>

<nav>
  <div class="logo" style="display:flex;align-items:center;gap:10px;text-decoration:none">
    <svg width="34" height="34" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="100" cy="100" r="98" stroke="rgba(255,255,255,0.07)" stroke-width="1"/>
      <defs>
        <linearGradient id="tG" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#ffffff"/><stop offset="1" stop-color="rgba(255,255,255,0.7)"/></linearGradient>
        <linearGradient id="gG" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00c8ff"/><stop offset="1" stop-color="#a855f7"/></linearGradient>
      </defs>
      <rect x="52" y="58" width="52" height="7" rx="2" fill="url(#tG)"/>
      <rect x="74" y="65" width="8" height="70" rx="2" fill="url(#tG)"/>
      <path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#gG)" stroke-width="7" fill="none" stroke-linecap="round"/>
      <rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#gG)"/>
    </svg>
    <span>TikGenius</span>
  </div>
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
      <a href="https://t.me/TikGenius_bot" target="_blank"><button class="btn-large ghost">Open in Telegram</button></a>
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
  <style>
    .footer-logo{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:0.75rem}
    .footer-logo span{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.4rem;background:linear-gradient(135deg,#a855f7,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
    .footer-tagline{color:var(--muted);font-size:0.85rem;margin-bottom:1.5rem}
    .social-links{display:flex;gap:1rem;justify-content:center;margin-bottom:1.5rem;flex-wrap:wrap}
    .social-link{display:flex;align-items:center;gap:6px;padding:0.45rem 1rem;border-radius:100px;border:1px solid rgba(255,255,255,0.08);color:rgba(255,255,255,0.45);font-size:0.82rem;font-family:'Plus Jakarta Sans',sans-serif;text-decoration:none;transition:all 0.2s;background:rgba(255,255,255,0.03)}
    .social-link:hover{color:white;border-color:rgba(255,255,255,0.2);background:rgba(255,255,255,0.06)}
    .social-link svg{width:14px;height:14px;flex-shrink:0}
    .footer-copy{color:rgba(255,255,255,0.15);font-size:0.78rem}
  </style>

  <!-- Logo -->
  <div class="footer-logo">
    <svg width="32" height="32" viewBox="0 0 200 200" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="100" cy="100" r="98" stroke="rgba(255,255,255,0.07)" stroke-width="1"/>
      <defs>
        <linearGradient id="ftG" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#ffffff"/><stop offset="1" stop-color="rgba(255,255,255,0.7)"/></linearGradient>
        <linearGradient id="fgG" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00c8ff"/><stop offset="1" stop-color="#a855f7"/></linearGradient>
      </defs>
      <rect x="52" y="58" width="52" height="7" rx="2" fill="url(#ftG)"/>
      <rect x="74" y="65" width="8" height="70" rx="2" fill="url(#ftG)"/>
      <path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#fgG)" stroke-width="7" fill="none" stroke-linecap="round"/>
      <rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#fgG)"/>
    </svg>
    <span>TikGenius</span>
  </div>

  <div class="footer-tagline">AI content studio for TikTok &amp; X creators worldwide</div>

  <!-- Social links -->
  <div class="social-links">
    <a class="social-link" href="https://www.tiktok.com/@tik_genius_" target="_blank" rel="noopener">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.33 6.33 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.69a8.19 8.19 0 004.79 1.54V6.78a4.85 4.85 0 01-1.02-.09z"/></svg>
      TikTok
    </a>
    <a class="social-link" href="https://x.com/tikgenius" target="_blank" rel="noopener">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.747l7.73-8.835L1.254 2.25H8.08l4.259 5.631 5.905-5.631zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
      Twitter / X
    </a>
    <a class="social-link" href="https://t.me/tikgenius" target="_blank" rel="noopener">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
      Community
    </a>
    <a class="social-link" href="https://t.me/TikGenius_bot" target="_blank" rel="noopener">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>
      Telegram Bot
    </a>
  </div>

  <div class="footer-copy">&copy; 2025 TikGenius — Built for creators worldwide</div>
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
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#05070a;
  --sidebar:#071019;
  --surface:#0b1117;
  --card:#101820;
  --border:#1d2a35;
  --border-subtle:#14202b;
  --purple:#14b8a6;
  --purple-light:#38bdf8;
  --cyan:#22d3ee;
  --pink:#f59e0b;
  --text:#f8fafc;
  --text-2:#a8b3bf;
  --text-3:#64748b;
  --green:#34d399;
  --radius:14px;
}
body{background:radial-gradient(circle at 50% -15%,rgba(34,211,238,.10),transparent 36%),var(--bg);color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;min-height:100vh;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}

/* ── LAYOUT ─────────────────────────────────────────── */
.app{display:flex;min-height:100vh}

/* ── SIDEBAR ─────────────────────────────────────────── */
.sidebar{
  width:260px;flex-shrink:0;
  background:var(--sidebar);
  border-right:1px solid var(--border-subtle);
  display:flex;flex-direction:column;
  height:100vh;position:sticky;top:0;
  overflow-y:auto;
}
.sidebar::-webkit-scrollbar{width:0}

.sb-top{padding:20px 16px 0}

/* Logo */
.sb-logo{display:flex;align-items:center;gap:10px;margin-bottom:24px;padding:0 4px}
.sb-logo-text{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:17px;background:linear-gradient(135deg,#fff 35%,var(--purple-light));-webkit-background-clip:text;-webkit-text-fill-color:transparent}

/* New chat button */
.btn-new{
  display:flex;align-items:center;gap:8px;
  width:100%;padding:10px 12px;
  background:transparent;border:1px solid var(--border);
  border-radius:var(--radius);color:var(--text-2);
  font-family:'Plus Jakarta Sans',sans-serif;font-size:13px;font-weight:500;
  cursor:pointer;transition:all .15s;margin-bottom:24px;
  justify-content:center;
}
.btn-new:hover{border-color:var(--purple-light);color:var(--text);background:rgba(167,139,250,.06)}
.btn-new svg{opacity:.6}

/* Section headers */
.sb-section{padding:0 8px;margin-bottom:4px}
.sb-section-label{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);padding:0 4px;margin-bottom:6px}

/* Nav items */
.nav-item{
  display:flex;align-items:center;gap:10px;
  width:100%;padding:8px 12px;border-radius:8px;
  background:none;border:none;color:var(--text-2);
  font-family:'Plus Jakarta Sans',sans-serif;font-size:13.5px;font-weight:400;
  cursor:pointer;transition:all .12s;text-align:left;
  position:relative;
}
.nav-item:hover{background:var(--surface);color:var(--text)}
.nav-item.active{background:rgba(124,58,237,.12);color:var(--purple-light)}
.nav-item.active::before{
  content:'';position:absolute;left:0;top:50%;transform:translateY(-50%);
  width:3px;height:60%;border-radius:0 2px 2px 0;
  background:var(--purple-light);
}
.nav-icon{width:18px;height:18px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;opacity:.75}
.nav-item.active .nav-icon{opacity:1}
.pro-badge{margin-left:auto;font-size:10px;font-weight:700;letter-spacing:.04em;
  background:linear-gradient(135deg,var(--purple),var(--pink));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;flex-shrink:0}

/* Sidebar divider */
.sb-divider{height:1px;background:var(--border-subtle);margin:12px 16px}

/* Sidebar bottom */
.sb-bottom{margin-top:auto;padding:16px}

/* User chip */
.user-chip{
  background:var(--surface);border:1px solid var(--border-subtle);
  border-radius:var(--radius);padding:12px;margin-bottom:12px;
}
.user-email{font-size:12px;color:var(--text-2);font-weight:500;margin-bottom:6px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plan-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.plan-badge{font-size:11px;font-weight:600;padding:2px 8px;border-radius:100px}
.plan-free{background:rgba(82,82,91,.25);color:var(--text-3)}
.plan-pro{background:rgba(124,58,237,.2);color:var(--purple-light)}
.uses-label{font-size:11px;color:var(--text-3)}
.bar-track{height:3px;background:var(--border);border-radius:100px;overflow:hidden;margin-top:4px}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--cyan),var(--purple-light));transition:width .4s}

/* Upgrade card */
.upgrade-card{
  background:linear-gradient(135deg,rgba(124,58,237,.15),rgba(236,72,153,.1));
  border:1px solid rgba(124,58,237,.25);border-radius:var(--radius);
  padding:14px;margin-bottom:10px;
}
.upgrade-card-title{font-size:13px;font-weight:600;margin-bottom:3px}
.upgrade-card-sub{font-size:12px;color:var(--text-2);margin-bottom:10px;line-height:1.5}
.btn-upgrade{
  width:100%;background:linear-gradient(135deg,var(--purple),var(--pink));
  border:none;color:white;padding:9px;border-radius:8px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:13px;font-weight:600;
  cursor:pointer;transition:opacity .2s;
}
.btn-upgrade:hover{opacity:.88}

.btn-logout{
  display:flex;align-items:center;gap:8px;width:100%;padding:8px 12px;
  background:none;border:none;color:var(--text-3);font-family:'Plus Jakarta Sans',sans-serif;
  font-size:13px;cursor:pointer;border-radius:8px;transition:all .15s;
}
.btn-logout:hover{color:var(--text-2);background:var(--surface)}

/* ── MAIN AREA ────────────────────────────────────────── */
.main{flex:1;display:flex;flex-direction:column;min-height:100vh;max-width:820px;margin:0 auto;width:100%;padding:0 24px}

/* Top bar */
.topbar{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 0;border-bottom:1px solid var(--border-subtle);
  margin-bottom:32px;position:sticky;top:0;
  background:var(--bg);z-index:10;
}
.topbar-left{display:flex;align-items:center;gap:12px}
.page-title{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:19px;color:var(--text);letter-spacing:-.025em}
.platform-toggle{
  display:flex;background:var(--surface);border:1px solid var(--border-subtle);
  border-radius:8px;padding:3px;gap:2px;
}
.platform-btn{
  padding:5px 14px;border-radius:6px;border:none;
  background:transparent;color:var(--text-3);
  font-family:'Plus Jakarta Sans',sans-serif;font-size:12px;font-weight:500;
  cursor:pointer;transition:all .15s;
}
.platform-btn.active{background:var(--card);color:var(--text);box-shadow:0 1px 3px rgba(0,0,0,.3)}

/* Region select */
.region-select{
  background:var(--surface);border:1px solid var(--border-subtle);
  color:var(--text-2);padding:6px 10px;border-radius:8px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:12px;outline:none;cursor:pointer;
}
.region-select:focus{border-color:var(--purple-light)}

/* ── INPUT AREA (ChatGPT style) ──────────────────────── */
.input-section{margin-bottom:28px}
.input-label{font-size:13px;color:var(--text-3);margin-bottom:10px;font-weight:500}

.input-box{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:16px;
  transition:border-color .2s,box-shadow .2s;
  overflow:hidden;
}
.input-box:focus-within{
  border-color:rgba(124,58,237,.5);
  box-shadow:0 0 0 3px rgba(124,58,237,.08);
}
.topic-input{
  width:100%;background:transparent;border:none;
  color:var(--text);padding:18px 20px 12px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;
  outline:none;resize:none;line-height:1.6;
  min-height:120px;
}
.topic-input::placeholder{color:var(--text-3)}

.input-footer{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px 10px 20px;border-top:1px solid var(--border-subtle);
}
.input-hint{font-size:12px;color:var(--text-3)}
.input-hint span{color:var(--purple-light)}

.btn-generate{
  display:flex;align-items:center;gap:8px;
  background:linear-gradient(135deg,var(--purple),var(--pink));
  border:none;color:white;padding:10px 20px;border-radius:10px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:14px;font-weight:600;
  cursor:pointer;transition:opacity .2s,transform .15s;flex-shrink:0;
}
.btn-generate:hover{opacity:.9;transform:translateY(-1px)}
.btn-generate:disabled{opacity:.4;cursor:not-allowed;transform:none}

/* Spinner */
.spinner{width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:white;border-radius:50%;animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

/* Error */
.error-msg{
  background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);
  color:#fca5a5;padding:10px 14px;border-radius:10px;
  font-size:13px;margin-top:10px;display:none;
}

/* ── OUTPUT ────────────────────────────────────────────── */
.output-wrap{display:none;animation:fadeSlide .3s ease}
.output-wrap.visible{display:block}
@keyframes fadeSlide{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

.output-header{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:14px;
}
.output-label{
  display:flex;align-items:center;gap:8px;
  font-size:12px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:var(--text-3);
}
.output-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

.btn-copy{
  display:flex;align-items:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);
  color:var(--text-2);padding:6px 14px;border-radius:8px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:12px;font-weight:500;
  cursor:pointer;transition:all .15s;
}
.btn-copy:hover{border-color:var(--purple-light);color:var(--text)}

.output-card{
  background:var(--card);border:1px solid var(--border-subtle);
  border-radius:16px;padding:24px;
}
.output-text{
  white-space:pre-wrap;line-height:1.85;font-size:14.5px;
  color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;
}

/* ── MOBILE ──────────────────────────────────────────── */
.mob-bar{display:none;align-items:center;justify-content:space-between;
  padding:14px 16px;border-bottom:1px solid var(--border-subtle);
  background:var(--sidebar);position:sticky;top:0;z-index:50}
.mob-logo{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:16px;
  background:linear-gradient(135deg,#fff,var(--purple-light));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.mob-menu-btn{background:none;border:none;color:var(--text-2);cursor:pointer;padding:4px}
.mob-drawer{
  display:none;position:fixed;inset:0;z-index:100;
}
.mob-drawer.open{display:flex}
.mob-drawer-bg{position:absolute;inset:0;background:rgba(0,0,0,.6);backdrop-filter:blur(4px)}
.mob-drawer-panel{
  position:relative;width:280px;background:var(--sidebar);
  border-right:1px solid var(--border-subtle);
  height:100%;overflow-y:auto;display:flex;flex-direction:column;
  animation:slideIn .2s ease;
}
@keyframes slideIn{from{transform:translateX(-100%)}to{transform:translateX(0)}}

@media(max-width:768px){
  .sidebar{display:none}
  .mob-bar{display:flex;height:56px;padding:0 14px;background:rgba(5,7,10,.88);backdrop-filter:blur(14px)}
  .mob-logo{font-family:'Space Grotesk',sans-serif;font-size:17px;letter-spacing:-.02em}
  .main{padding:0 14px}
  .topbar{position:static;margin-bottom:16px;padding:12px 0}
  .page-title{font-size:17px}
  .platform-btn{padding:5px 10px;font-size:12px}
  .composer-title{font-size:1.65rem;line-height:1.1}
  .composer-sub{font-size:.92rem;line-height:1.55}
  .topic-input{font-size:14px;min-height:110px}
  .input-footer{padding:10px 12px;gap:10px}
  .btn-generate{padding:10px 14px;border-radius:10px;font-size:13px}
  .output-card,.input-card{border-radius:18px}
}
</style>
</head>
<body>

<!-- Mobile top bar -->
<div class="mob-bar">
  <div class="mob-logo">TikGenius</div>
  <div style="display:flex;align-items:center;gap:10px">
    <select class="region-select" id="regionSelectMob" onchange="changeRegion(this.value)" style="font-size:11px">
      <option value="nigeria">🇳🇬 Nigeria</option>
      <option value="usa">🇺🇸 USA</option>
      <option value="uk">🇬🇧 UK</option>
      <option value="caribbean">🇯🇲 Caribbean</option>
      <option value="eastafrica">🇰🇪 East Africa</option>
      <option value="southafrica">🇿🇦 South Africa</option>
      <option value="global" selected>🌍 Global</option>
    </select>
    <button class="mob-menu-btn" onclick="openDrawer()">
      <svg width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <path d="M4 6h16M4 12h16M4 18h16"/>
      </svg>
    </button>
  </div>
</div>

<!-- Mobile drawer -->
<div class="mob-drawer" id="mobDrawer">
  <div class="mob-drawer-bg" onclick="closeDrawer()"></div>
  <div class="mob-drawer-panel" id="mobPanel">
    <!-- filled by JS -->
  </div>
</div>

<div class="app">

  <!-- ── SIDEBAR ── -->
  <aside class="sidebar">
    <div class="sb-top">
      <div class="sb-logo">
        <svg width="28" height="28" viewBox="0 0 200 200" fill="none">
          <circle cx="100" cy="100" r="98" stroke="rgba(255,255,255,0.08)" stroke-width="1.5"/>
          <defs>
            <linearGradient id="dTG" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#fff"/><stop offset="1" stop-color="rgba(255,255,255,.65)"/></linearGradient>
            <linearGradient id="dGG" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00c8ff"/><stop offset="1" stop-color="#a855f7"/></linearGradient>
          </defs>
          <rect x="52" y="58" width="52" height="7" rx="2" fill="url(#dTG)"/>
          <rect x="74" y="65" width="8" height="70" rx="2" fill="url(#dTG)"/>
          <path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#dGG)" stroke-width="7" fill="none" stroke-linecap="round"/>
          <rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#dGG)"/>
        </svg>
        <span class="sb-logo-text">TikGenius</span>
      </div>

      <!-- TikTok tools -->
      <div class="sb-section">
        <div class="sb-section-label">TikTok</div>
        <button class="nav-item active" onclick="setMode('captions')" id="nav-captions">
          <span class="nav-icon">✍️</span> Captions
        </button>
        <button class="nav-item" onclick="setMode('hooks')" id="nav-hooks">
          <span class="nav-icon">🎣</span> Hooks
        </button>
        <button class="nav-item" onclick="setMode('pov')" id="nav-pov">
          <span class="nav-icon">🎥</span> POV Ideas
        </button>
        <button class="nav-item" onclick="setMode('hashtags')" id="nav-hashtags">
          <span class="nav-icon">📊</span> Hashtags
        </button>
        <button class="nav-item" onclick="setMode('bio')" id="nav-bio">
          <span class="nav-icon">👤</span> Bio
        </button>
        <button class="nav-item" onclick="setMode('script')" id="nav-script">
          <span class="nav-icon">📝</span> Script <span class="pro-badge">PRO</span>
        </button>
        <button class="nav-item" onclick="setMode('trends')" id="nav-trends">
          <span class="nav-icon">📈</span> Trends <span class="pro-badge">PRO</span>
        </button>
      </div>

      <div class="sb-divider"></div>

      <!-- X / Twitter tools -->
      <div class="sb-section">
        <div class="sb-section-label">Twitter / X</div>
        <button class="nav-item" onclick="setMode('captions','x')" id="nav-xtweets">
          <span class="nav-icon">𝕏</span> Tweets
        </button>
        <button class="nav-item" onclick="setMode('hooks','x')" id="nav-xhooks">
          <span class="nav-icon">🧲</span> Thread Hooks
        </button>
        <button class="nav-item" onclick="setMode('threads','x')" id="nav-xthread">
          <span class="nav-icon">🧵</span> Full Thread <span class="pro-badge">PRO</span>
        </button>
      </div>
    </div>

    <div class="sb-bottom">
      <!-- User info -->
      <div class="user-chip">
        <div class="user-email" id="userEmail">Loading...</div>
        <div class="plan-row">
          <span class="plan-badge plan-free" id="userPlan">Free</span>
          <span class="uses-label" id="usesLabel">5/5 uses left</span>
        </div>
        <div class="bar-track" id="usesBar">
          <div class="bar-fill" id="barFill" style="width:100%"></div>
        </div>
      </div>

      <!-- Upgrade -->
      <div class="upgrade-card" id="upgradeCard">
        <div class="upgrade-card-title">Upgrade to Pro</div>
        <div class="upgrade-card-sub">Unlimited scripts, threads, trends &amp; more.</div>
        <button class="btn-upgrade" onclick="doUpgrade()">Go Pro — ₦2,000/mo</button>
      </div>

      <button class="btn-logout" onclick="doLogout()">
        <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
        Log out
      </button>
    </div>
  </aside>

  <!-- ── MAIN ── -->
  <main class="main">

    <!-- Top bar -->
    <div class="topbar">
      <div class="topbar-left">
        <div class="page-title" id="modeTitle">Captions</div>
        <div class="platform-toggle" id="platformToggle">
          <button class="platform-btn active" id="tiktokBtn" onclick="setPlatform('tiktok')">TikTok</button>
          <button class="platform-btn" id="xBtn" onclick="setPlatform('x')">Twitter / X</button>
        </div>
      </div>
      <select class="region-select" id="regionSelect" onchange="changeRegion(this.value)">
        <option value="nigeria">🇳🇬 Nigerian</option>
        <option value="usa">🇺🇸 American</option>
        <option value="uk">🇬🇧 British</option>
        <option value="caribbean">🇯🇲 Caribbean</option>
        <option value="eastafrica">🇰🇪 East African</option>
        <option value="southafrica">🇿🇦 South African</option>
        <option value="global" selected>🌍 Global</option>
      </select>
    </div>

    <!-- Input -->
    <div class="input-section">
      <div class="input-box">
        <textarea class="topic-input" id="topicInput" rows="4"
          placeholder="What is your video or post about?

Be specific — the more detail you give, the better the output.
Example: I work so hard but I am still broke"></textarea>
        <div class="input-footer">
          <span class="input-hint">⌘ + Enter to generate &nbsp;·&nbsp; Min <span>3 words</span></span>
          <button class="btn-generate" id="generateBtn" onclick="generate()">
            <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
            Generate
          </button>
        </div>
      </div>
      <div class="error-msg" id="errorMsg"></div>
    </div>

    <!-- Output -->
    <div class="output-wrap" id="outputCard">
      <div class="output-header">
        <div class="output-label">
          <div class="output-dot"></div>
          <span id="outputTitle">Ready to post</span>
        </div>
        <button class="btn-copy" onclick="copyOutput()">
          <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
          Copy All
        </button>
      </div>
      <div class="output-card">
        <div class="output-text" id="outputText"></div>
      </div>
    </div>

  </main>
</div>

<script>
let currentMode = 'captions';
let currentPlatform = 'tiktok';
let userData = {};

const modeTitles = {
  captions:'Captions', hooks:'Hooks', pov:'POV Ideas',
  hashtags:'Hashtags', bio:'Bio', script:'Video Script', trends:'Trend Ideas',
  threads:'X Thread'
};

// ── Sidebar HTML for mobile drawer ──────────────────
function sidebarHTML() {
  return document.querySelector('.sidebar').innerHTML;
}

function openDrawer() {
  document.getElementById('mobPanel').innerHTML = sidebarHTML();
  document.getElementById('mobDrawer').classList.add('open');
}
function closeDrawer() {
  document.getElementById('mobDrawer').classList.remove('open');
}

// ── Load user ────────────────────────────────────────
async function loadUser() {
  const res = await fetch('/api/me');
  if (res.status === 401) { window.location.href = '/'; return; }
  userData = await res.json();

  document.getElementById('userEmail').textContent = userData.email;
  const isPro = userData.plan === 'pro';
  document.getElementById('userPlan').textContent = isPro ? '⭐ Pro' : 'Free';
  document.getElementById('userPlan').className = 'plan-badge ' + (isPro ? 'plan-pro' : 'plan-free');

  const sel = document.getElementById('regionSelect');
  const selMob = document.getElementById('regionSelectMob');
  if (sel) sel.value = userData.region || 'global';
  if (selMob) selMob.value = userData.region || 'global';

  if (userData.unlimited) {
    document.getElementById('usesBar').style.display = 'none';
    const uc = document.getElementById('upgradeCard');
    if (uc) uc.style.display = 'none';
    document.getElementById('usesLabel').textContent = 'Unlimited ✓';
  } else {
    const rem = userData.uses_remaining;
    document.getElementById('usesLabel').textContent = rem + '/5 uses left today';
    document.getElementById('barFill').style.width = (rem / 5 * 100) + '%';
  }
}

// ── Mode / Platform ──────────────────────────────────
function setMode(mode, platform) {
  currentMode = mode;
  if (platform) { currentPlatform = platform; updatePlatformUI(); }

  document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
  const navId = platform === 'x'
    ? (mode === 'threads' ? 'nav-xthread' : mode === 'hooks' ? 'nav-xhooks' : 'nav-xtweets')
    : 'nav-' + mode;
  const el = document.getElementById(navId);
  if (el) el.classList.add('active');

  document.getElementById('modeTitle').textContent = modeTitles[mode] || mode;
  document.getElementById('outputCard').classList.remove('visible');
  closeDrawer();
}

function setPlatform(p) {
  currentPlatform = p;
  updatePlatformUI();
}

function updatePlatformUI() {
  document.getElementById('tiktokBtn').classList.toggle('active', currentPlatform === 'tiktok');
  document.getElementById('xBtn').classList.toggle('active', currentPlatform === 'x');
}

// ── Region ───────────────────────────────────────────
async function changeRegion(val) {
  const region = val || document.getElementById('regionSelect').value;
  // sync both selects
  ['regionSelect','regionSelectMob'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = region;
  });
  await fetch('/api/set-region', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({region})
  });
}

// ── Generate ─────────────────────────────────────────
async function generate() {
  const topic = document.getElementById('topicInput').value.trim();
  const btn = document.getElementById('generateBtn');
  const errEl = document.getElementById('errorMsg');
  const outputCard = document.getElementById('outputCard');
  const outputText = document.getElementById('outputText');

  errEl.style.display = 'none';
  if (!topic) { showError('Please enter a topic.'); return; }
  if (topic.split(' ').length < 3) { showError('Be more specific — add at least 3 words.'); return; }

  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Generating...';
  outputCard.classList.remove('visible');

  const res = await fetch('/api/generate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode: currentMode, topic, platform: currentPlatform})
  });

  const data = await res.json();
  btn.disabled = false;
  btn.innerHTML = '<svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Generate';

  if (data.error) { showError(data.error); return; }

  outputText.textContent = data.result;
  document.getElementById('outputTitle').textContent = modeTitles[currentMode] + ' — ready to post';
  outputCard.classList.add('visible');
  outputCard.scrollIntoView({behavior:'smooth', block:'nearest'});

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

// ── Copy ─────────────────────────────────────────────
function copyOutput() {
  const text = document.getElementById('outputText').textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.btn-copy');
    btn.innerHTML = '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg> Copied!';
    setTimeout(() => {
      btn.innerHTML = '<svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg> Copy All';
    }, 2000);
  });
}

// ── Upgrade / Logout ─────────────────────────────────
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
