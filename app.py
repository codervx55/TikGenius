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
from flask import Flask, request, jsonify, session, redirect, url_for, Response
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

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = "6415641863"
ADMIN_EXPORT_KEY = os.getenv("ADMIN_EXPORT_KEY", SECRET_KEY)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@tikgenius.app")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", ADMIN_EXPORT_KEY)

app = Flask(__name__)
app.secret_key = SECRET_KEY
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = os.getenv("SESSION_COOKIE_HTTPONLY", "true").lower() == "true"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
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
            cur.execute("ALTER TABLE web_payments ADD COLUMN IF NOT EXISTS raw_email TEXT")
            cur.execute("ALTER TABLE web_payments ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'web'")
            # Website analytics shown only in the admin dashboard
            cur.execute("""CREATE TABLE IF NOT EXISTS site_page_views (
                id SERIAL PRIMARY KEY,
                path TEXT NOT NULL,
                method TEXT DEFAULT 'GET',
                referrer TEXT,
                user_agent TEXT,
                ip_hash TEXT,
                user_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS site_clicks (
                id SERIAL PRIMARY KEY,
                element TEXT NOT NULL,
                label TEXT,
                path TEXT,
                referrer TEXT,
                ip_hash TEXT,
                user_id INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_page_views_created ON site_page_views(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_page_views_path ON site_page_views(path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_clicks_created ON site_clicks(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_clicks_element ON site_clicks(element)")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= AUTH HELPERS =========================
def keep_user_signed_in(user_id, email):
    """Create a rolling 30-day login session for web users."""
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["email"] = email
    session.modified = True

@app.before_request
def refresh_web_login_session():
    # Any logged-in web user keeps a rolling 30-day session while they are active.
    if session.get("user_id"):
        session.permanent = True
        session.modified = True

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
    """Save successful Paystack payment once. Duplicate callbacks/webhooks are ignored."""
    if not reference:
        return
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
        "callback_url": request.host_url.rstrip("/") + "/paystack/callback",
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


# ========================= WEBSITE ANALYTICS =========================
def visitor_hash():
    """Privacy-friendly visitor identifier for counting unique visitors."""
    raw = f"{request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0]}|{request.headers.get('User-Agent', '')}"
    return hashlib.sha256((SECRET_KEY + raw).encode()).hexdigest()[:32]

def should_track_request():
    if request.method != "GET":
        return False
    path = request.path or "/"
    if path.startswith(("/admin", "/api", "/paystack", "/telegram-webhook", "/static")):
        return False
    return path in ("/", "/dashboard")

@app.before_request
def record_page_view():
    if not should_track_request():
        return
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO site_page_views
                (path, method, referrer, user_agent, ip_hash, user_id)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                (request.path, request.method, request.referrer, request.headers.get("User-Agent", "")[:500], visitor_hash(), session.get("user_id")))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Analytics page-view error: {e}")
    finally:
        if conn:
            release_db(conn)

@app.route("/api/track-click", methods=["POST"])
def track_click():
    data = request.json or {}
    element = (data.get("element") or "unknown")[:120]
    label = (data.get("label") or "")[:200]
    path = (data.get("path") or request.referrer or "")[:300]
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO site_clicks
                (element, label, path, referrer, ip_hash, user_id)
                VALUES (%s, %s, %s, %s, %s, %s)""",
                (element, label, path, request.referrer, visitor_hash(), session.get("user_id")))
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Analytics click error: {e}")
    finally:
        if conn:
            release_db(conn)
    return jsonify({"success": True})

ANALYTICS_JS = """<script>
(function(){
  function sendClick(el){
    try{
      var label=(el.innerText||el.value||el.getAttribute('aria-label')||el.id||el.className||'').toString().trim().slice(0,200);
      var element=(el.id||el.name||el.className||el.tagName||'unknown').toString().slice(0,120);
      var payload=JSON.stringify({element:element,label:label,path:location.pathname});
      if(navigator.sendBeacon){navigator.sendBeacon('/api/track-click', new Blob([payload],{type:'application/json'}));}
      else{fetch('/api/track-click',{method:'POST',headers:{'Content-Type':'application/json'},body:payload,keepalive:true});}
    }catch(e){}
  }
  document.addEventListener('click',function(e){
    var el=e.target.closest('button,a,[data-track-click]');
    if(el) sendClick(el);
  },true);
})();
</script>"""

def with_analytics(html):
    return html.replace("</body>", ANALYTICS_JS + "</body>") if isinstance(html, str) else html

# ========================= WEB ROUTES =========================

