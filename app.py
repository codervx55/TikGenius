cat > /mnt/user-data/outputs/app.py << 'ENDOFFILE'
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
from flask import Flask, request, jsonify, session, redirect, Response
from html import escape
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
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@tikgenius.app")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", SECRET_KEY)
ADMIN_EXPORT_KEY = os.getenv("ADMIN_EXPORT_KEY", SECRET_KEY)

PRICE_KOBO = 200000
FREE_LIMIT = 5
FREE_RESULTS = 4
PRO_RESULTS = 7
ADMIN_TG_ID = "6415641863"

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

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
            cur.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                plan TEXT DEFAULT 'free',
                expires DATE,
                activated_at TIMESTAMP,
                usage_date DATE,
                usage_count INTEGER DEFAULT 0,
                region TEXT DEFAULT 'global'
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS web_users (
                id SERIAL PRIMARY KEY,
                name TEXT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                plan TEXT DEFAULT 'free',
                expires DATE,
                usage_date DATE,
                usage_count INTEGER DEFAULT 0,
                region TEXT DEFAULT 'global',
                created_at TIMESTAMP DEFAULT NOW(),
                last_login_at TIMESTAMP
            )""")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS name TEXT")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP")
            cur.execute("""CREATE TABLE IF NOT EXISTS web_generations (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES web_users(id) ON DELETE CASCADE,
                mode TEXT,
                platform TEXT,
                topic TEXT,
                result TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS web_payments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES web_users(id) ON DELETE SET NULL,
                telegram_id BIGINT,
                reference TEXT UNIQUE NOT NULL,
                amount_kobo INTEGER NOT NULL DEFAULT 0,
                currency TEXT DEFAULT 'NGN',
                status TEXT DEFAULT 'success',
                source TEXT DEFAULT 'web',
                paid_at TIMESTAMP DEFAULT NOW(),
                raw_email TEXT
            )""")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= AUTH =========================
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

def record_payment(reference, amount_kobo, currency="NGN", status="success", source="web", web_user_id=None, telegram_id=None, raw_email=None):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO web_payments
                (user_id, telegram_id, reference, amount_kobo, currency, status, source, raw_email)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reference) DO NOTHING
            """, (web_user_id, telegram_id, reference, int(amount_kobo or 0), currency or "NGN", status or "success", source or "web", raw_email))
        conn.commit()
    finally:
        release_db(conn)

# ========================= TELEGRAM HELPERS =========================
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

# ========================= REGIONS =========================
REGION_VOICES = {
    "nigeria": "You write for Nigerian creators. You understand Nigerian internet culture — the hustle, NEPA, soft life dreams, family pressure, this economy, Lagos life, glow ups, faith and doubt.",
    "usa": "You write for American creators. You understand US TikTok — the slang, therapy-speak, hustle culture critique, main character energy, manifestation era.",
    "uk": "You write for British creators. You understand UK TikTok — dry humour, roadman culture, British understatement, council estate to success stories.",
    "caribbean": "You write for Caribbean creators — Jamaican, Trinidadian, Barbadian. You understand the culture, patois energy, the vibes, the pride, the grind.",
    "eastafrica": "You write for East African creators — Kenyan, Ugandan, Tanzanian. You understand the hustle, Nairobi life, the culture, the ambition, the faith.",
    "southafrica": "You write for South African creators. You understand SA TikTok — township energy, Joburg life, amapiano culture, load shedding jokes.",
    "global": "You write for creators worldwide. Focus on emotions and truths that cross all cultures: hustle, love, growth, struggle, success, identity."
}

REGION_NAMES = {
    "nigeria": "🇳🇬 Nigerian", "usa": "🇺🇸 American", "uk": "🇬🇧 British",
    "caribbean": "🇯🇲 Caribbean", "eastafrica": "🇰🇪 East African",
    "southafrica": "🇿🇦 South African", "global": "🌍 Global"
}

# ========================= POST TIMING =========================
POSTING_TIMES = {
    "tiktok": [
        ("Monday", "7:00 AM", "Morning commute scroll — high engagement before work"),
        ("Tuesday", "12:00 PM", "Lunch break — people browsing while eating"),
        ("Wednesday", "7:00 PM", "Evening wind-down — peak TikTok usage time"),
        ("Thursday", "9:00 PM", "Pre-weekend energy — people are in a good mood"),
        ("Friday", "6:00 PM", "Friday evening — highest engagement of the week"),
        ("Saturday", "11:00 AM", "Weekend morning — people relaxed and scrolling"),
        ("Sunday", "8:00 PM", "Sunday evening — prep for the week mindset"),
    ],
    "x": [
        ("Monday", "8:00 AM", "Start of week — people catching up on X"),
        ("Tuesday", "9:00 AM", "Weekday morning — highest X engagement window"),
        ("Wednesday", "12:00 PM", "Midweek lunch — people checking X during break"),
        ("Thursday", "6:00 PM", "After work — people unwinding on X"),
        ("Friday", "9:00 AM", "Friday morning — high X activity before weekend"),
        ("Saturday", "10:00 AM", "Weekend morning — relaxed browsing"),
        ("Sunday", "7:00 PM", "Sunday evening — active X conversations"),
    ]
}

# ========================= AI PROMPTS =========================
TIKTOK_SYSTEM = """You are TikGenius — a viral TikTok content strategist who has studied millions of viral posts globally.

You write content that:
- Triggers an emotion in the first 3 words
- Makes people feel seen, called out, or deeply understood
- Is simple enough for anyone to get instantly
- Has a second line that surprises, twists, or lands like a punch

{region_voice}

PUNCTUATION: Never use "..." — use a dash ( — ), line break, or full stop instead.
LANGUAGE: Clean modern English. Casual, real, emotional, sharp. Never sound like a motivational poster."""

X_SYSTEM = """You are XGenius — a Twitter/X content strategist who understands virality deeply.

{region_voice}

Your content is bold, quotable, sharp. The kind people screenshot and send to their group chat.
Every tweet under 280 characters. Clean English. No ellipsis (...)."""

TIKTOK_PROMPTS = {
"hooks": """Write {count} TikTok hooks for: {topic}

Study these viral hooks:
"Nobody is coming to save you. Build yourself." — Direct, activates the ego
"The version of me from 2 years ago would not recognise me." — Curiosity + transformation
"Tell me why I worked this hard just to still be stressed 😭" — Funny + relatable
"POV: you finally got everything you asked for. You're still not satisfied." — Honest truth

Write {count} ORIGINAL hooks:
- Trigger emotion in FIRST 3 WORDS
- Mix tones: inspiring, funny, painfully honest
- Under 20 words each
- No ellipsis
- Format: 1) 2) 3) etc""",

"captions": """Write {count} TikTok captions for: {topic}

Study these viral captions:
"I used to shrink myself for people who weren't even paying attention. Never again." — Past + painful truth + declaration
"God will give you the life you prayed for. Just not in the timeline you imagined. 😭" — Promise + twist
"Nobody prepared me for how lonely success would feel before it arrived." — Raw honest truth
"This time last year I was crying about something that doesn't even matter anymore. Growth." — Contrast + punchline

Write {count} ORIGINAL captions:
- Every caption needs a TWIST — line 2 flips line 1
- 1 to 3 sentences max
- No ellipsis
- ONE emoji maximum
- Format: 1) 2) 3) etc""",

"pov": """Write {count} TikTok POV concepts for: {topic}

Study these viral POVs:
"POV: You finally stopped chasing people who were never running towards you."
"POV: You worked in silence for 2 years. Now everyone wants to know your secret."
"POV: You are exhausted. Not lazy. Not ungrateful. Just genuinely, deeply exhausted."

Write {count} ORIGINAL POVs:
- Each describes a SPECIFIC feeling or moment
- So specific someone thinks "how did they know"
- No ellipsis
- Format: 1) POV: [scenario]""",

"hashtags": """Generate {count} strategic TikTok hashtag sets for: {topic}

Each set: exactly 7 hashtags
- 2 massive reach tags: #fyp #foryoupage #tiktok #viral
- 2 medium reach: topic-specific tags people search
- 2 niche tags: very specific to content
- 1 community tag for the region

Format:
Set 1: #tag #tag #tag #tag #tag #tag #tag
(one set per line, nothing else)""",

"bio": """Write {count} TikTok bios for niche: {topic}

Study these bios:
"building the life I used to dream about 🤫 | tips + real talk"
"I left the 9-5. Now I film my life. 📹 | come along"
"healing out loud so you don't have to do it alone 🖤"

Write {count} ORIGINAL bios:
- Under 80 characters each
- Clear personality + content promise
- No ellipsis
- Format: 1) 2) 3) etc""",

"script": """Write a complete TikTok video script for: {topic}

[HOOK — 0-3 seconds]
First line. Immediate emotional reaction. Under 15 words.

[BODY — 4-45 seconds]
How a real creator SPEAKS on camera. Short sentences. No filler.

[PUNCHLINE — 45-55 seconds]
The one line people screenshot. The gut punch.

[CTA — 55-60 seconds]
One question that makes people comment, save, or share.

Rules: 130-160 words max. No ellipsis.""",

"trends": """Generate {count} specific TikTok video ideas for: {topic}

For each idea:
Idea [N]: [Video title written like a caption]
Hook: [Exact first line — stops scroll in 2 seconds]
Format: [storytime / POV / talking to camera / voiceover / text on screen]
Why it works: [1 sentence on the psychology]

Base on formats that go viral: storytimes, "things nobody tells you", POV setups, "I tried X for 30 days", transformation, honest takes.
No intro."""
}