@app.route("/")
def index():
    return with_analytics(HOME_HTML)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")
    return with_analytics(DASHBOARD_HTML)

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
        keep_user_signed_in(user_id, email)
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
        keep_user_signed_in(user["id"], user["email"])
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
        return jsonify({"error": "Topic is required"}), 400
    if len(topic.split()) < 3:
        return jsonify({"error": "Be more specific — add more detail to your topic"}), 400

    user_id = session["user_id"]
    if not check_and_increment_web_usage(user_id):
        return jsonify({"error": "You have used all 5 free generations today. Upgrade to Pro for unlimited access."}), 429

    user = get_web_user(user_id)
    region = user["region"] if user else "global"
    result = ask_groq(mode, topic, platform, region)

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO web_generations (user_id, mode, platform, topic, result)
                VALUES (%s, %s, %s, %s, %s)""", (user_id, mode, platform, topic, result))
        conn.commit()
    finally:
        release_db(conn)

    return jsonify({
        "result": result,
        "uses_remaining": web_uses_remaining(user_id) if not is_web_pro(user_id) else None,
        "unlimited": is_web_pro(user_id)
    })

@app.route("/api/upgrade", methods=["POST"])
@login_required
def upgrade():
    user = get_web_user(session["user_id"])
    if not user:
        return jsonify({"error": "Please log in again to upgrade."}), 401
    if not PAYSTACK_SECRET_KEY:
        return jsonify({"error": "Payment is not configured yet. Add PAYSTACK_SECRET_KEY on Railway."}), 500
    link = create_payment_link(user["email"], session["user_id"], "web")
    if link:
        return jsonify({"url": link})
    return jsonify({"error": "Could not create payment link. Please try again."}), 500

@app.route("/upgrade")
@login_required
def upgrade_redirect():
    user = get_web_user(session["user_id"])
    if not user or not PAYSTACK_SECRET_KEY:
        return redirect("/dashboard")
    link = create_payment_link(user["email"], session["user_id"], "web")
    return redirect(link or "/dashboard")

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


# ========================= ADMIN PANEL =========================
def admin_allowed():
    if session.get("admin_authed") is True:
        return True
    key = request.args.get("key") or request.headers.get("X-Admin-Key")
    return bool(ADMIN_EXPORT_KEY and key and hmac.compare_digest(str(key), str(ADMIN_EXPORT_KEY)))

def admin_login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not admin_allowed():
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper

def money_ngn(kobo):
    return f"₦{(int(kobo or 0) / 100):,.0f}"

ADMIN_LOGIN_HTML = """<!doctype html>
<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>TikGenius Admin Login</title>
<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap' rel='stylesheet'>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:Inter,system-ui,sans-serif;background:radial-gradient(circle at 20% 0,#18345a 0,#08111e 34%,#05070c 100%);color:#f8fbff;display:grid;place-items:center;padding:18px}.login{width:min(440px,100%);background:rgba(10,18,32,.82);border:1px solid rgba(125,167,255,.22);box-shadow:0 30px 90px rgba(0,0,0,.42);border-radius:28px;padding:26px;backdrop-filter:blur(16px)}.brand{display:flex;align-items:center;gap:10px;font-weight:900;font-size:24px;letter-spacing:-.04em}.mark{width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,#22d3ee,#10b981,#f59e0b);display:grid;place-items:center;color:#061018;font-weight:900}.muted{color:#98a9c4;line-height:1.6;margin:8px 0 22px}label{font-size:13px;color:#b8c7dd;font-weight:700;display:block;margin:14px 0 7px}input{width:100%;padding:15px 16px;border-radius:16px;border:1px solid #263852;background:#070d16;color:#fff;font:600 16px Inter;outline:none}input:focus{border-color:#38bdf8;box-shadow:0 0 0 4px rgba(56,189,248,.10)}button{width:100%;margin-top:18px;border:0;border-radius:16px;padding:15px;background:linear-gradient(135deg,#22d3ee,#10b981,#f6b21a);font-weight:900;color:#061018;font-size:16px}.err{display:%ERRDISPLAY%;margin-top:14px;color:#fecdd3;background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.3);padding:12px;border-radius:14px;font-weight:700}.foot{font-size:12px;color:#77859a;margin-top:16px;text-align:center}
</style></head><body><form class='login' method='post'><div class='brand'><div class='mark'>TG</div><div>TikGenius Admin</div></div><p class='muted'>Private dashboard for revenue, premium users, free users, email list and payments.</p><label>Admin email</label><input name='email' type='email' autocomplete='username' required><label>Password</label><input name='password' type='password' autocomplete='current-password' required><button>Unlock Dashboard</button><div class='err'>%ERROR%</div><div class='foot'>Protected by session login. Do not share your admin password.</div></form></body></html>"""

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if hmac.compare_digest(email, (ADMIN_EMAIL or "").strip().lower()) and hmac.compare_digest(password, str(ADMIN_PASSWORD or "")):
            session["admin_authed"] = True
            return redirect(url_for("admin_panel"))
        error = "Wrong admin email or password."
    html = ADMIN_LOGIN_HTML.replace("%ERROR%", escape(error)).replace("%ERRDISPLAY%", "block" if error else "none")
    return html

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authed", None)
    return redirect(url_for("admin_login"))

@app.route("/admin")
@app.route("/admin/emails")
@admin_login_required
def admin_panel():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM web_users")
            total_users = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS premium FROM web_users WHERE plan='pro' AND expires >= CURRENT_DATE")
            premium_users = cur.fetchone()["premium"]
            cur.execute("SELECT COUNT(*) AS free FROM web_users WHERE NOT (plan='pro' AND expires >= CURRENT_DATE)")
            free_users = cur.fetchone()["free"]
            cur.execute("SELECT COUNT(*) AS today FROM web_users WHERE created_at::date = CURRENT_DATE")
            today_signups = cur.fetchone()["today"]
            cur.execute("SELECT COUNT(*) AS gens FROM web_generations")
            total_generations = cur.fetchone()["gens"]
            cur.execute("""SELECT COUNT(*) AS views,
                                  COUNT(DISTINCT ip_hash) AS visitors,
                                  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS views_24h,
                                  COUNT(DISTINCT ip_hash) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS visitors_24h
                           FROM site_page_views""")
            traffic_stats = cur.fetchone()
            cur.execute("""SELECT COUNT(*) AS clicks,
                                  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS clicks_24h
                           FROM site_clicks""")
            click_stats = cur.fetchone()
            cur.execute("""SELECT path, COUNT(*) AS views, COUNT(DISTINCT ip_hash) AS visitors
                           FROM site_page_views GROUP BY path ORDER BY views DESC LIMIT 8""")
            top_pages = cur.fetchall()
            cur.execute("""SELECT element, COALESCE(NULLIF(label, ''), element) AS label, COUNT(*) AS clicks
                           FROM site_clicks GROUP BY element, label ORDER BY clicks DESC LIMIT 10""")
            top_clicks = cur.fetchall()
            cur.execute("SELECT COALESCE(SUM(amount_kobo),0) AS revenue, COUNT(*) AS count FROM web_payments WHERE status='success'")
            pay_stats = cur.fetchone()
            cur.execute("""SELECT p.reference, p.amount_kobo, p.currency, p.source, p.paid_at,
                                  COALESCE(w.email, p.raw_email, '') AS email
                           FROM web_payments p
                           LEFT JOIN web_users w ON w.id=p.user_id
                           ORDER BY p.paid_at DESC LIMIT 20""")
            payments = cur.fetchall()
            cur.execute("""SELECT id, COALESCE(name, '') AS name, email, plan, expires, region, usage_count, created_at, last_login_at
                           FROM web_users ORDER BY created_at DESC LIMIT 300""")
            users = cur.fetchall()
    finally:
        release_db(conn)

    conversion = round((premium_users / total_users * 100), 1) if total_users else 0
    payment_rows = "".join(
        f"<tr><td>{escape(str(p['paid_at'] or ''))}</td><td>{escape(p['email'] or '')}</td><td>{money_ngn(p['amount_kobo'])}</td><td>{escape(p['source'] or '')}</td><td class='muted ref'>{escape(p['reference'] or '')}</td></tr>"
        for p in payments
    ) or "<tr><td colspan='5' class='muted'>No payment recorded yet. New successful Paystack payments will appear here.</td></tr>"

    user_rows = "".join(
        f"<tr><td>{u['id']}</td><td>{escape(u['name'] or '')}</td><td>{escape(u['email'])}</td><td><span class='pill {('pro' if u['plan']=='pro' and u['expires'] else 'free')}'>{escape(u['plan'] or 'free')}</span></td><td>{escape(str(u['expires'] or ''))}</td><td>{escape(u['region'] or '')}</td><td>{u['usage_count'] or 0}</td><td>{escape(str(u['created_at'] or ''))}</td><td>{escape(str(u['last_login_at'] or ''))}</td></tr>"
        for u in users
    ) or "<tr><td colspan='9' class='muted'>No users yet.</td></tr>"

    top_page_rows = "".join(
        f"<tr><td>{escape(r['path'] or '')}</td><td>{r['views']}</td><td>{r['visitors']}</td></tr>" for r in top_pages
    ) or "<tr><td colspan='3' class='muted'>No page views tracked yet.</td></tr>"
    top_click_rows = "".join(
        f"<tr><td>{escape(r['label'] or '')}</td><td>{escape(r['element'] or '')}</td><td>{r['clicks']}</td></tr>" for r in top_clicks
    ) or "<tr><td colspan='3' class='muted'>No clicks tracked yet.</td></tr>"

    return f"""<!doctype html>