X_PROMPTS = {
"captions": """Write {count} viral Twitter/X posts about: {topic}

Study these:
"Stop romanticising the struggle. Rest is not laziness. Recovery is not weakness."
"Not every chapter of your life needs an audience." — 9 words. Universal.
"Success without peace is just a well-funded anxiety attack."
"The energy you protect this year will determine what you build next year."

Write {count} ORIGINAL tweets:
- Each under 280 characters
- Bold, quotable, screenshot-worthy
- No ellipsis
- Format: 1) 2) 3) etc""",

"hooks": """Write {count} Twitter/X thread starter hooks about: {topic}

Study these:
"I spent 3 years building something. Nobody saw it. Then everything changed in 90 days:"
"10 things nobody tells you about [topic] (but should):"
"Why everything you've been told about [topic] is making things worse:"

Write {count} ORIGINAL thread hooks:
- Immediate curiosity or emotional reaction
- Under 30 words each
- Format: 1) 2) 3) etc""",

"threads": """Write a complete Twitter/X thread about: {topic}

Tweet 1 — HOOK: [Makes people click "show this thread"]
Tweet 2 — CONTEXT: [Set up the problem personally]
Tweet 3 — THE TRUTH: [Reframes how they see the topic]
Tweet 4 — GO DEEPER: [Specific example or evidence]
Tweet 5 — THE TWIST: [Unexpected angle]
Tweet 6 — PRACTICAL: [What to actually do]
Tweet 7 — CLOSE: [Most quotable line — goes in someone's bio]

Each tweet under 280 characters. No ellipsis."""
}

# ========================= AI FUNCTION =========================
def ask_ai(mode, topic, platform="tiktok", region="global", is_pro_user=False):
    count = PRO_RESULTS if is_pro_user else FREE_RESULTS
    region_voice = REGION_VOICES.get(region, REGION_VOICES["global"])

    if platform == "x":
        system = X_SYSTEM.format(region_voice=region_voice)
        prompt_template = X_PROMPTS.get(mode, X_PROMPTS["captions"])
    else:
        system = TIKTOK_SYSTEM.format(region_voice=region_voice)
        prompt_template = TIKTOK_PROMPTS.get(mode, TIKTOK_PROMPTS["captions"])

    # Script and threads don't use count
    if mode in ("script", "threads"):
        prompt = prompt_template.format(topic=topic)
    else:
        prompt = prompt_template.format(topic=topic, count=count)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.92,
        "max_tokens": 1800,
        "top_p": 0.95
    }

    try:
        res = http_session.post(url, json=payload, headers=headers, timeout=30)
        data = res.json()
        if "choices" in data and data["choices"]:
            return data["choices"][0]["message"]["content"].strip()
        print(f"Groq error: {data}")
        return None
    except Exception as e:
        print(f"Groq Error: {e}")
        return None

def parse_results(raw, platform="tiktok", is_pro_user=False):
    """
    Parse AI output into a list of individual result strings.
    Each result is one numbered item.
    """
    if not raw:
        return []

    lines = raw.strip().split("\n")
    results = []
    current = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Detect start of a new numbered item: 1) or 1. or Set 1: or Idea 1: or Tweet 1
        import re
        is_new = bool(re.match(r'^(\d+[\)\.]|Set \d+:|Idea \d+:|Tweet \d+)', stripped))
        if is_new and current:
            results.append("\n".join(current).strip())
            current = [stripped]
        else:
            current.append(stripped)

    if current:
        results.append("\n".join(current).strip())

    # Limit to count
    count = PRO_RESULTS if is_pro_user else FREE_RESULTS
    return results[:count]

def get_posting_times(platform, count):
    times = POSTING_TIMES.get(platform, POSTING_TIMES["tiktok"])
    selected = []
    used = []
    while len(selected) < count:
        available = [t for t in times if t not in used]
        if not available:
            used = []
            available = times
        pick = random.choice(available)
        selected.append(pick)
        used.append(pick)
    return selected

# ========================= PAYMENT =========================
def create_payment_link(email, user_id, source="web"):
    reference = f"TG-{user_id}-{source}-{int(datetime.utcnow().timestamp())}"
    callback = ""
    try:
        callback = request.host_url.rstrip("/") + "/paystack/callback"
    except:
        pass
    payload = {
        "email": email,
        "amount": PRICE_KOBO,
        "reference": reference,
        "callback_url": callback,
        "metadata": {
            "web_user_id": user_id if source == "web" else None,
            "telegram_id": user_id if source == "telegram" else None,
            "source": source
        }
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

# ========================= WEB API ROUTES =========================
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
    name = data.get("name", "").strip()
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
            cur.execute("""INSERT INTO web_users (name, email, password_hash, region)
                VALUES (%s, %s, %s, %s) RETURNING id""",
                (name, email, generate_password_hash(password), region))
            user_id = cur.fetchone()["id"]
        conn.commit()
        session.permanent = True
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
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET last_login_at=NOW() WHERE id=%s", (user["id"],))
        conn.commit()
        session.permanent = True
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
        "unlimited": pro,
        "result_count": PRO_RESULTS if pro else FREE_RESULTS
    })

@app.route("/api/history")
@login_required
def history():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, mode, platform, topic, result, created_at
                FROM web_generations WHERE user_id=%s
                ORDER BY created_at DESC LIMIT 30""", (session["user_id"],))
            rows = cur.fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        release_db(conn)

@app.route("/api/history/clear", methods=["POST", "DELETE"])
@login_required
def clear_history():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM web_generations WHERE user_id=%s", (session["user_id"],))
        conn.commit()
        return jsonify({"success": True})
    finally:
        release_db(conn)

@app.route("/api/generate", methods=["POST"])
@login_required
def generate():
    data = request.json or {}
    mode = data.get("mode", "captions")
    topic = data.get("topic", "").strip()
    platform = data.get("platform", "tiktok")

    if not topic:
        return jsonify({"error": "Please enter a topic"}), 400
    if len(topic.split()) < 3:
        return jsonify({"error": "Be more specific — add at least 3 words"}), 400

    user_id = session["user_id"]
    pro = is_web_pro(user_id)

    if not check_and_increment_web_usage(user_id):
        return jsonify({"error": "free_limit"}), 429

    user = get_web_user(user_id)
    region = user["region"] if user else "global"
    raw = ask_ai(mode, topic, platform, region, pro)

    if not raw:
        return jsonify({"error": "Generation failed. Please try again."}), 500

    # Parse into individual results
    results = parse_results(raw, platform, pro)

    # Attach posting times to each result
    if mode not in ("script", "threads"):
        times = get_posting_times(platform, len(results))
        enriched = []
        for i, (result, time_info) in enumerate(zip(results, times)):
            enriched.append({
                "text": result,
                "post_day": time_info[0],
                "post_time": time_info[1],
                "post_reason": time_info[2]
            })
    else:
        enriched = [{"text": raw, "post_day": None, "post_time": None, "post_reason": None}]

    # Save to history
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO web_generations (user_id, mode, platform, topic, result)
                VALUES (%s, %s, %s, %s, %s)""", (user_id, mode, platform, topic, raw))
        conn.commit()
    finally:
        release_db(conn)

    return jsonify({
        "results": enriched,
        "is_pro": pro,
        "uses_remaining": web_uses_remaining(user_id) if not pro else None,
        "unlimited": pro
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

# ========================= PAYSTACK =========================
def verify_paystack_reference(reference):
    if not PAYSTACK_SECRET_KEY or not reference:
        return False, "Missing config"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get(f"https://api.paystack.co/transaction/verify/{reference}",
                               headers=headers, timeout=20)
        data = res.json()
    except Exception as e:
        return False, str(e)

    if not data.get("status") or data.get("data", {}).get("status") != "success":
        return False, "Payment not successful"

    tx = data["data"]
    if int(tx.get("amount", 0)) < PRICE_KOBO:
        return False, "Amount too low"

    metadata = tx.get("metadata") or {}
    source = metadata.get("source", "web")
    ref = tx.get("reference") or reference
    customer = tx.get("customer") or {}
    paid_email = customer.get("email")

    if source == "web":
        web_user_id = metadata.get("web_user_id")
        if not web_user_id:
            return False, "Missing web user ID"
        record_payment(ref, tx.get("amount"), tx.get("currency", "NGN"), "success", "web", int(web_user_id), None, paid_email)
        expires = activate_web_pro(int(web_user_id))
        return True, f"Premium activated until {expires}"

    telegram_id = metadata.get("telegram_id")
    if telegram_id:
        record_payment(ref, tx.get("amount"), tx.get("currency", "NGN"), "success", "telegram", None, int(telegram_id), paid_email)
        expires = activate_pro(telegram_id)
        send_telegram_message(telegram_id, f"Payment confirmed. Pro active till {expires}. Everything unlocked.")
        return True, f"Telegram premium activated until {expires}"

    return False, "Missing user metadata"

@app.route("/paystack/callback")
def paystack_callback():
    reference = request.args.get("reference") or request.args.get("trxref")
    ok, message = verify_paystack_reference(reference)
    if ok:
        return redirect("/dashboard?payment=success")
    return f"Payment verification failed: {message}", 400

@app.route("/paystack-webhook", methods=["POST"])
@app.route("/paystack/webhook", methods=["POST"])
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
            reference = data.get("reference")
            customer = data.get("customer") or {}
            paid_email = customer.get("email")
            if source == "web":
                web_user_id = metadata.get("web_user_id")
                if web_user_id:
                    record_payment(reference, amount, data.get("currency", "NGN"), "success", "web", int(web_user_id), None, paid_email)
                    activate_web_pro(web_user_id)
            else:
                telegram_id = metadata.get("telegram_id")
                if telegram_id:
                    record_payment(reference, amount, data.get("currency", "NGN"), "success", "telegram", None, int(telegram_id), paid_email)
                    expires = activate_pro(telegram_id)
                    send_telegram_message(telegram_id, f"Payment confirmed. Pro active till {expires}. Everything unlocked. Try /script or /trends now.")
    return jsonify({"status": "ok"}), 200

# ========================= ADMIN =========================
def admin_allowed():
    if session.get("admin_authed"):
        return True
    key = request.args.get("key") or request.headers.get("X-Admin-Key")
    return bool(ADMIN_EXPORT_KEY and key and hmac.compare_digest(str(key), str(ADMIN_EXPORT_KEY)))

def admin_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not admin_allowed():
            return redirect("/admin/login?next=" + request.path)
        return fn(*args, **kwargs)
    return wrapper

def money_ngn(kobo):
    return f"₦{(int(kobo or 0) / 100):,.0f}"

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if hmac.compare_digest(email, (ADMIN_EMAIL or "").strip().lower()) and hmac.compare_digest(password, str(ADMIN_PASSWORD or "")):
            session["admin_authed"] = True
            return redirect("/admin")
        error = "Wrong admin email or password."
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>Admin Login</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:#060a12;color:#f8fbff;font-family:system-ui,sans-serif;display:grid;place-items:center;padding:18px}}.b{{width:min(400px,100%);background:#0d1525;border:1px solid #243550;border-radius:24px;padding:24px}}h2{{margin:0 0 18px}}label{{display:block;font-size:13px;color:#93a4bd;margin:12px 0 6px}}input{{width:100%;padding:14px;border-radius:12px;border:1px solid #263852;background:#070d16;color:#fff;font-size:15px}}button{{width:100%;margin-top:16px;padding:14px;border-radius:12px;border:0;background:linear-gradient(135deg,#14b8a6,#38bdf8);font-weight:800;color:#061018;font-size:15px}}.e{{color:#fecdd3;margin-top:12px;font-size:13px}}</style></head>
    <body><form class='b' method='post'><h2>TikGenius Admin</h2><label>Email</label><input name='email' type='email'><label>Password</label><input name='password' type='password'><button>Unlock</button><div class='e'>{escape(error)}</div></form></body></html>"""

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authed", None)
    return redirect("/admin/login")

@app.route("/admin")
@admin_login_required
def admin_panel():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM web_users")
            total_users = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS pro FROM web_users WHERE plan='pro' AND expires >= CURRENT_DATE")
            premium_users = cur.fetchone()["pro"]
            cur.execute("SELECT COUNT(*) AS today FROM web_users WHERE created_at::date = CURRENT_DATE")
            today_signups = cur.fetchone()["today"]
            cur.execute("SELECT COALESCE(SUM(amount_kobo),0) AS revenue, COUNT(*) AS count FROM web_payments WHERE status='success'")
            pay_stats = cur.fetchone()
            cur.execute("""SELECT p.reference, p.amount_kobo, p.source, p.paid_at, COALESCE(w.email, p.raw_email, '') AS email
                           FROM web_payments p LEFT JOIN web_users w ON w.id=p.user_id
                           ORDER BY p.paid_at DESC LIMIT 20""")
            payments = cur.fetchall()
            cur.execute("""SELECT id, COALESCE(name,'') AS name, email, plan, expires, region, usage_count, created_at
                           FROM web_users ORDER BY created_at DESC LIMIT 300""")
            users = cur.fetchall()
    finally:
        release_db(conn)

    conversion = round((premium_users / total_users * 100), 1) if total_users else 0
    pay_rows = "".join(f"<tr><td>{escape(str(p['paid_at'] or ''))[:16]}</td><td>{escape(p['email'] or '')}</td><td>{money_ngn(p['amount_kobo'])}</td><td>{escape(p['source'] or '')}</td></tr>" for p in payments) or "<tr><td colspan='4'>No payments yet</td></tr>"
    user_rows = "".join(f"<tr><td>{u['id']}</td><td>{escape(u['email'])}</td><td><span style='color:{'#6ee7b7' if u['plan']=='pro' else '#c4b5fd'}'>{u['plan']}</span></td><td>{escape(str(u['expires'] or ''))}</td><td>{escape(u['region'] or '')}</td><td>{u['usage_count'] or 0}</td><td>{escape(str(u['created_at'] or ''))[:10]}</td></tr>" for u in users) or "<tr><td colspan='7'>No users yet</td></tr>"

    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>TikGenius Admin</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;background:#060a12;color:#f0f7ff;font-family:system-ui,sans-serif;padding:16px}}.wrap{{max-width:1200px;margin:auto}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0}}.stat{{background:#0d1525;border:1px solid #243550;border-radius:18px;padding:16px}}.stat b{{display:block;font-size:1.7rem;font-weight:900}}.stat span{{font-size:11px;color:#93a4bd;text-transform:uppercase}}.card{{background:#0d1525;border:1px solid #243550;border-radius:18px;padding:16px;margin-bottom:14px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px;border-bottom:1px solid #1e293b;text-align:left;font-size:13px;white-space:nowrap}}th{{color:#bfdbfe;font-size:11px;text-transform:uppercase}}a{{color:#38bdf8;text-decoration:none;padding:6px 10px;border:1px solid #243550;border-radius:8px;font-size:12px}}h2{{margin:0 0 12px;font-size:16px}}input{{width:100%;padding:10px;border-radius:10px;border:1px solid #243550;background:#070d16;color:#fff;margin-bottom:12px;font-size:14px}}.head{{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}}
    </style></head><body><div class='wrap'>
    <div class='head'><h1 style='margin:0'>TikGenius Admin</h1><a href='/admin/logout'>Log out</a></div>
    <div class='stats'>
      <div class='stat'><span>Revenue</span><b>{money_ngn(pay_stats['revenue'])}</b></div>
      <div class='stat'><span>Premium Users</span><b>{premium_users}</b></div>
      <div class='stat'><span>Free Users</span><b>{total_users - premium_users}</b></div>
      <div class='stat'><span>Total Signups</span><b>{total_users}</b></div>
      <div class='stat'><span>Today Signups</span><b>{today_signups}</b></div>
      <div class='stat'><span>Conversion</span><b>{conversion}%</b></div>
    </div>
    <div class='card'><h2>Recent Payments</h2><div style='overflow:auto'><table><thead><tr><th>Date</th><th>Email</th><th>Amount</th><th>Source</th></tr></thead><tbody>{pay_rows}</tbody></table></div></div>
    <div class='card'><h2>Users</h2><div style='margin-bottom:8px'><a href='/admin/emails.csv'>⬇ Download CSV</a></div><input id='s' placeholder='Search...' onkeyup='filterRows()'><div style='overflow:auto'><table id='ut'><thead><tr><th>ID</th><th>Email</th><th>Plan</th><th>Expires</th><th>Region</th><th>Uses</th><th>Signup</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
    </div><script>function filterRows(){{let q=document.getElementById('s').value.toLowerCase();document.querySelectorAll('#ut tbody tr').forEach(r=>r.style.display=r.innerText.toLowerCase().includes(q)?'':'none')}}</script></body></html>"""

@app.route("/admin/emails.csv")
@admin_login_required
def admin_emails_csv():
    import csv, io
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, COALESCE(name,'') AS name, email, plan, expires, region, usage_count, created_at FROM web_users ORDER BY created_at DESC")
            users = cur.fetchall()
    finally:
        release_db(conn)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "email", "plan", "expires", "region", "usage_count", "created_at"])
    for u in users:
        writer.writerow([u["id"], u["name"], u["email"], u["plan"], u["expires"] or "", u["region"], u["usage_count"] or 0, u["created_at"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tikgenius_emails.csv"})

# ========================= TELEGRAM BOT =========================
LOADING = {
    "hooks": ["🧠 Writing hooks that stop the scroll...", "🔥 Finding your angle..."],
    "captions": ["💅 Cooking captions people will screenshot...", "😭 Making the twist land..."],
    "pov": ["🎥 Building POVs they will tag friends in...", "🍿 Setting up the scene..."],
    "hashtags": ["📊 Building your hashtag strategy...", "🚀 Mixing reach and niche tags..."],
    "bio": ["✨ Writing bios that get the follow...", "📱 Building your profile hook..."],
    "script": ["🎬 Writing hook, body, punchline...", "📝 Building a 60-second script..."],
    "trends": ["📈 Analysing what is working now...", "🔥 Building trend ideas..."],
    "threads": ["🧵 Building the viral thread...", "✍️ Writing something quotable..."]
}

EXAMPLES = {
    "hooks": "/hooks I prayed for this life and I am still not happy",
    "captions": "/captions I work so hard but I am still broke",
    "hashtags": "/hashtags lifestyle and soft life content creator",
    "pov": "/pov you finally made it and nobody who doubted you said sorry",
    "bio": "/bio lifestyle and soft life content creator",
    "script": "/script things nobody tells you before working for yourself",
    "trends": "/trends money mindset and hustle content",
    "threads": "/threads x why resting feels like a crime"
}

TIKTOK_COMMANDS = {"/hooks", "/captions", "/pov", "/hashtags", "/bio", "/script", "/trends"}
X_COMMANDS = {"/xtweets", "/xhooks", "/xthread"}
PRO_COMMANDS = {"/script", "/trends", "/xthread"}

REGION_KEYBOARD = {"inline_keyboard": [
    [{"text": "🇳🇬 Nigerian", "callback_data": "region_nigeria"}, {"text": "🇺🇸 American", "callback_data": "region_usa"}],
    [{"text": "🇬🇧 British", "callback_data": "region_uk"}, {"text": "🇯🇲 Caribbean", "callback_data": "region_caribbean"}],
    [{"text": "🇰🇪 East African", "callback_data": "region_eastafrica"}, {"text": "🇿🇦 South African", "callback_data": "region_southafrica"}],
    [{"text": "🌍 Global / General", "callback_data": "region_global"}]
]}

def send_telegram_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_typing(chat_id):
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5)
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
            cur.execute("""INSERT INTO users (user_id, region) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET region=EXCLUDED.region""", (user_id, region))
        conn.commit()
    finally:
        release_db(conn)

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}

    if "callback_query" in data:
        cb = data["callback_query"]
        user_id = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        cb_data = cb.get("data", "")
        if cb_data.startswith("region_"):
            region = cb_data.replace("region_", "")
            set_user_region(user_id, region)
            region_name = REGION_NAMES.get(region, "Global")
            send_telegram_message(chat_id, f"✅ Region set to {region_name}\n\nNow try:\n{EXAMPLES.get('captions')}")
        try:
            http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=5)
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
        send_telegram_message(chat_id, f"✨ Welcome {first_name} — you found TikGenius 🔥\n\nI write viral TikTok and Twitter/X content for creators worldwide.\n\nFirst — pick your content style:")
        send_telegram_message(chat_id, "Choose your region:", reply_markup=REGION_KEYBOARD)

    elif command == "/commands":
        send_telegram_message(chat_id,
            f"━━━ TIKTOK ━━━\n/hooks [topic]\n/captions [topic]\n/pov [topic]\n/hashtags [topic]\n/bio [niche]\n/script [idea] ⭐ Pro\n\n"
            f"━━━ TWITTER / X ━━━\n/xtweets [topic]\n/xhooks [topic]\n/xthread [topic] ⭐ Pro\n\n"
            f"━━━ OTHER ━━━\n/trends [niche] ⭐ Pro\n/region — change region\n/plan — check plan\n/upgrade — go Pro\n\n"
            f"Free: {FREE_LIMIT} uses/day — {FREE_RESULTS} results each\n"
            f"Pro: ₦2,000/month — {PRO_RESULTS} results, unlimited uses\n\n"
            f"Be specific:\n❌ /captions tired\n✅ /captions I work so hard but I am still broke")

    elif command == "/region":
        send_telegram_message(chat_id, "Choose your content region:", reply_markup=REGION_KEYBOARD)

    elif command == "/plan":
        region = get_user_region(user_id)
        region_name = REGION_NAMES.get(region, "Global")
        if is_pro(user_id):
            send_telegram_message(chat_id, f"✅ Pro — expires {get_pro_expiry(user_id)}\nRegion: {region_name}\n{PRO_RESULTS} results per generation, unlimited uses.")
        else:
            remaining = free_uses_remaining(user_id)
            send_telegram_message(chat_id, f"🆓 Free Plan — {remaining}/{FREE_LIMIT} uses left today\nRegion: {region_name}\n{FREE_RESULTS} results per generation\n\nUpgrade → /upgrade")

    elif command == "/upgrade":
        link = tg_create_payment_link(user_id, username)
        send_telegram_message(chat_id,
            f"🚀 TikGenius Pro — ₦2,000/month\n\n✅ {PRO_RESULTS} results per generation\n✅ Unlimited uses daily\n✅ Full video scripts (/script)\n✅ Trend ideas (/trends)\n✅ Full X threads (/xthread)\n✅ All 7 regions\n\nPay here:\n{link or 'Try again'}\n\nActivation is automatic ✅")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_TG_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_telegram_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_telegram_message(chat_id, "❌ Not allowed.")

    elif command == "/stats":
        if str(user_id) != ADMIN_TG_ID:
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
                cur.execute("SELECT COALESCE(SUM(amount_kobo),0) AS revenue FROM web_payments WHERE status='success'")
                revenue = cur.fetchone()["revenue"]
            send_telegram_message(chat_id, f"📊 TikGenius Stats\n\nRevenue: {money_ngn(revenue)}\n\nTELEGRAM\n👥 Users: {tg_total}\n💎 Pro: {tg_pro}\n\nWEBSITE\n👥 Users: {web_total}\n💎 Pro: {web_pro}")
        finally:
            release_db(conn)

    elif command in TIKTOK_COMMANDS:
        mode = command.replace("/", "")
        if command in PRO_COMMANDS and not is_pro(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id, f"🔒 Pro feature.\n\nUpgrade for ₦2,000/month:\n{link or '/upgrade'}")
            return jsonify({"ok": True})
        if not topic:
            send_telegram_message(chat_id, f"Add a topic.\n\nExample:\n{EXAMPLES.get(mode)}")
            return jsonify({"ok": True})
        if len(topic.split()) < 3:
            send_telegram_message(chat_id, f"Be more specific.\n\nTry: {EXAMPLES.get(mode)}")
            return jsonify({"ok": True})
        if not check_and_increment_free_usage(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id, f"⏳ {FREE_LIMIT} free uses used today.\n\nUpgrade to Pro:\n{link or '/upgrade'}")
            return jsonify({"ok": True})
        send_typing(chat_id)
        send_telegram_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it..."])))
        region = get_user_region(user_id)
        pro = is_pro(user_id)
        raw = ask_ai(mode, topic, "tiktok", region, pro)
        if raw:
            results = parse_results(raw, "tiktok", pro)
            times = get_posting_times("tiktok", len(results)) if mode not in ("script",) else []
            output_parts = [f"✨ TikGenius — {mode.capitalize()}\n"]
            if mode == "script":
                output_parts.append(raw[:3000])
            else:
                for i, (result, time_info) in enumerate(zip(results, times)):
                    output_parts.append(f"{result}\n📅 Best time: {time_info[0]} {time_info[1]}\n💡 {time_info[2]}\n")
            send_telegram_message(chat_id, "\n".join(output_parts)[:3800])
        else:
            send_telegram_message(chat_id, "⚠️ Something went wrong. Please try again.")
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_telegram_message(chat_id, f"💡 {remaining} free use(s) left today. Go Pro → /upgrade")

    elif command in X_COMMANDS:
        mode_map = {"/xtweets": "captions", "/xhooks": "hooks", "/xthread": "threads"}
        mode = mode_map[command]
        if command == "/xthread" and not is_pro(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id, f"🔒 X Threads is Pro.\n\nUpgrade:\n{link or '/upgrade'}")
            return jsonify({"ok": True})
        if not topic:
            send_telegram_message(chat_id, f"Add a topic.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})
        if len(topic.split()) < 3:
            send_telegram_message(chat_id, f"Be more specific.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})
        if not check_and_increment_free_usage(user_id):
            link = tg_create_payment_link(user_id, username)
            send_telegram_message(chat_id, f"⏳ Free uses finished.\n\nUpgrade:\n{link or '/upgrade'}")
            return jsonify({"ok": True})
        send_typing(chat_id)
        send_telegram_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it..."])))
        region = get_user_region(user_id)
        pro = is_pro(user_id)
        raw = ask_ai(mode, topic, "x", region, pro)
        if raw:
            results = parse_results(raw, "x", pro)
            times = get_posting_times("x", len(results)) if mode != "threads" else []
            output_parts = [f"✨ XGenius — {mode.capitalize()}\n"]
            if mode == "threads":
                output_parts.append(raw[:3000])
            else:
                for i, (result, time_info) in enumerate(zip(results, times)):
                    output_parts.append(f"{result}\n📅 Best time: {time_info[0]} {time_info[1]}\n💡 {time_info[2]}\n")
            send_telegram_message(chat_id, "\n".join(output_parts)[:3800])
        else:
            send_telegram_message(chat_id, "⚠️ Something went wrong. Please try again.")
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_telegram_message(chat_id, f"💡 {remaining} free use(s) left today. Go Pro → /upgrade")

    else:
        send_telegram_message(chat_id, "Unknown command. Use /commands to see everything.")

    return jsonify({"ok": True})

# ========================= HTML =========================
HOME_HTML = open("/mnt/user-data/outputs/home.html").read() if os.path.exists("/mnt/user-data/outputs/home.html") else "<h1>TikGenius</h1>"

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>TikGenius</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
:root{--bg:#070b10;--card:#0d1525;--border:#1e2d3d;--text:#eef6ff;--muted:#7a8fa6;--brand:#14b8a6;--brand2:#38bdf8;--gold:#f6b21a;--danger:#fb7185;--green:#22c55e}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif;min-height:100vh;min-height:-webkit-fill-available}

/* TOP NAV */
.topnav{position:sticky;top:0;z-index:50;background:rgba(7,11,16,0.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:0 16px;height:56px;display:flex;align-items:center;justify-content:space-between}
.nav-logo{font-weight:800;font-size:18px;letter-spacing:-.03em}.nav-logo span{color:var(--brand2)}
.nav-right{display:flex;align-items:center;gap:8px}
.nav-plan{font-size:12px;font-weight:700;padding:4px 10px;border-radius:100px;background:rgba(20,184,166,0.15);color:var(--brand);border:1px solid rgba(20,184,166,0.3)}
.nav-plan.pro{background:rgba(246,178,26,0.15);color:var(--gold);border-color:rgba(246,178,26,0.3)}
.menu-btn{background:var(--card);border:1px solid var(--border);color:var(--text);width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:18px;cursor:pointer}

/* DRAWER */
.drawer-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:100;backdrop-filter:blur(4px)}
.drawer-overlay.open{display:block}
.drawer{position:fixed;left:0;top:0;bottom:0;width:min(300px,85vw);background:#0b1420;border-right:1px solid var(--border);z-index:101;transform:translateX(-100%);transition:transform 0.3s ease;overflow-y:auto;padding:20px 16px}
.drawer.open{transform:translateX(0)}
.drawer-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.drawer-logo{font-weight:800;font-size:20px}.drawer-logo span{color:var(--brand2)}
.close-btn{background:var(--card);border:1px solid var(--border);color:var(--muted);width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:16px}
.drawer-user{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:12px;margin-bottom:16px}
.drawer-email{font-size:13px;color:var(--muted);margin-bottom:6px;word-break:break-all}
.drawer-uses{font-size:13px;font-weight:600}
.uses-bar{height:6px;background:var(--border);border-radius:100px;margin-top:6px;overflow:hidden}
.uses-fill{height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));transition:width 0.3s}

.drawer-section{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;font-weight:700;margin:16px 0 8px}
.drawer-item{display:flex;align-items:center;gap:10px;padding:11px 12px;border-radius:12px;color:var(--muted);font-size:14px;font-weight:500;cursor:pointer;border:1px solid transparent;margin-bottom:4px;background:none;width:100%;text-align:left}
.drawer-item:hover,.drawer-item.active{background:rgba(20,184,166,0.08);color:var(--text);border-color:var(--border)}
.drawer-item.active{color:var(--brand2)}
.drawer-item .icon{font-size:16px;width:20px;text-align:center}
.pro-badge{font-size:9px;background:linear-gradient(135deg,var(--brand),var(--gold));color:#061018;padding:2px 5px;border-radius:4px;font-weight:800;margin-left:auto}

.upgrade-banner{background:linear-gradient(135deg,rgba(20,184,166,0.15),rgba(246,178,26,0.1));border:1px solid rgba(246,178,26,0.25);border-radius:14px;padding:14px;margin-top:8px}
.upgrade-banner h4{margin:0 0 4px;font-size:14px}
.upgrade-banner p{margin:0 0 10px;font-size:12px;color:var(--muted)}
.upgrade-btn{width:100%;background:linear-gradient(135deg,var(--brand),var(--gold));border:none;color:#061018;border-radius:10px;padding:10px;font-weight:800;font-size:14px;cursor:pointer}

.drawer-logout{width:100%;background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:10px;padding:10px;font-size:13px;cursor:pointer;margin-top:12px}

/* MAIN */
.main{padding:16px;max-width:680px;margin:0 auto}

/* MODE PILLS */
.mode-scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;margin-bottom:16px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.mode-scroll::-webkit-scrollbar{display:none}
.mode-pill{flex-shrink:0;padding:8px 14px;border-radius:100px;border:1px solid var(--border);background:transparent;color:var(--muted);font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all 0.2s}
.mode-pill.active{background:rgba(56,189,248,0.12);border-color:var(--brand2);color:var(--brand2)}
.pro-pill::after{content:'⭐';margin-left:4px;font-size:10px}

/* PLATFORM TOGGLE */
.platform-toggle{display:flex;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:3px;margin-bottom:16px;width:fit-content}
.platform-btn{padding:7px 16px;border-radius:9px;border:none;background:transparent;color:var(--muted);font-size:13px;font-weight:600;cursor:pointer;transition:all 0.2s}
.platform-btn.active{background:var(--brand2);color:#061018}

/* REGION */
.region-row{display:flex;align-items:center;gap:8px;margin-bottom:16px}
.region-label{font-size:12px;color:var(--muted);font-weight:600}
.region-select{background:var(--card);border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:10px;font-size:13px;font-family:Inter,sans-serif;outline:none;flex:1}

/* INPUT */
.input-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:14px;margin-bottom:12px}
.input-label{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:8px}
.topic-input{width:100%;background:rgba(255,255,255,0.04);border:1px solid var(--border);color:var(--text);padding:12px;border-radius:12px;font-family:Inter,sans-serif;font-size:15px;line-height:1.5;resize:none;outline:none;min-height:90px}
.topic-input:focus{border-color:var(--brand2)}
.input-tip{font-size:11px;color:var(--muted);margin-top:6px}
.generate-btn{width:100%;background:linear-gradient(135deg,var(--brand),var(--gold));border:none;color:#061018;border-radius:12px;padding:14px;font-size:16px;font-weight:800;cursor:pointer;margin-top:10px;transition:opacity 0.2s}
.generate-btn:disabled{opacity:0.5;cursor:not-allowed}
.error-msg{display:none;background:rgba(251,113,133,0.1);border:1px solid rgba(251,113,133,0.3);color:var(--danger);border-radius:10px;padding:10px 12px;font-size:13px;margin-top:8px}

/* RESULTS */
.results-section{display:none}
.results-section.show{display:block}
.results-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.results-title{font-size:14px;font-weight:700;color:var(--muted)}
.copy-all-btn{background:var(--card);border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer}

.result-card{background:var(--card);border:1px solid var(--border);border-radius:16px;margin-bottom:10px;overflow:hidden}
.result-content{padding:14px;font-size:15px;line-height:1.65;color:var(--text);white-space:pre-wrap}
.result-footer{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-top:1px solid var(--border);background:rgba(0,0,0,0.2)}
.post-time{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)}
.post-time strong{color:var(--brand2);font-size:12px}
.copy-btn{background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.2);color:var(--brand2);padding:6px 12px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;transition:all 0.2s;white-space:nowrap}
.copy-btn:active{background:rgba(56,189,248,0.2)}
.copy-btn.copied{background:rgba(34,197,94,0.15);border-color:rgba(34,197,94,0.3);color:var(--green)}

/* UPGRADE PROMPT */
.upgrade-prompt{background:linear-gradient(135deg,rgba(20,184,166,0.12),rgba(246,178,26,0.08));border:1px solid rgba(246,178,26,0.25);border-radius:16px;padding:16px;margin-top:10px;text-align:center}
.upgrade-prompt h3{margin:0 0 6px;font-size:16px}
.upgrade-prompt p{margin:0 0 12px;font-size:13px;color:var(--muted)}
.upgrade-prompt-btn{background:linear-gradient(135deg,var(--brand),var(--gold));border:none;color:#061018;border-radius:10px;padding:12px 24px;font-weight:800;font-size:15px;cursor:pointer}

/* FREE LIMIT */
.free-limit-card{background:rgba(251,113,133,0.08);border:1px solid rgba(251,113,133,0.25);border-radius:16px;padding:16px;text-align:center;display:none}
.free-limit-card.show{display:block}
.free-limit-card h3{margin:0 0 6px;font-size:16px;color:var(--danger)}
.free-limit-card p{margin:0 0 12px;font-size:13px;color:var(--muted)}

/* HISTORY */
.history-section{margin-top:20px}
.history-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.history-title{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:1px}
.clear-btn{background:transparent;border:1px solid var(--border);color:var(--muted);padding:5px 10px;border-radius:8px;font-size:11px;cursor:pointer}
.history-scroll{display:flex;gap:8px;overflow-x:auto;padding-bottom:4px;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.history-scroll::-webkit-scrollbar{display:none}
.history-item{flex-shrink:0;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;cursor:pointer;min-width:160px;max-width:200px}
.history-item b{display:block;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.history-item span{font-size:11px;color:var(--muted)}
.history-empty{font-size:13px;color:var(--muted);padding:10px;background:var(--card);border:1px dashed var(--border);border-radius:12px;text-align:center}
</style>
</head>
<body>

<!-- TOP NAV -->
<div class="topnav">
  <div class="nav-logo">Tik<span>Genius</span></div>
  <div class="nav-right">
    <div class="nav-plan" id="navPlan">Free</div>
    <div class="menu-btn" onclick="openDrawer()">☰</div>
  </div>
</div>

<!-- DRAWER -->
<div class="drawer-overlay" id="drawerOverlay" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <div class="drawer-header">
    <div class="drawer-logo">Tik<span>Genius</span></div>
    <div class="close-btn" onclick="closeDrawer()">✕</div>
  </div>
  <div class="drawer-user">
    <div class="drawer-email" id="drawerEmail">Loading...</div>
    <div class="drawer-uses" id="drawerUses">5/5 free uses left today</div>
    <div class="uses-bar"><div class="uses-fill" id="drawerFill" style="width:100%"></div></div>
  </div>

  <div class="drawer-section">TikTok Tools</div>
  <button class="drawer-item active" id="di-captions" onclick="setModeFromDrawer('captions','tiktok')"><span class="icon">✍️</span> Captions</button>
  <button class="drawer-item" id="di-hooks" onclick="setModeFromDrawer('hooks','tiktok')"><span class="icon">🎣</span> Hooks</button>
  <button class="drawer-item" id="di-pov" onclick="setModeFromDrawer('pov','tiktok')"><span class="icon">🎥</span> POV Ideas</button>
  <button class="drawer-item" id="di-hashtags" onclick="setModeFromDrawer('hashtags','tiktok')"><span class="icon">📊</span> Hashtags</button>
  <button class="drawer-item" id="di-bio" onclick="setModeFromDrawer('bio','tiktok')"><span class="icon">👤</span> Bio</button>
  <button class="drawer-item" id="di-script" onclick="setModeFromDrawer('script','tiktok')"><span class="icon">📝</span> Script <span class="pro-badge">PRO</span></button>
  <button class="drawer-item" id="di-trends" onclick="setModeFromDrawer('trends','tiktok')"><span class="icon">📈</span> Trends <span class="pro-badge">PRO</span></button>

  <div class="drawer-section">Twitter / X Tools</div>
  <button class="drawer-item" id="di-xcaptions" onclick="setModeFromDrawer('captions','x')"><span class="icon">𝕏</span> X Tweets</button>
  <button class="drawer-item" id="di-xhooks" onclick="setModeFromDrawer('hooks','x')"><span class="icon">🧲</span> X Hooks</button>
  <button class="drawer-item" id="di-xthreads" onclick="setModeFromDrawer('threads','x')"><span class="icon">🧵</span> X Thread <span class="pro-badge">PRO</span></button>

  <div id="upgradeDrawer" class="upgrade-banner" style="display:none">
    <h4>Upgrade to Pro</h4>
    <p>Get 7 results per generation, unlimited uses, scripts, threads and trends.</p>
    <button class="upgrade-btn" onclick="doUpgrade()">Upgrade — ₦2,000/mo</button>
  </div>

  <button class="drawer-logout" onclick="doLogout()">Log out</button>
</div>

<!-- MAIN CONTENT -->
<div class="main">

  <!-- MODE PILLS -->
  <div class="mode-scroll" id="modePills">
    <div class="mode-pill active" onclick="setMode('captions','tiktok',this)">Captions</div>
    <div class="mode-pill" onclick="setMode('hooks','tiktok',this)">Hooks</div>
    <div class="mode-pill" onclick="setMode('pov','tiktok',this)">POV</div>
    <div class="mode-pill" onclick="setMode('hashtags','tiktok',this)">Hashtags</div>
    <div class="mode-pill" onclick="setMode('bio','tiktok',this)">Bio</div>
    <div class="mode-pill pro-pill" onclick="setMode('script','tiktok',this)">Script</div>
    <div class="mode-pill pro-pill" onclick="setMode('trends','tiktok',this)">Trends</div>
    <div class="mode-pill" onclick="setMode('captions','x',this)">X Tweets</div>
    <div class="mode-pill" onclick="setMode('hooks','x',this)">X Hooks</div>
    <div class="mode-pill pro-pill" onclick="setMode('threads','x',this)">X Thread</div>
  </div>

  <!-- PLATFORM + REGION -->
  <div class="region-row">
    <div class="platform-toggle">
      <button class="platform-btn active" id="tiktokBtn" onclick="switchPlatform('tiktok')">TikTok</button>
      <button class="platform-btn" id="xBtn" onclick="switchPlatform('x')">Twitter / X</button>
    </div>
    <select class="region-select" id="regionSelect" onchange="changeRegion(this.value)">
      <option value="global">🌍 Global</option>
      <option value="nigeria">🇳🇬 Nigerian</option>
      <option value="usa">🇺🇸 American</option>
      <option value="uk">🇬🇧 British</option>
      <option value="caribbean">🇯🇲 Caribbean</option>
      <option value="eastafrica">🇰🇪 East African</option>
      <option value="southafrica">🇿🇦 South African</option>
    </select>
  </div>

  <!-- INPUT -->
  <div class="input-card">
    <div class="input-label" id="inputLabel">What is your TikTok video about?</div>
    <textarea class="topic-input" id="topicInput" placeholder="Be specific for better results.

Example: I work so hard but I am still broke
Example: I finally left the toxic relationship and I feel guilty
Example: Things nobody tells you before you start a business"></textarea>
    <div class="input-tip">💡 At least 5 words — the more detail, the better the output</div>
    <div class="error-msg" id="errorMsg"></div>
    <button class="generate-btn" id="generateBtn" onclick="generate()">✨ Generate</button>
  </div>

  <!-- FREE LIMIT -->
  <div class="free-limit-card" id="freeLimitCard">
    <h3>You've used all 5 free generations today</h3>
    <p>Upgrade to Pro for unlimited generations, 7 results each, plus scripts, threads and trends.</p>
    <button class="upgrade-prompt-btn" onclick="doUpgrade()">Upgrade — ₦2,000/month</button>
  </div>

  <!-- RESULTS -->
  <div class="results-section" id="resultsSection">
    <div class="results-header">
      <div class="results-title" id="resultsTitle">Results</div>
      <button class="copy-all-btn" onclick="copyAll()">Copy All</button>
    </div>
    <div id="resultsList"></div>

    <!-- UPGRADE PROMPT FOR FREE USERS -->
    <div id="upgradePrompt" style="display:none" class="upgrade-prompt">
      <h3>Want 7 results instead of 4?</h3>
      <p>Pro gives you 7 results per generation, unlimited uses, scripts, X threads and trend ideas.</p>
      <button class="upgrade-prompt-btn" onclick="doUpgrade()">Upgrade to Pro — ₦2,000/month</button>
    </div>
  </div>

  <!-- HISTORY -->
  <div class="history-section">
    <div class="history-head">
      <div class="history-title">Recent</div>
      <button class="clear-btn" onclick="clearHistory()">Clear</button>
    </div>
    <div class="history-scroll" id="historyList">
      <div class="history-empty">Your recent generations will appear here</div>
    </div>
  </div>

</div>

<script>
let currentMode = 'captions';
let currentPlatform = 'tiktok';
let userData = {};
let allResults = [];

const modeLabels = {
  captions: 'What is your video about?',
  hooks: 'What is your video about?',
  pov: 'What is your video about?',
  hashtags: 'What topic or niche are you posting about?',
  bio: 'What is your content niche?',
  script: 'What is your video idea?',
  trends: 'What is your content niche?',
  threads: 'What is your thread about?'
};

async function loadUser() {
  const res = await fetch('/api/me');
  if (res.status === 401) { location.href = '/'; return; }
  userData = await res.json();

  document.getElementById('drawerEmail').textContent = userData.email;
  document.getElementById('regionSelect').value = userData.region || 'global';

  const isPro = userData.plan === 'pro';
  document.getElementById('navPlan').textContent = isPro ? '⭐ Pro' : 'Free';
  document.getElementById('navPlan').className = 'nav-plan' + (isPro ? ' pro' : '');

  if (isPro) {
    document.getElementById('drawerUses').textContent = 'Pro — unlimited generations';
    document.getElementById('drawerFill').style.width = '100%';
    document.getElementById('upgradeDrawer').style.display = 'none';
  } else {
    const rem = userData.uses_remaining || 0;
    document.getElementById('drawerUses').textContent = rem + '/5 free uses left today';
    document.getElementById('drawerFill').style.width = (rem / 5 * 100) + '%';
    document.getElementById('upgradeDrawer').style.display = rem <= 2 ? 'block' : 'none';
  }

  await loadHistory();
}

function setMode(mode, platform, el) {
  currentMode = mode;
  currentPlatform = platform;

  document.querySelectorAll('.mode-pill').forEach(p => p.classList.remove('active'));
  if (el) el.classList.add('active');

  document.getElementById('tiktokBtn').classList.toggle('active', platform === 'tiktok');
  document.getElementById('xBtn').classList.toggle('active', platform === 'x');
  document.getElementById('inputLabel').textContent = modeLabels[mode] || 'What is your topic?';
  document.getElementById('resultsSection').classList.remove('show');
  hideError();
}

function setModeFromDrawer(mode, platform) {
  currentMode = mode;
  currentPlatform = platform;

  document.querySelectorAll('.drawer-item').forEach(i => i.classList.remove('active'));
  const key = platform === 'x' ? `di-x${mode === 'captions' ? 'captions' : mode === 'hooks' ? 'hooks' : 'threads'}` : `di-${mode}`;
  const el = document.getElementById(key);
  if (el) el.classList.add('active');

  document.getElementById('tiktokBtn').classList.toggle('active', platform === 'tiktok');
  document.getElementById('xBtn').classList.toggle('active', platform === 'x');
  document.getElementById('inputLabel').textContent = modeLabels[mode] || 'What is your topic?';
  document.getElementById('resultsSection').classList.remove('show');
  closeDrawer();
  hideError();
}

function switchPlatform(p) {
  currentPlatform = p;
  document.getElementById('tiktokBtn').classList.toggle('active', p === 'tiktok');
  document.getElementById('xBtn').classList.toggle('active', p === 'x');
}

function openDrawer() {
  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawerOverlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawerOverlay').classList.remove('open');
  document.body.style.overflow = '';
}

async function changeRegion(region) {
  await fetch('/api/set-region', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({region})
  });
}

function hideError() {
  const e = document.getElementById('errorMsg');
  e.style.display = 'none';
}

function showError(msg) {
  const e = document.getElementById('errorMsg');
  e.textContent = msg;
  e.style.display = 'block';
}

async function generate() {
  const topic = document.getElementById('topicInput').value.trim();
  const btn = document.getElementById('generateBtn');
  hideError();
  document.getElementById('freeLimitCard').classList.remove('show');

  if (!topic) { showError('Please enter a topic'); return; }
  if (topic.split(/\s+/).length < 3) { showError('Add more detail — at least 3 words'); return; }

  btn.disabled = true;
  btn.textContent = '⏳ Generating...';
  document.getElementById('resultsSection').classList.remove('show');

  const res = await fetch('/api/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode: currentMode, topic, platform: currentPlatform})
  });

  btn.disabled = false;
  btn.textContent = '✨ Generate';

  if (res.status === 429) {
    const data = await res.json();
    if (data.error === 'free_limit') {
      document.getElementById('freeLimitCard').classList.add('show');
    } else {
      showError(data.error || 'Too many requests');
    }
    return;
  }

  const data = await res.json();
  if (data.error) { showError(data.error); return; }

  allResults = data.results || [];
  renderResults(data);
  await loadHistory();

  if (!data.unlimited && data.uses_remaining !== undefined) {
    const rem = data.uses_remaining;
    document.getElementById('drawerUses').textContent = rem + '/5 free uses left today';
    document.getElementById('drawerFill').style.width = (rem / 5 * 100) + '%';
    if (rem <= 0) {
      document.getElementById('freeLimitCard').classList.add('show');
    }
    if (rem <= 2) {
      document.getElementById('upgradeDrawer').style.display = 'block';
    }
  }
}

function renderResults(data) {
  const list = document.getElementById('resultsList');
  const isScript = currentMode === 'script' || currentMode === 'threads';
  list.innerHTML = '';

  allResults.forEach((item, i) => {
    const card = document.createElement('div');
    card.className = 'result-card';

    const content = document.createElement('div');
    content.className = 'result-content';
    content.textContent = item.text;
    card.appendChild(content);

    const footer = document.createElement('div');
    footer.className = 'result-footer';

    if (item.post_day && !isScript) {
      const timeEl = document.createElement('div');
      timeEl.className = 'post-time';
      timeEl.innerHTML = `📅 <strong>${item.post_day} ${item.post_time}</strong> — ${item.post_reason}`;
      footer.appendChild(timeEl);
    } else {
      footer.appendChild(document.createElement('div'));
    }

    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.textContent = 'Copy';
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(item.text).then(() => {
        copyBtn.textContent = 'Copied!';
        copyBtn.classList.add('copied');
        setTimeout(() => { copyBtn.textContent = 'Copy'; copyBtn.classList.remove('copied'); }, 2000);
      });
    };
    footer.appendChild(copyBtn);
    card.appendChild(footer);
    list.appendChild(card);
  });

  const count = allResults.length;
  const isPro = data.unlimited;
  document.getElementById('resultsTitle').textContent = `${count} result${count !== 1 ? 's' : ''} generated`;
  document.getElementById('upgradePrompt').style.display = (!isPro && count > 0) ? 'block' : 'none';
  document.getElementById('resultsSection').classList.add('show');
  document.getElementById('resultsSection').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

function copyAll() {
  const text = allResults.map(r => r.text).join('\n\n---\n\n');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.querySelector('.copy-all-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy All', 2000);
  });
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const data = await res.json();
  const list = document.getElementById('historyList');
  if (!data.items || data.items.length === 0) {
    list.innerHTML = '<div class="history-empty">Your recent generations will appear here</div>';
    return;
  }
  list.innerHTML = data.items.map(item => {
    const platform = item.platform === 'x' ? 'X' : 'TikTok';
    const date = new Date(item.created_at).toLocaleDateString();
    return `<div class="history-item" onclick="showHistoryItem(${JSON.stringify(item.topic).replace(/'/g,'&#39;')}, ${JSON.stringify(item.result).replace(/'/g,'&#39;')})">
      <b>${escHtml(item.topic || 'Untitled')}</b>
      <span>${platform} • ${item.mode} • ${date}</span>
    </div>`;
  }).join('');
}

function showHistoryItem(topic, result) {
  document.getElementById('topicInput').value = topic;
  const list = document.getElementById('resultsList');
  list.innerHTML = `<div class="result-card"><div class="result-content">${escHtml(result)}</div><div class="result-footer"><div></div><button class="copy-btn" onclick="navigator.clipboard.writeText(${JSON.stringify(result)}).then(()=>{this.textContent='Copied!';setTimeout(()=>this.textContent='Copy',2000)})">Copy</button></div></div>`;
  document.getElementById('resultsTitle').textContent = 'From history';
  document.getElementById('upgradePrompt').style.display = 'none';
  document.getElementById('resultsSection').classList.add('show');
  document.getElementById('resultsSection').scrollIntoView({behavior: 'smooth'});
}

function escHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

async function clearHistory() {
  await fetch('/api/history/clear', {method: 'POST'});
  await loadHistory();
}

async function doUpgrade() {
  const res = await fetch('/api/upgrade', {method: 'POST'});
  const data = await res.json();
  if (data.url) location.href = data.url;
  else showError(data.error || 'Could not open payment page');
}

async function doLogout() {
  await fetch('/api/logout', {method: 'POST'});
  location.href = '/';
}

// Check for payment success in URL
if (location.search.includes('payment=success')) {
  setTimeout(() => {
    alert('🎉 Payment confirmed! Your Pro account is now active.');
    history.replaceState({}, '', '/dashboard');
  }, 500);
}

loadUser();
</script>
</body>
</html>"""

# ========================= HOME PAGE (inline) =========================
HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius — Go Viral. In Your Voice.</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#070b10;--card:#0d1525;--border:#1e2d3d;--text:#eef6ff;--muted:#7a8fa6;--brand:#14b8a6;--brand2:#38bdf8;--gold:#f6b21a}
body{background:radial-gradient(circle at 50% -5%,rgba(20,184,166,0.12),transparent 40%),var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif;min-height:100vh;overflow-x:hidden;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:1rem 1.25rem;border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;background:rgba(7,11,16,0.92);backdrop-filter:blur(14px)}
.logo{font-weight:800;font-size:1.15rem;letter-spacing:-.03em}.logo span{color:var(--brand2)}
.nav-btns{display:flex;gap:8px}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:10px;cursor:pointer;font-family:Inter,sans-serif;font-size:14px;font-weight:600}
.btn-primary{background:linear-gradient(135deg,var(--brand),var(--brand2));border:none;color:#031013;padding:8px 16px;border-radius:10px;cursor:pointer;font-family:Inter,sans-serif;font-size:14px;font-weight:700}
.hero{text-align:center;padding:56px 20px 40px;max-width:700px;margin:0 auto}
.badge{display:inline-block;background:rgba(56,189,248,0.1);border:1px solid rgba(56,189,248,0.25);color:var(--brand2);padding:6px 14px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:24px}
.hero h1{font-size:clamp(2.2rem,8vw,3.8rem);font-weight:900;line-height:1.05;letter-spacing:-.04em;margin-bottom:16px}
.hero h1 span{background:linear-gradient(135deg,var(--brand2),var(--gold));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero p{color:var(--muted);font-size:1.05rem;line-height:1.7;max-width:500px;margin:0 auto 28px}
.hero-btn{display:inline-block;background:linear-gradient(135deg,var(--brand),var(--gold));color:#031013;padding:14px 28px;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;border:none;font-family:Inter,sans-serif}
.section{padding:48px 20px;max-width:1000px;margin:0 auto}
.section-label{text-align:center;color:var(--brand2);font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;margin-bottom:10px}
.section h2{text-align:center;font-size:clamp(1.6rem,4vw,2.4rem);font-weight:800;margin-bottom:10px;letter-spacing:-.03em}
.section .sub{text-align:center;color:var(--muted);max-width:450px;margin:0 auto 28px;font-size:15px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:18px}
.card .tag{display:inline-block;background:rgba(56,189,248,0.1);color:var(--brand2);padding:4px 10px;border-radius:100px;font-size:12px;font-weight:600;margin-bottom:12px}
.card .prompt{color:var(--muted);font-size:13px;margin-bottom:12px;font-style:italic}
.card .output p{margin-bottom:8px;padding-left:10px;border-left:2px solid var(--brand);font-size:14px;line-height:1.55}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:24px}
.step{text-align:center}
.step-num{width:44px;height:44px;border-radius:50%;background:linear-gradient(135deg,var(--brand),var(--gold));display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;margin:0 auto 12px;color:#031013}
.step h3{font-size:15px;font-weight:700;margin-bottom:6px}
.step p{color:var(--muted);font-size:13px;line-height:1.55}
.regions{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:20px}
.region-tag{background:var(--card);border:1px solid var(--border);padding:7px 14px;border-radius:100px;font-size:13px}
.pricing{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;max-width:620px;margin:24px auto 0}
.pcard{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:20px}
.pcard.featured{border-color:var(--brand);position:relative}
.pcard.featured::before{content:'MOST POPULAR';position:absolute;top:-10px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,var(--brand),var(--gold));color:#031013;font-size:10px;font-weight:800;padding:3px 10px;border-radius:100px;letter-spacing:1px;white-space:nowrap}
.pcard .price-label{color:var(--muted);font-size:13px;margin-bottom:4px}
.pcard .price-amount{font-size:2.2rem;font-weight:900;letter-spacing:-.04em;margin-bottom:2px}
.pcard .price-period{color:var(--muted);font-size:13px;margin-bottom:16px}
.pcard ul{list-style:none;margin-bottom:20px}
.pcard ul li{padding:6px 0;border-bottom:1px solid var(--border);font-size:13px;color:var(--muted)}
.pcard ul li span{color:var(--text)}
.pcard ul li::before{content:'✓ ';color:var(--brand2)}
.pcard .pcard-btn{width:100%;padding:12px;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;font-family:Inter,sans-serif;background:linear-gradient(135deg,var(--brand),var(--gold));border:none;color:#031013}
footer{border-top:1px solid var(--border);padding:28px 20px;text-align:center;color:var(--muted);font-size:13px}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.85);backdrop-filter:blur(8px);z-index:1000;align-items:center;justify-content:center;padding:20px}
.modal-overlay.active{display:flex}
.modal{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:24px;width:100%;max-width:400px}
.modal h2{font-size:1.4rem;font-weight:800;margin-bottom:4px}
.modal p{color:var(--muted);font-size:13px;margin-bottom:16px}
.tabs{display:flex;gap:4px;background:var(--bg);padding:3px;border-radius:10px;margin-bottom:16px}
.tab{flex:1;padding:8px;text-align:center;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600;border:none;background:transparent;color:var(--muted);font-family:Inter,sans-serif;transition:all 0.2s}
.tab.active{background:var(--brand);color:#031013}
.fg{margin-bottom:12px}
.fg label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;font-weight:600}
.fg input,.fg select{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:11px 12px;border-radius:10px;font-family:Inter,sans-serif;font-size:15px;outline:none}
.fg input:focus,.fg select:focus{border-color:var(--brand2)}
.form-error{color:#fb7185;font-size:13px;margin-top:6px;display:none}
.submit-btn{width:100%;padding:13px;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;font-family:Inter,sans-serif;background:linear-gradient(135deg,var(--brand),var(--gold));border:none;color:#031013;margin-top:6px}
.cancel{background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;margin-top:10px;display:block;text-align:center;width:100%}
@media(max-width:600px){nav{padding:.8rem 1rem}.hero{padding:40px 16px 32px}.section{padding:36px 16px}.cards,.pricing,.steps{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav>
  <div class="logo">Tik<span>Genius</span></div>
  <div class="nav-btns">
    <button class="btn-ghost" onclick="openModal('login')">Log in</button>
    <button class="btn-primary" onclick="openModal('signup')">Start Free</button>
  </div>
</nav>

<div class="hero">
  <div class="badge">✦ AI Content Studio for Creators</div>
  <h1>Go viral.<br><span>In your voice.</span></h1>
  <p>TikGenius writes your TikTok captions, hooks, POVs, scripts and Twitter threads — in the cultural voice your audience actually connects with.</p>
  <button class="hero-btn" onclick="openModal('signup')">Start Free — No Card Needed</button>
</div>

<div class="section">
  <div class="section-label">Real Output</div>
  <h2>Content that actually hits</h2>
  <p class="sub">Ready to copy and post — no editing needed</p>
  <div class="cards">
    <div class="card">
      <div class="tag">TikTok Captions</div>
      <div class="prompt">"I work so hard but I'm still broke"</div>
      <div class="output">
        <p>I used to think hard work guaranteed results. Nobody told me about the gap in between.</p>
        <p>Working hard in silence because not everyone needs to see the process. The results will speak.</p>
        <p>Nobody prepared me for how lonely the building phase would feel. 😭</p>
      </div>
    </div>
    <div class="card">
      <div class="tag">TikTok Hooks</div>
      <div class="prompt">"I prayed for this life and I'm still not happy"</div>
      <div class="output">
        <p>God answered every prayer. I still found something to worry about.</p>
        <p>Tell me why I got everything I asked for and I'm still not satisfied 😭</p>
        <p>POV: you built the life you dreamed about. The dream forgot the anxiety part.</p>
      </div>
    </div>
    <div class="card">
      <div class="tag">Twitter / X</div>
      <div class="prompt">"Why resting feels like a crime"</div>
      <div class="output">
        <p>You are not lazy. You are exhausted. There is a difference nobody let you learn.</p>
        <p>Rest is not a reward for finishing everything. Nothing is ever finished. Rest anyway.</p>
        <p>Success without peace is just a well-funded anxiety attack.</p>
      </div>
    </div>
  </div>
</div>

<div class="section">
  <div class="section-label">How It Works</div>
  <h2>Three steps</h2>
  <div class="steps">
    <div class="step"><div class="step-num">1</div><h3>Pick your region</h3><p>Nigerian, American, British, Caribbean, East African, South African, or Global.</p></div>
    <div class="step"><div class="step-num">2</div><h3>Type your topic</h3><p>Describe your video or post. The more specific, the better the output.</p></div>
    <div class="step"><div class="step-num">3</div><h3>Copy and post</h3><p>Each result comes with a copy button and the best time to post it.</p></div>
  </div>
</div>

<div class="section">
  <div class="section-label">Global</div>
  <h2>Your culture. Your voice.</h2>
  <p class="sub">Content written in the voice your audience actually understands</p>
  <div class="regions">
    <span class="region-tag">🇳🇬 Nigerian</span>
    <span class="region-tag">🇺🇸 American</span>
    <span class="region-tag">🇬🇧 British</span>
    <span class="region-tag">🇯🇲 Caribbean</span>
    <span class="region-tag">🇰🇪 East African</span>
    <span class="region-tag">🇿🇦 South African</span>
    <span class="region-tag">🌍 Global</span>
  </div>
</div>

<div class="section">
  <div class="section-label">Pricing</div>
  <h2>Simple pricing</h2>
  <div class="pricing">
    <div class="pcard">
      <div class="price-label">Free Forever</div>
      <div class="price-amount">₦0</div>
      <div class="price-period">4 results · 5 generations/day</div>
      <ul>
        <li><span>Hooks, captions, POVs</span></li>
        <li><span>Hashtags + bios</span></li>
        <li><span>All 7 regions</span></li>
        <li><span>TikTok + Twitter/X</span></li>
        <li><span>Best posting time per result</span></li>
      </ul>
      <button class="pcard-btn" onclick="openModal('signup')">Start Free</button>
    </div>
    <div class="pcard featured">
      <div class="price-label">Pro</div>
      <div class="price-amount">₦2,000</div>
      <div class="price-period">7 results · unlimited generations</div>
      <ul>
        <li><span>Everything in Free</span></li>
        <li><span>Full 60-second scripts</span></li>
        <li><span>Full X threads</span></li>
        <li><span>Trend ideas for your niche</span></li>
        <li><span>No daily limits ever</span></li>
      </ul>
      <button class="pcard-btn" onclick="openModal('signup')">Get Pro</button>
    </div>
  </div>
</div>

<footer>
  <div style="font-weight:800;font-size:16px;margin-bottom:8px">Tik<span style="color:#38bdf8">Genius</span></div>
  <p>AI content studio for TikTok & X creators worldwide · Built for creators who want to grow</p>
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
      <div class="fg"><label>Email</label><input type="email" id="signupEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="signupPassword" placeholder="Min 6 characters"></div>
      <div class="fg"><label>Your Content Region</label>
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
      <button class="submit-btn" onclick="doSignup()">Create Account</button>
    </div>
    <div id="loginForm" style="display:none">
      <div class="fg"><label>Email</label><input type="email" id="loginEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="loginPassword" placeholder="Your password"></div>
      <div class="form-error" id="loginError"></div>
      <button class="submit-btn" onclick="doLogin()">Log In</button>
    </div>
    <button class="cancel" onclick="closeModal()">Cancel</button>
  </div>
</div>

<script>
function openModal(tab){document.getElementById('authModal').classList.add('active');switchTab(tab)}
function closeModal(){document.getElementById('authModal').classList.remove('active')}
function switchTab(tab){
  document.getElementById('signupForm').style.display=tab==='signup'?'block':'none';
  document.getElementById('loginForm').style.display=tab==='login'?'block':'none';
  document.getElementById('signupTab').classList.toggle('active',tab==='signup');
  document.getElementById('loginTab').classList.toggle('active',tab==='login');
  document.getElementById('modalTitle').textContent=tab==='signup'?'Create your account':'Welcome back';
  document.getElementById('modalSub').textContent=tab==='signup'?'Start free — no card needed':'Log in to your TikGenius account';
}
async function doSignup(){
  const email=document.getElementById('signupEmail').value;
  const password=document.getElementById('signupPassword').value;
  const region=document.getElementById('signupRegion').value;
  const err=document.getElementById('signupError');
  err.style.display='none';
  const res=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,region})});
  const data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return}
  location.href=data.redirect;
}
async function doLogin(){
  const email=document.getElementById('loginEmail').value;
  const password=document.getElementById('loginPassword').value;
  const err=document.getElementById('loginError');
  err.style.display='none';
  const res=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
  const data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return}
  location.href=data.redirect;
}
document.getElementById('authModal').addEventListener('click',function(e){if(e.target===this)closeModal()});
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
ENDOFFILE
echo "Done"