<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>TikGenius Admin</title>
<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap' rel='stylesheet'>
<style>
:root{{--bg:#060a12;--panel:#0d1525;--panel2:#111c31;--line:#243550;--text:#f3f7ff;--muted:#93a4bd;--cyan:#38bdf8;--green:#10b981;--gold:#f6b21a;--red:#fb7185}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,system-ui,sans-serif;background:radial-gradient(circle at top left,#172b52 0,#081120 34%,#05070c 100%);color:var(--text);min-height:100vh}}.wrap{{max-width:1280px;margin:auto;padding:18px}}.hero{{background:linear-gradient(135deg,rgba(56,189,248,.14),rgba(16,185,129,.10),rgba(246,178,26,.10));border:1px solid rgba(125,167,255,.22);border-radius:26px;padding:18px;box-shadow:0 20px 70px rgba(0,0,0,.26);margin-bottom:14px}}.top{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap}}.brand{{display:flex;gap:12px;align-items:center}}.mark{{width:42px;height:42px;border-radius:15px;background:linear-gradient(135deg,var(--cyan),var(--green),var(--gold));display:grid;place-items:center;color:#061018;font-weight:900}}h1{{font-size:clamp(1.35rem,5vw,2.15rem);letter-spacing:-.055em;margin:0}}.muted{{color:var(--muted);font-size:.92rem;line-height:1.5}}.logout{{font-size:.84rem;color:#dbeafe;text-decoration:none;border:1px solid rgba(148,163,184,.25);padding:9px 12px;border-radius:999px;background:rgba(8,13,23,.56)}}.hero-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}}.mini{{padding:12px;border-radius:18px;background:rgba(5,10,18,.55);border:1px solid rgba(148,163,184,.17)}}.mini b{{display:block;font-size:1.08rem}}.mini span{{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:800}}
.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0}}.card{{background:linear-gradient(180deg,rgba(17,28,49,.92),rgba(10,17,30,.96));border:1px solid rgba(90,119,164,.45);border-radius:22px;padding:17px;box-shadow:0 12px 40px rgba(0,0,0,.20)}}.label{{color:#a5b4fc;font-size:.74rem;text-transform:uppercase;letter-spacing:.11em;font-weight:900}}.num{{font-size:clamp(1.7rem,7vw,2.35rem);font-weight:900;letter-spacing:-.05em;margin-top:8px}}.section{{margin-top:14px}}h2{{margin:0 0 12px;font-size:1.05rem;letter-spacing:-.03em}}.tablebox{{overflow:auto;border-radius:18px;border:1px solid rgba(90,119,164,.38)}}table{{width:100%;border-collapse:collapse;min-width:900px;background:#0b1322}}th,td{{padding:12px 13px;border-bottom:1px solid #1e293b;text-align:left;font-size:.86rem;white-space:nowrap}}th{{color:#bfdbfe;background:#101b30;font-size:.72rem;text-transform:uppercase;letter-spacing:.075em}}.ref{{max-width:210px;overflow:hidden;text-overflow:ellipsis}}.pill{{padding:5px 9px;border-radius:999px;font-weight:900;font-size:.72rem}}.pill.pro{{background:rgba(16,185,129,.16);color:#6ee7b7;border:1px solid rgba(16,185,129,.32)}}.pill.free{{background:rgba(99,102,241,.16);color:#c4b5fd;border:1px solid rgba(99,102,241,.32)}}.search{{width:100%;padding:13px 14px;border-radius:14px;border:1px solid #334155;background:#07101d;color:white;margin:4px 0 14px;outline:none}}.search:focus{{border-color:var(--cyan);box-shadow:0 0 0 4px rgba(56,189,248,.10)}}.download-zone{{margin:18px 0 30px;padding:16px;border-radius:22px;border:1px dashed rgba(148,163,184,.35);background:rgba(8,13,23,.45)}}.download-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}a.smallbtn{{background:#17243a;color:#dbeafe;text-decoration:none;padding:8px 10px;border-radius:10px;font-weight:800;font-size:.78rem;display:inline-flex;gap:6px;align-items:center;border:1px solid rgba(148,163,184,.24)}}a.smallbtn:hover{{border-color:var(--cyan)}}
@media(max-width:1000px){{.grid{{grid-template-columns:repeat(2,1fr)}}.hero-stats{{grid-template-columns:1fr 1fr}}}}@media(max-width:560px){{.wrap{{padding:12px}}.hero{{border-radius:22px;padding:15px}}.grid{{grid-template-columns:1fr}}.hero-stats{{grid-template-columns:1fr}}.card{{border-radius:20px}}th,td{{padding:11px 12px;font-size:.82rem}}}}
</style></head>
<body><div class='wrap'>
  <section class='hero'><div class='top'><div class='brand'><div class='mark'>TG</div><div><h1>TikGenius Admin</h1><div class='muted'>Revenue, premium users, free users, emails, payments and growth activity.</div></div></div><a class='logout' href='/admin/logout'>Log out</a></div><div class='hero-stats'><div class='mini'><span>Revenue</span><b>{money_ngn(pay_stats['revenue'])}</b></div><div class='mini'><span>Premium conversion</span><b>{conversion}%</b></div><div class='mini'><span>Today signups</span><b>{today_signups}</b></div></div></section>
  <div class='grid'>
    <div class='card'><div class='label'>Total Revenue</div><div class='num'>{money_ngn(pay_stats['revenue'])}</div><div class='muted'>{pay_stats['count']} successful payments</div></div>
    <div class='card'><div class='label'>Premium Users</div><div class='num'>{premium_users}</div><div class='muted'>Active Pro accounts</div></div>
    <div class='card'><div class='label'>Free Users</div><div class='num'>{free_users}</div><div class='muted'>Not premium yet</div></div>
    <div class='card'><div class='label'>Total Signups</div><div class='num'>{total_users}</div><div class='muted'>{today_signups} today</div></div>
    <div class='card'><div class='label'>Generations</div><div class='num'>{total_generations}</div><div class='muted'>AI outputs created</div></div>
    <div class='card'><div class='label'>Website Impressions</div><div class='num'>{traffic_stats['views'] or 0}</div><div class='muted'>{traffic_stats['views_24h'] or 0} in last 24h</div></div>
    <div class='card'><div class='label'>Unique Visitors</div><div class='num'>{traffic_stats['visitors'] or 0}</div><div class='muted'>{traffic_stats['visitors_24h'] or 0} in last 24h</div></div>
    <div class='card'><div class='label'>Website Clicks</div><div class='num'>{click_stats['clicks'] or 0}</div><div class='muted'>{click_stats['clicks_24h'] or 0} in last 24h</div></div>
  </div>
  <div class='section card'><h2>Website Analytics</h2><div class='tablebox'><table><thead><tr><th>Page</th><th>Impressions</th><th>Unique Visitors</th></tr></thead><tbody>{top_page_rows}</tbody></table></div></div>
  <div class='section card'><h2>Click Tracking</h2><div class='tablebox'><table><thead><tr><th>Button / Link Label</th><th>Element</th><th>Clicks</th></tr></thead><tbody>{top_click_rows}</tbody></table></div></div>
  <div class='section card'><h2>Recent Payments</h2><div class='tablebox'><table><thead><tr><th>Date</th><th>Email</th><th>Amount</th><th>Source</th><th>Reference</th></tr></thead><tbody>{payment_rows}</tbody></table></div></div>
  <div class='section card'><h2>Audience Emails</h2><input class='search' id='search' placeholder='Search email, name, plan...' onkeyup='filterRows()'><div class='tablebox'><table id='users'><thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Plan</th><th>Expires</th><th>Region</th><th>Uses</th><th>Signup Date</th><th>Last Login</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
  <div class='download-zone'><div class='label'>Downloads</div><div class='muted'>Export data only when needed. Keep these files private.</div><div class='download-row'><a class='smallbtn' href='/admin/emails.csv'>⬇ Emails CSV</a><a class='smallbtn' href='/admin/payments.csv'>⬇ Payments CSV</a></div></div>
</div><script>function filterRows(){{let q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('#users tbody tr').forEach(r=>{{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'}})}}</script></body></html>"""

@app.route("/admin/emails.csv")
@admin_login_required
def admin_emails_csv():
    import csv, io
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, COALESCE(name, '') AS name, email, plan, expires, region, usage_count, created_at, last_login_at
                           FROM web_users ORDER BY created_at DESC""")
            users = cur.fetchall()
    finally:
        release_db(conn)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "name", "email", "plan", "expires", "region", "usage_count", "created_at", "last_login_at"])
    for u in users:
        writer.writerow([u["id"], u["name"], u["email"], u["plan"], u["expires"] or "", u["region"], u["usage_count"] or 0, u["created_at"], u["last_login_at"] or ""])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tikgenius_emails.csv"})

@app.route("/admin/payments.csv")
@admin_login_required
def admin_payments_csv():
    import csv, io
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT p.reference, p.amount_kobo, p.currency, p.status, p.source, p.paid_at, COALESCE(w.email, p.raw_email, '') AS email
                           FROM web_payments p LEFT JOIN web_users w ON w.id=p.user_id ORDER BY p.paid_at DESC""")
            payments = cur.fetchall()
    finally:
        release_db(conn)
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(["reference", "email", "amount_naira", "currency", "status", "source", "paid_at"])
    for p in payments:
        writer.writerow([p["reference"], p["email"], int(p["amount_kobo"] or 0)/100, p["currency"], p["status"], p["source"], p["paid_at"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tikgenius_payments.csv"})

@app.route("/api/admin/audience-count")
def admin_audience_count():
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM web_users")
            total = cur.fetchone()["total"]
        return jsonify({"total": total})
    finally:
        release_db(conn)

def verify_paystack_reference(reference):
    """Verify a Paystack transaction and activate the correct user if paid."""
    if not PAYSTACK_SECRET_KEY:
        return False, "Paystack secret key missing"
    if not reference:
        return False, "Missing payment reference"

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers=headers,
            timeout=20
        )
        data = res.json()
    except Exception as e:
        print(f"Paystack verify error: {e}")
        return False, "Could not verify payment"

    if not data.get("status") or data.get("data", {}).get("status") != "success":
        return False, "Payment not successful yet"

    tx = data["data"]
    if int(tx.get("amount", 0)) < PRICE_KOBO:
        return False, "Payment amount is too low"

    metadata = tx.get("metadata") or {}
    source = metadata.get("source", "web")

    reference = tx.get("reference") or reference
    customer = tx.get("customer") or {}
    paid_email = customer.get("email")

    if source == "web":
        web_user_id = metadata.get("web_user_id")
        if not web_user_id:
            return False, "Missing web user ID"
        record_payment(reference, tx.get("amount"), tx.get("currency", "NGN"), tx.get("status", "success"), "web", int(web_user_id), None, paid_email)
        expires = activate_web_pro(int(web_user_id))
        return True, f"Premium activated until {expires}"

    telegram_id = metadata.get("telegram_id")
    if telegram_id:
        record_payment(reference, tx.get("amount"), tx.get("currency", "NGN"), tx.get("status", "success"), "telegram", None, int(telegram_id), paid_email)
        expires = activate_pro(telegram_id)
        send_telegram_message(telegram_id, f"Payment confirmed. Welcome to Pro. Access active till {expires}.")
        return True, f"Telegram premium activated until {expires}"

    return False, "Missing user metadata"

@app.route("/paystack/callback")
def paystack_callback():
    reference = request.args.get("reference") or request.args.get("trxref")
    ok, message = verify_paystack_reference(reference)
    if ok:
        # If the payer is logged in, refresh their session and return to dashboard.
        return redirect("/dashboard?payment=success")
    return f"Payment verification failed: {message}", 400

@app.route("/api/payment-status")
@login_required
def payment_status():
    return jsonify({"premium": is_web_pro(session["user_id"])})

# ========================= PAYSTACK WEBHOOK =========================
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
                    record_payment(reference, amount, data.get("currency", "NGN"), data.get("status", "success"), "web", int(web_user_id), None, paid_email)
                    activate_web_pro(web_user_id)
            else:
                telegram_id = metadata.get("telegram_id")
                if telegram_id:
                    record_payment(reference, amount, data.get("currency", "NGN"), data.get("status", "success"), "telegram", None, int(telegram_id), paid_email)
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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#070b10;--panel:#0d141d;--panel2:#111b27;--line:#213041;--text:#eef6ff;--muted:#8fa0b5;--brand:#14b8a6;--brand2:#38bdf8;--gold:#f6b21a;--danger:#fb7185}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top right,#102435 0,#070b10 38%);color:var(--text);font-family:Inter,system-ui,sans-serif;min-height:100vh}.app{display:grid;grid-template-columns:310px 1fr;min-height:100vh}.side{background:rgba(13,20,29,.92);border-right:1px solid var(--line);padding:18px;position:sticky;top:0;height:100vh;overflow:auto}.logo{font-weight:800;font-size:23px;letter-spacing:-.04em;margin-bottom:14px}.logo span{color:var(--brand2)}.user{font-size:12px;color:var(--muted);padding:10px 12px;background:#0a1018;border:1px solid var(--line);border-radius:14px;margin-bottom:12px}.usage{padding:14px;background:linear-gradient(135deg,#10202b,#111827);border:1px solid var(--line);border-radius:16px;margin-bottom:14px}.usage strong{display:block;font-size:14px;margin-bottom:8px}.bar{height:8px;background:#1e293b;border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--brand),var(--brand2));width:100%}.upgrade{display:none;margin-top:10px;background:linear-gradient(135deg,#14b8a6,#f6b21a);border:0;color:#061018;border-radius:12px;font-weight:800;padding:11px;width:100%}.upgrade.show{display:block}.section-title{font-size:11px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);margin:18px 4px 9px}.modes{display:grid;gap:7px}.mode{border:1px solid transparent;background:transparent;color:var(--muted);text-align:left;padding:11px 12px;border-radius:12px;font-weight:650}.mode.active,.mode:hover{background:#111b27;color:var(--text);border-color:var(--line)}.history-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:18px 4px 9px}.history-head .section-title{margin:0}.clear-history{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:6px 9px;font-size:11px;font-weight:700}.clear-history:hover{color:var(--text);border-color:var(--brand2)}.history{display:grid;gap:8px}.hist{padding:10px;background:#0a1018;border:1px solid var(--line);border-radius:12px;cursor:pointer}.hist b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.hist span{font-size:11px;color:var(--muted)}.empty{color:var(--muted);font-size:13px;line-height:1.45;padding:10px;background:#0a1018;border:1px dashed var(--line);border-radius:12px}.logout{margin-top:14px;width:100%;background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:12px;padding:10px}.main{padding:20px;max-width:980px;width:100%;margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}.mobile-logo{display:none;font-weight:800;font-size:22px}.region{background:#0d141d;color:var(--text);border:1px solid var(--line);border-radius:12px;padding:10px}.card{background:rgba(13,20,29,.84);border:1px solid var(--line);border-radius:22px;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.25)}.guide{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px}.tip{background:#081018;border:1px solid var(--line);border-radius:16px;padding:12px}.tip b{font-size:13px}.tip p{margin:6px 0 0;color:var(--muted);font-size:12px;line-height:1.4}.prompt{width:100%;min-height:150px;background:#071018;color:var(--text);border:1px solid var(--line);border-radius:18px;padding:16px;font:500 16px/1.55 Inter;resize:vertical;outline:none}.prompt:focus{border-color:var(--brand2);box-shadow:0 0 0 4px rgba(56,189,248,.08)}.actions{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px}.hint{font-size:12px;color:var(--muted)}.generate{background:linear-gradient(135deg,var(--brand),var(--gold));border:0;border-radius:14px;color:#061018;font-weight:800;padding:14px 20px;font-size:15px}.error{display:none;margin-top:12px;color:#fecdd3;background:rgba(244,63,94,.1);border:1px solid rgba(244,63,94,.3);padding:12px;border-radius:14px}.premium-lock{display:none;margin-top:14px;padding:16px;border-radius:18px;background:linear-gradient(135deg,rgba(20,184,166,.14),rgba(246,178,26,.12));border:1px solid rgba(246,178,26,.35)}.premium-lock.show{display:block}.premium-lock h3{margin:0 0 6px}.premium-lock p{margin:0 0 12px;color:var(--muted)}.output{display:none;margin-top:16px}.output.show{display:block}.output-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.copy{background:#111b27;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:8px 11px}.result{white-space:pre-wrap;line-height:1.7;color:#dce9f7;background:#071018;border:1px solid var(--line);border-radius:18px;padding:16px}.mobile-history{display:none;margin-bottom:14px}.drawer-btn{display:none;background:#111b27;color:var(--text);border:1px solid var(--line);border-radius:12px;padding:10px 12px}@media(max-width:800px){.app{display:block}.side{display:none}.main{padding:14px}.mobile-logo{display:block}.drawer-btn{display:block}.top{position:sticky;top:0;z-index:5;background:rgba(7,11,16,.94);padding:12px 0;border-bottom:1px solid var(--line)}.guide{grid-template-columns:1fr}.card{padding:14px;border-radius:18px}.prompt{min-height:130px;font-size:15px}.actions{align-items:stretch;flex-direction:column}.generate{width:100%}.mobile-history{display:block}.mobile-history .history{display:flex;overflow:auto;gap:8px;padding-bottom:3px}.mobile-history .hist{min-width:190px}.region{max-width:145px}.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50}.modal.show{display:block}.modal-panel{position:absolute;left:0;top:0;bottom:0;width:85%;max-width:310px;background:#0d141d;border-right:1px solid var(--line);padding:18px;overflow:auto}}@media(min-width:801px){.modal{display:none!important}}
</style>
</head>
<body>
<div class="app">
<aside class="side" id="desktopSide">
  <div class="logo">Tik<span>Genius</span></div>
  <div class="user"><div id="userEmail">Loading...</div></div>
  <div class="usage"><strong id="usesLabel">5/5 free generations left</strong><div class="bar"><div class="fill" id="barFill"></div></div><button type="button" class="upgrade" id="upgradeBtn" data-upgrade onclick="doUpgrade(event)">Upgrade to Premium</button></div>
  <div class="section-title">Create for TikTok & X</div><div class="modes" id="modes"></div>
  <div class="history-head"><div class="section-title">Recent history</div><button class="clear-history" onclick="clearHistory()">Clear</button></div><div class="history" id="historyList"><div class="empty">Your TikTok and X content history will appear here.</div></div>
  <button class="logout" onclick="doLogout()">Log out</button>
</aside>
<div class="modal" id="drawer" onclick="closeDrawer(event)"><div class="modal-panel" id="drawerPanel"></div></div>
<main class="main">
  <div class="top"><div class="mobile-logo">Tik<span style="color:var(--brand2)">Genius</span></div><button class="drawer-btn" onclick="openDrawer()">☰ Menu</button><select class="region" id="regionSelect" onchange="changeRegion(this.value)"><option value="global">🌍 Global</option><option value="nigeria">🇳🇬 Nigerian</option><option value="usa">🇺🇸 American</option><option value="uk">🇬🇧 British</option><option value="caribbean">🇯🇲 Caribbean</option><option value="eastafrica">🇰🇪 East African</option><option value="southafrica">🇿🇦 South African</option></select></div>
  <div class="mobile-history"><div class="history-head"><div class="section-title">Recent history</div><button class="clear-history" onclick="clearHistory()">Clear</button></div><div class="history" id="historyMobile"><div class="empty">No TikTok/X history yet.</div></div></div>
  <section class="card">
    <div class="guide"><div class="tip"><b>1. Choose TikTok or X</b><p>Pick captions, hooks, scripts, hashtags, or X threads from the menu.</p></div><div class="tip"><b>2. Be specific</b><p>Say the topic, audience, emotion, platform, and goal.</p></div><div class="tip"><b>3. Add your style</b><p>Example: funny Nigerian street voice, luxury, Gen Z, or bold X thought-leader.</p></div></div>
    <textarea class="prompt" id="topicInput" placeholder="Example: Give me 5 TikTok captions for a skincare video targeting young women who want clear skin. Or: write an X thread about building discipline as a young creator."></textarea>
    <div class="actions"><div class="hint">Minimum 3 words. Works for TikTok and X.</div><button class="generate" id="generateBtn" onclick="generate()">Generate</button></div>
    <div class="error" id="errorMsg"></div>
    <div class="premium-lock" id="premiumLock"><h3>You used your 5 free generations</h3><p>Upgrade to Premium to keep generating unlimited captions, hooks, scripts and content ideas.</p><button type="button" class="upgrade show" data-upgrade onclick="doUpgrade(event)">Upgrade to Premium</button></div>
  </section>
  <section class="output" id="outputCard"><div class="output-head"><b id="outputTitle">Ready to post</b><button class="copy" onclick="copyOutput()">Copy</button></div><div class="result" id="outputText"></div></section>
</main>
</div>
<script>
let currentMode='captions', currentPlatform='tiktok', userData={};
const modes=[['captions','TikTok Captions','tiktok'],['hooks','Viral Hooks','tiktok'],['pov','POV Ideas','tiktok'],['script','Video Script','tiktok'],['hashtags','Hashtags','tiktok'],['threads','X Thread','x'],['hooks','X Hooks','x']];
const modeTitles={captions:'Captions',hooks:'Hooks',pov:'POV Ideas',script:'Video Script',hashtags:'Hashtags',threads:'X Thread'};
function renderModes(target='modes'){const el=document.getElementById(target); if(!el)return; el.innerHTML=modes.map((m,i)=>`<button class="mode ${i==0?'active':''}" onclick="setMode('${m[0]}','${m[2]}',this)">${m[1]}</button>`).join('')}
function setMode(m,p,btn){currentMode=m;currentPlatform=p;document.querySelectorAll('.mode').forEach(x=>x.classList.remove('active'));if(btn)btn.classList.add('active');document.getElementById('outputCard').classList.remove('show')}
async function loadUser(){const res=await fetch('/api/me');if(res.status===401){location.href='/';return}userData=await res.json();document.querySelectorAll('#userEmail').forEach(e=>e.textContent=userData.email);document.getElementById('regionSelect').value=userData.region||'global';updateUsage(userData.uses_remaining,userData.unlimited);loadHistory()}
function updateUsage(rem,unlimited){const label=document.getElementById('usesLabel'), fill=document.getElementById('barFill'), up=document.getElementById('upgradeBtn'); if(unlimited){label.textContent='Premium: unlimited generations';fill.style.width='100%';up.classList.remove('show');return} label.textContent=rem+'/5 free generations left';fill.style.width=(rem/5*100)+'%';if(rem<=0){up.classList.add('show');document.getElementById('premiumLock').classList.add('show')}else{up.classList.remove('show')}}
async function loadHistory(){const res=await fetch('/api/history');const data=await res.json();const html=(data.items&&data.items.length)?data.items.map(i=>{const platform=(i.platform==='x')?'X':'TikTok';return `<div class="hist" onclick='showHistory(${JSON.stringify(i).replace(/'/g,"&#39;")})'><b>${escapeHtml(i.topic||'Untitled')}</b><span>${platform} • ${i.mode} • ${new Date(i.created_at).toLocaleDateString()}</span></div>`}).join(''):'<div class="empty">Your TikTok and X content history will appear here.</div>';['historyList','historyMobile','drawerHistory'].forEach(id=>{const el=document.getElementById(id);if(el)el.innerHTML=html})}
async function clearHistory(){if(!confirm('Clear all your TikTok and X generation history?'))return;const res=await fetch('/api/history/clear',{method:'POST'});if(res.ok){document.getElementById('outputCard').classList.remove('show');loadHistory()}else{showError('Could not clear history. Please try again.')}}
function showHistory(i){document.getElementById('topicInput').value=i.topic||'';document.getElementById('outputText').textContent=i.result||'';document.getElementById('outputTitle').textContent=(modeTitles[i.mode]||i.mode)+' from history';document.getElementById('outputCard').classList.add('show');document.getElementById('outputCard').scrollIntoView({behavior:'smooth'});document.getElementById('drawer').classList.remove('show')}
function escapeHtml(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function changeRegion(region){await fetch('/api/set-region',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region})})}
async function generate(){const topic=document.getElementById('topicInput').value.trim(),btn=document.getElementById('generateBtn');hideError();if(!topic)return showError('Please enter your prompt.');if(topic.split(/\s+/).length<3)return showError('Please add at least 3 words.');btn.disabled=true;btn.textContent='Generating...';const res=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:currentMode,platform:currentPlatform,topic})});const data=await res.json();btn.disabled=false;btn.textContent='Generate';if(data.error){showError(data.error);if(res.status===429)document.getElementById('premiumLock').classList.add('show');return}document.getElementById('outputText').textContent=data.result;document.getElementById('outputTitle').textContent=(modeTitles[currentMode]||currentMode)+' — ready to post';document.getElementById('outputCard').classList.add('show');document.getElementById('outputCard').scrollIntoView({behavior:'smooth'});if(data.uses_remaining!==undefined)updateUsage(data.uses_remaining,false);loadHistory()}
function showError(m){const e=document.getElementById('errorMsg');e.textContent=m;e.style.display='block'}function hideError(){document.getElementById('errorMsg').style.display='none'}
function copyOutput(){navigator.clipboard.writeText(document.getElementById('outputText').textContent)}
let upgradeInProgress=false;
async function doUpgrade(event){
  if(event && event.preventDefault) event.preventDefault();
  if(upgradeInProgress) return false;
  upgradeInProgress=true;
  hideError();
  const buttons=Array.from(document.querySelectorAll('[data-upgrade], .upgrade'));
  buttons.forEach(b=>{b.disabled=true; b.dataset.oldText=b.textContent; b.textContent='Opening payment...'});
  try{
    const res=await fetch('/api/upgrade',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json'}});
    let data={};
    try{data=await res.json()}catch(e){}
    if(res.status===401){location.href='/';return false}
    if(data.url){window.location.assign(data.url);return false}
    showError(data.error||'Could not open payment page. Please try again.');
  }catch(e){
    showError('Network error. Please check your connection and try again.');
  }finally{
    upgradeInProgress=false;
    buttons.forEach(b=>{b.disabled=false; b.textContent=b.dataset.oldText||'Upgrade to Premium'});
  }
  return false;
}
document.addEventListener('click',function(e){
  const btn=e.target.closest('[data-upgrade]');
  if(btn){doUpgrade(e)}
},false);
async function doLogout(){await fetch('/api/logout',{method:'POST'});location.href='/'}
function openDrawer(){const p=document.getElementById('drawerPanel');p.innerHTML=document.getElementById('desktopSide').innerHTML;const h=p.querySelector('#historyList');if(h)h.id='drawerHistory';const m=p.querySelector('#modes');if(m)m.id='drawerModes';document.getElementById('drawer').classList.add('show');renderModes('drawerModes');loadHistory()}function closeDrawer(e){if(e.target.id==='drawer')document.getElementById('drawer').classList.remove('show')}
renderModes();loadUser();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
