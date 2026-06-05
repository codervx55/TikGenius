import os
import hmac
import hashlib
import random
import secrets
import string
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
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

PRICE_KOBO = 200000          # ₦2,000
FREE_LIMIT = 5
DOWNLOADER_FREE_LIMIT = 3
ADMIN_ID = "6415641863"
ADMIN_EXPORT_KEY = os.getenv("ADMIN_EXPORT_KEY", SECRET_KEY)
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@tikgenius.app")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", ADMIN_EXPORT_KEY)

# ── Referral config ──────────────────────────────────────
REFERRAL_COMMISSION_KOBO = int(os.getenv("REFERRAL_COMMISSION_KOBO", "50000"))   # ₦500 default
REFERRAL_MIN_WITHDRAW_KOBO = 200000   # ₦2,000

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
                last_login_at TIMESTAMP,
                referral_code TEXT UNIQUE,
                referred_by INTEGER REFERENCES web_users(id) ON DELETE SET NULL
            )""")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS name TEXT")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS referral_code TEXT")
            cur.execute("ALTER TABLE web_users ADD COLUMN IF NOT EXISTS referred_by INTEGER")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_referral_code ON web_users(referral_code)")

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

            # ── Referrals table ──────────────────────────────
            cur.execute("""CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                referred_id INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                status TEXT DEFAULT 'pending',
                commission_kobo INTEGER DEFAULT 0,
                paid_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(referred_id)
            )""")

            # ── Wallets table ────────────────────────────────
            cur.execute("""CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                balance_kobo INTEGER DEFAULT 0,
                total_earned_kobo INTEGER DEFAULT 0,
                total_withdrawn_kobo INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            )""")

            # ── Withdrawals table ────────────────────────────
            cur.execute("""CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                amount_kobo INTEGER NOT NULL,
                bank_code TEXT NOT NULL,
                account_number TEXT NOT NULL,
                account_name TEXT,
                recipient_code TEXT,
                transfer_reference TEXT,
                status TEXT DEFAULT 'pending',
                failure_reason TEXT,
                requested_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )""")

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
            cur.execute("""CREATE TABLE IF NOT EXISTS web_downloads (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES web_users(id) ON DELETE SET NULL,
                tiktok_url TEXT,
                video_title TEXT,
                plan TEXT DEFAULT 'free',
                ad_watched BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_page_views_created ON site_page_views(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_page_views_path ON site_page_views(path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_clicks_created ON site_clicks(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_site_clicks_element ON site_clicks(element)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_web_downloads_created ON web_downloads(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_web_downloads_user ON web_downloads(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id)")
            cur.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
                id SERIAL PRIMARY KEY,
                ip_hash TEXT NOT NULL,
                email TEXT,
                attempted_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_hash, attempted_at)")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= REFERRAL HELPERS =========================

def generate_referral_code():
    """Generate a unique 8-character alphanumeric referral code."""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=8))
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM web_users WHERE referral_code=%s", (code,))
                if not cur.fetchone():
                    return code
        finally:
            release_db(conn)

def ensure_referral_code(user_id):
    """Make sure a user has a referral code. Returns the code."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT referral_code FROM web_users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if row and row["referral_code"]:
                return row["referral_code"]
            code = generate_referral_code()
            cur.execute("UPDATE web_users SET referral_code=%s WHERE id=%s", (code, user_id))
        conn.commit()
        return code
    finally:
        release_db(conn)

def get_or_create_wallet(user_id, conn=None):
    """Return wallet row, creating it if missing."""
    own_conn = conn is None
    if own_conn:
        conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM wallets WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            if row:
                return row
            cur.execute("""INSERT INTO wallets (user_id, balance_kobo, total_earned_kobo, total_withdrawn_kobo)
                VALUES (%s, 0, 0, 0) ON CONFLICT (user_id) DO NOTHING RETURNING *""", (user_id,))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM wallets WHERE user_id=%s", (user_id,))
                row = cur.fetchone()
        if own_conn:
            conn.commit()
        return row
    finally:
        if own_conn:
            release_db(conn)

def credit_referral_commission(referrer_id, referred_id, amount_kobo):
    """Credit commission to referrer wallet and mark referral as paid."""
    conn = get_db()
    try:
        get_or_create_wallet(referrer_id, conn)
        with conn.cursor() as cur:
            cur.execute("""UPDATE wallets
                SET balance_kobo = balance_kobo + %s,
                    total_earned_kobo = total_earned_kobo + %s,
                    updated_at = NOW()
                WHERE user_id = %s""", (amount_kobo, amount_kobo, referrer_id))
            cur.execute("""UPDATE referrals
                SET status='paid', commission_kobo=%s, paid_at=NOW()
                WHERE referrer_id=%s AND referred_id=%s""",
                (amount_kobo, referrer_id, referred_id))
        conn.commit()
    finally:
        release_db(conn)

def resolve_referrer(ref_code):
    """Return referrer user_id from a ref code, or None."""
    if not ref_code:
        return None
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM web_users WHERE referral_code=%s", (ref_code,))
            row = cur.fetchone()
        return row["id"] if row else None
    finally:
        release_db(conn)

def get_referral_stats(user_id):
    """Return referral stats for a user."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status='paid') AS paid,
                COUNT(*) FILTER (WHERE status='pending') AS pending,
                COALESCE(SUM(commission_kobo) FILTER (WHERE status='paid'), 0) AS total_earned_kobo
                FROM referrals WHERE referrer_id=%s""", (user_id,))
            stats = cur.fetchone()
            wallet = get_or_create_wallet(user_id, conn)
        return dict(stats), dict(wallet)
    finally:
        release_db(conn)

# ========================= PAYSTACK TRANSFER (WITHDRAWAL) =========================

def paystack_get_banks():
    """Fetch list of Nigerian banks from Paystack."""
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get("https://api.paystack.co/bank?currency=NGN&perPage=100",
                               headers=headers, timeout=15)
        data = res.json()
        if data.get("status"):
            return data["data"]
    except Exception as e:
        print(f"Paystack banks error: {e}")
    return []

def paystack_resolve_account(account_number, bank_code):
    """Verify account number with bank."""
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get(
            f"https://api.paystack.co/bank/resolve?account_number={account_number}&bank_code={bank_code}",
            headers=headers, timeout=15)
        data = res.json()
        if data.get("status"):
            return data["data"].get("account_name"), None
        return None, data.get("message", "Could not verify account")
    except Exception as e:
        return None, str(e)

def paystack_create_recipient(account_name, account_number, bank_code):
    """Create a Paystack transfer recipient."""
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {
        "type": "nuban",
        "name": account_name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": "NGN"
    }
    try:
        res = http_session.post("https://api.paystack.co/transferrecipient",
                                json=payload, headers=headers, timeout=15)
        data = res.json()
        if data.get("status"):
            return data["data"]["recipient_code"], None
        return None, data.get("message", "Could not create recipient")
    except Exception as e:
        return None, str(e)

def paystack_initiate_transfer(amount_kobo, recipient_code, reference, reason="TikGenius referral earnings"):
    """Initiate a Paystack transfer."""
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {
        "source": "balance",
        "amount": amount_kobo,
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference
    }
    try:
        res = http_session.post("https://api.paystack.co/transfer",
                                json=payload, headers=headers, timeout=20)
        data = res.json()
        if data.get("status"):
            return data["data"], None
        return None, data.get("message", "Transfer failed")
    except Exception as e:
        return None, str(e)

# ========================= AUTH HELPERS =========================
def keep_user_signed_in(user_id, email):
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    session["email"] = email
    session.modified = True

@app.before_request
def refresh_web_login_session():
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

        # ── Credit referrer if applicable ─────────────────
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT r.referrer_id FROM referrals r
                    WHERE r.referred_id=%s AND r.status='pending'""", (user_id,))
                ref_row = cur.fetchone()
            if ref_row:
                credit_referral_commission(ref_row["referrer_id"], user_id, REFERRAL_COMMISSION_KOBO)
        except Exception as e:
            print(f"Referral commission error: {e}")

        return expires.strftime("%Y-%m-%d")
    finally:
        release_db(conn)

def record_payment(reference, amount_kobo, currency="NGN", status="success", source="web", web_user_id=None, telegram_id=None, raw_email=None):
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
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
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
    raw = f"{request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0]}|{request.headers.get('User-Agent', '')}"
    return hashlib.sha256((SECRET_KEY + raw).encode()).hexdigest()[:32]

def should_track_request():
    if request.method != "GET":
        return False
    path = request.path or "/"
    if path.startswith(("/admin", "/api", "/paystack", "/telegram-webhook", "/static")):
        return False
    return path in ("/", "/dashboard", "/download", "/refer")

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
                (request.path, request.method, request.referrer,
                 request.headers.get("User-Agent", "")[:500], visitor_hash(), session.get("user_id")))
        conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"Analytics page-view error: {e}")
    finally:
        if conn: release_db(conn)

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
        if conn: conn.rollback()
        print(f"Analytics click error: {e}")
    finally:
        if conn: release_db(conn)
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
    ref = request.args.get("ref", "")
    if ref:
        session["pending_ref"] = ref
    return with_analytics(HOME_HTML)

@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")
    return with_analytics(DASHBOARD_HTML)

@app.route("/refer")
@login_required
def refer_page():
    return with_analytics(REFER_HTML)

@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.json or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    region = data.get("region", "global")
    ref_code = data.get("ref_code", "") or session.get("pending_ref", "")

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    referrer_id = resolve_referrer(ref_code) if ref_code else None

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM web_users WHERE email=%s", (email,))
            if cur.fetchone():
                return jsonify({"error": "Email already registered"}), 400

            new_ref_code = generate_referral_code()
            referred_by = referrer_id if referrer_id else None

            cur.execute("""INSERT INTO web_users (name, email, password_hash, region, referral_code, referred_by)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (name, email, generate_password_hash(password), region, new_ref_code, referred_by))
            user_id = cur.fetchone()["id"]

            # Record referral relationship
            if referrer_id and referrer_id != user_id:
                cur.execute("""INSERT INTO referrals (referrer_id, referred_id, status)
                    VALUES (%s, %s, 'pending') ON CONFLICT (referred_id) DO NOTHING""",
                    (referrer_id, user_id))

            # Create wallet
            cur.execute("""INSERT INTO wallets (user_id) VALUES (%s) ON CONFLICT DO NOTHING""", (user_id,))

        conn.commit()
        session.pop("pending_ref", None)
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
    ip = visitor_hash()
    conn = get_db()
    try:
        # Brute-force check: max 10 failed attempts per IP in 15 minutes
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS cnt FROM login_attempts
                WHERE ip_hash=%s AND attempted_at > NOW() - INTERVAL '15 minutes'""", (ip,))
            attempts = cur.fetchone()["cnt"]
        if attempts >= 10:
            return jsonify({"error": "Too many login attempts. Please wait 15 minutes and try again."}), 429
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM web_users WHERE email=%s", (email,))
            user = cur.fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            with conn.cursor() as cur:
                cur.execute("INSERT INTO login_attempts (ip_hash, email) VALUES (%s, %s)", (ip, email[:200]))
            conn.commit()
            return jsonify({"error": "Invalid email or password"}), 401
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET last_login_at=NOW() WHERE id=%s", (user["id"],))
            # Ensure wallet exists
            cur.execute("INSERT INTO wallets (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user["id"],))
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
    ref_code = ensure_referral_code(session["user_id"])
    base_url = request.host_url.rstrip("/")
    return jsonify({
        "email": user["email"],
        "plan": "pro" if pro else "free",
        "expires": user["expires"].strftime("%Y-%m-%d") if user["expires"] else None,
        "region": user["region"],
        "uses_remaining": FREE_LIMIT if pro else web_uses_remaining(session["user_id"]),
        "unlimited": pro,
        "referral_code": ref_code,
        "referral_link": f"{base_url}/?ref={ref_code}"
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

    VALID_MODES = {"captions", "hooks", "pov", "script", "hashtags", "bio", "trends", "threads"}
    VALID_PLATFORMS = {"tiktok", "x"}
    if mode not in VALID_MODES: mode = "captions"
    if platform not in VALID_PLATFORMS: platform = "tiktok"
    topic = topic[:800]
    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    if len(topic.split()) < 3:
        return jsonify({"error": "Be more specific — add at least 3 words"}), 400

    user_id = session["user_id"]
    if not check_and_increment_web_usage(user_id):
        return jsonify({"error": "You have used all 5 free generations today. Upgrade to Premium for unlimited access."}), 429

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
        return jsonify({"error": "Payment is not configured yet."}), 500
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

@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.json or {}
    current = data.get("current_password", "")
    new_pw = data.get("new_password", "")
    if not current or not new_pw:
        return jsonify({"error": "Current and new password are required"}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    user = get_web_user(session["user_id"])
    if not user or not check_password_hash(user["password_hash"], current):
        return jsonify({"error": "Current password is incorrect"}), 401
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET password_hash=%s WHERE id=%s",
                (generate_password_hash(new_pw), session["user_id"]))
        conn.commit()
        return jsonify({"success": True, "message": "Password updated successfully"})
    finally:
        release_db(conn)

@app.route("/api/referral/leaderboard")
def referral_leaderboard():
    """Public leaderboard — top 10 referrers by paid conversions."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT
                w.name, w.email,
                COUNT(*) FILTER (WHERE r.status='paid') AS paid_count,
                COALESCE(SUM(r.commission_kobo) FILTER (WHERE r.status='paid'), 0) AS earned_kobo
                FROM referrals r
                JOIN web_users w ON w.id = r.referrer_id
                GROUP BY w.id, w.name, w.email
                HAVING COUNT(*) FILTER (WHERE r.status='paid') > 0
                ORDER BY paid_count DESC LIMIT 10""")
            rows = cur.fetchall()
        # Mask emails for privacy
        leaderboard = []
        for i, r in enumerate(rows):
            email = r["email"] or ""
            parts = email.split("@")
            masked = parts[0][:2] + "***@" + parts[1] if len(parts) == 2 else "***"
            display_name = (r["name"] or "").strip() or masked
            leaderboard.append({
                "rank": i + 1,
                "name": display_name[:30],
                "paid_referrals": r["paid_count"],
                "earned_ngn": int(r["earned_kobo"]) / 100
            })
        return jsonify({"leaderboard": leaderboard})
    finally:
        release_db(conn)

@app.route("/health")
def health():
    """Railway health check endpoint."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        release_db(conn)
        return jsonify({"status": "ok", "db": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detail": str(e)}), 500

# ========================= REFERRAL API ROUTES =========================

@app.route("/api/referral/stats")
@login_required
def referral_stats():
    user_id = session["user_id"]
    ref_code = ensure_referral_code(user_id)
    base_url = request.host_url.rstrip("/")
    stats, wallet = get_referral_stats(user_id)

    # Recent referrals
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT r.status, r.commission_kobo, r.created_at, r.paid_at,
                w.email
                FROM referrals r
                JOIN web_users w ON w.id = r.referred_id
                WHERE r.referrer_id = %s
                ORDER BY r.created_at DESC LIMIT 20""", (user_id,))
            recent = cur.fetchall()
    finally:
        release_db(conn)

    return jsonify({
        "referral_code": ref_code,
        "referral_link": f"{base_url}/?ref={ref_code}",
        "total_referrals": stats["total"],
        "paid_referrals": stats["paid"],
        "pending_referrals": stats["pending"],
        "total_earned_kobo": stats["total_earned_kobo"],
        "total_earned_ngn": int(stats["total_earned_kobo"]) / 100,
        "balance_kobo": wallet["balance_kobo"],
        "balance_ngn": int(wallet["balance_kobo"]) / 100,
        "total_withdrawn_kobo": wallet["total_withdrawn_kobo"],
        "total_withdrawn_ngn": int(wallet["total_withdrawn_kobo"]) / 100,
        "min_withdraw_ngn": REFERRAL_MIN_WITHDRAW_KOBO / 100,
        "commission_ngn": REFERRAL_COMMISSION_KOBO / 100,
        "can_withdraw": int(wallet["balance_kobo"]) >= REFERRAL_MIN_WITHDRAW_KOBO,
        "recent": [dict(r) for r in recent]
    })

@app.route("/api/referral/banks")
@login_required
def get_banks():
    banks = paystack_get_banks()
    return jsonify({"banks": banks})

@app.route("/api/referral/verify-account", methods=["POST"])
@login_required
def verify_account():
    data = request.json or {}
    account_number = (data.get("account_number") or "").strip()
    bank_code = (data.get("bank_code") or "").strip()
    if not account_number or not bank_code:
        return jsonify({"error": "Account number and bank code required"}), 400
    account_name, err = paystack_resolve_account(account_number, bank_code)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"account_name": account_name})

@app.route("/api/referral/withdraw", methods=["POST"])
@login_required
def withdraw():
    user_id = session["user_id"]
    data = request.json or {}
    account_number = (data.get("account_number") or "").strip()
    bank_code = (data.get("bank_code") or "").strip()
    account_name = (data.get("account_name") or "").strip()

    if not account_number or not bank_code or not account_name:
        return jsonify({"error": "Account number, bank, and account name are required"}), 400

    # Check balance
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance_kobo FROM wallets WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        balance = int(row["balance_kobo"]) if row else 0
    finally:
        release_db(conn)

    if balance < REFERRAL_MIN_WITHDRAW_KOBO:
        return jsonify({"error": f"Minimum withdrawal is ₦{REFERRAL_MIN_WITHDRAW_KOBO//100:,}. Your balance is ₦{balance//100:,}."}), 400

    # Check for pending withdrawal
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM withdrawals WHERE user_id=%s AND status='pending'""", (user_id,))
            if cur.fetchone():
                return jsonify({"error": "You already have a pending withdrawal. Please wait for it to complete."}), 400
    finally:
        release_db(conn)

    # Create Paystack recipient
    recipient_code, err = paystack_create_recipient(account_name, account_number, bank_code)
    if err:
        return jsonify({"error": f"Could not create transfer recipient: {err}"}), 400

    # Initiate transfer
    transfer_ref = f"TIKW-{user_id}-{int(datetime.utcnow().timestamp())}"
    transfer_data, err = paystack_initiate_transfer(balance, recipient_code, transfer_ref)
    if err:
        return jsonify({"error": f"Transfer failed: {err}"}), 400

    # Deduct balance and record withdrawal
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""UPDATE wallets
                SET balance_kobo = balance_kobo - %s,
                    total_withdrawn_kobo = total_withdrawn_kobo + %s,
                    updated_at = NOW()
                WHERE user_id = %s""", (balance, balance, user_id))
            cur.execute("""INSERT INTO withdrawals
                (user_id, amount_kobo, bank_code, account_number, account_name,
                 recipient_code, transfer_reference, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (user_id, balance, bank_code, account_number, account_name,
                 recipient_code, transfer_ref,
                 transfer_data.get("status", "pending")))
        conn.commit()
    finally:
        release_db(conn)

    return jsonify({
        "success": True,
        "message": f"₦{balance//100:,} withdrawal initiated. It will arrive in your account within minutes.",
        "amount_ngn": balance / 100,
        "reference": transfer_ref
    })

@app.route("/api/referral/withdrawal-history")
@login_required
def withdrawal_history():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT amount_kobo, bank_code, account_number, account_name,
                status, failure_reason, requested_at, completed_at
                FROM withdrawals WHERE user_id=%s
                ORDER BY requested_at DESC LIMIT 20""", (session["user_id"],))
            rows = cur.fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        release_db(conn)

# ========================= PAYSTACK TRANSFER WEBHOOK =========================
@app.route("/paystack-webhook", methods=["POST"])
@app.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    if not PAYSTACK_SECRET_KEY:
        return jsonify({"error": "misconfigured"}), 500
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid"}), 400

    event = request.json or {}
    event_type = event.get("event")

    if event_type == "charge.success":
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
                        f"Payment confirmed. Welcome to Pro.\n\nAccess active till {expires}\n\nEverything unlocked.")

    elif event_type == "transfer.success":
        ref = event.get("data", {}).get("reference", "")
        if ref:
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    cur.execute("""UPDATE withdrawals SET status='success', completed_at=NOW()
                        WHERE transfer_reference=%s""", (ref,))
                conn.commit()
            finally:
                release_db(conn)

    elif event_type == "transfer.failed":
        data = event.get("data", {})
        ref = data.get("reference", "")
        reason = data.get("reason") or "Transfer failed"
        if ref:
            conn = get_db()
            try:
                with conn.cursor() as cur:
                    # Refund balance
                    cur.execute("""UPDATE wallets w SET balance_kobo = balance_kobo + wd.amount_kobo
                        FROM withdrawals wd WHERE wd.transfer_reference=%s AND wd.user_id=w.user_id""", (ref,))
                    cur.execute("""UPDATE withdrawals SET status='failed', failure_reason=%s
                        WHERE transfer_reference=%s""", (reason, ref))
                conn.commit()
            finally:
                release_db(conn)

    return jsonify({"status": "ok"}), 200

# ========================= VIDEO DOWNLOADER =========================
def downloader_uses_today(user_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) AS count FROM web_downloads
                WHERE user_id=%s AND plan='free' AND created_at::date = CURRENT_DATE""", (user_id,))
            row = cur.fetchone()
            return int(row["count"] or 0) if row else 0
    finally:
        release_db(conn)

def downloader_uses_remaining(user_id):
    if is_web_pro(user_id): return None
    return max(0, DOWNLOADER_FREE_LIMIT - downloader_uses_today(user_id))

@app.route("/download")
def download_page():
    return with_analytics(DOWNLOAD_HTML)

@app.route("/api/download/file")
@login_required
def download_file():
    file_url = (request.args.get("url") or "").strip()
    filename = (request.args.get("filename") or "tiktok-video.mp4").strip()
    if not file_url:
        return "Missing file URL", 400
    if not file_url.startswith(("http://", "https://")):
        return "Invalid file URL", 400
    safe_filename = "".join(c if c.isalnum() or c in (".", "_", "-") else "_" for c in filename)[:120]
    if not safe_filename:
        safe_filename = "tiktok-video.mp4"
    try:
        upstream = http_session.get(file_url, stream=True, timeout=30)
        upstream.raise_for_status()
    except Exception as e:
        print(f"Download proxy error: {e}")
        return "Could not download the file. Please try again.", 502
    content_type = upstream.headers.get("Content-Type") or "application/octet-stream"
    return Response(upstream.iter_content(chunk_size=8192), content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"', "Cache-Control": "no-store"})

@app.route("/api/download/fetch", methods=["POST"])
@login_required
def download_fetch():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Please paste a TikTok video link."}), 400
    if "tiktok.com" not in url and "vm.tiktok" not in url:
        return jsonify({"error": "That doesn't look like a TikTok URL."}), 400
    if not RAPIDAPI_KEY:
        return jsonify({"error": "Downloader API key is not configured yet."}), 500

    api_url = "https://tiktok-video-no-watermark2.p.rapidapi.com/"
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": "tiktok-video-no-watermark2.p.rapidapi.com"}
    params = {"url": url, "hd": "1"}
    try:
        res = http_session.get(api_url, headers=headers, params=params, timeout=20)
        result = res.json()
    except Exception as e:
        print(f"RapidAPI downloader error: {e}")
        return jsonify({"error": "Could not reach the downloader service."}), 502

    if result.get("code") != 0 or not result.get("data"):
        msg = result.get("msg") or "Could not fetch this video."
        return jsonify({"error": msg}), 400

    vid = result["data"]
    user_id = session["user_id"]
    pro = is_web_pro(user_id)
    remaining = downloader_uses_remaining(user_id)
    if not pro and remaining <= 0:
        return jsonify({"error": "You have used your 3 free TikTok downloads today. Upgrade to Premium for unlimited downloads."}), 429

    return jsonify({
        "ok": True,
        "title": vid.get("title", "TikTok Video"),
        "author": vid.get("author", {}).get("nickname", ""),
        "cover": vid.get("cover", ""),
        "duration": vid.get("duration", 0),
        "play_url": vid.get("play", ""),
        "wmplay_url": vid.get("wmplay", ""),
        "music_url": vid.get("music", ""),
        "is_pro": pro,
        "uses_remaining": remaining,
        "unlimited": pro
    })

@app.route("/api/download/confirm", methods=["POST"])
@login_required
def download_confirm():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "TikTok Video")[:400]
    user_id = session["user_id"]
    pro = is_web_pro(user_id)
    if not url:
        return jsonify({"error": "Missing TikTok URL."}), 400
    if not pro and downloader_uses_today(user_id) >= DOWNLOADER_FREE_LIMIT:
        return jsonify({"error": "You have used your 3 free TikTok downloads today."}), 429
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO web_downloads (user_id, tiktok_url, video_title, plan, ad_watched)
                VALUES (%s, %s, %s, %s, %s)""",
                (user_id, url, title, "pro" if pro else "free", False))
        conn.commit()
    finally:
        release_db(conn)
    return jsonify({"ok": True, "uses_remaining": downloader_uses_remaining(user_id), "unlimited": pro})

# ========================= PAYMENT VERIFICATION =========================
def verify_paystack_reference(reference):
    if not PAYSTACK_SECRET_KEY:
        return False, "Paystack secret key missing"
    if not reference:
        return False, "Missing payment reference"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get(f"https://api.paystack.co/transaction/verify/{reference}",
                               headers=headers, timeout=20)
        data = res.json()
    except Exception as e:
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
        return redirect("/dashboard?payment=success")
    return f"Payment verification failed: {message}", 400

@app.route("/api/payment-status")
@login_required
def payment_status():
    return jsonify({"premium": is_web_pro(session["user_id"])})

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
<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap' rel='stylesheet'>
<style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:Inter,system-ui,sans-serif;background:radial-gradient(circle at 20% 0,#18345a 0,#08111e 34%,#05070c 100%);color:#f8fbff;display:grid;place-items:center;padding:18px}.login{width:min(440px,100%);background:rgba(10,18,32,.82);border:1px solid rgba(125,167,255,.22);box-shadow:0 30px 90px rgba(0,0,0,.42);border-radius:28px;padding:26px;backdrop-filter:blur(16px)}.brand{display:flex;align-items:center;gap:10px;font-weight:900;font-size:24px;letter-spacing:-.04em}.mark{width:38px;height:38px;border-radius:14px;background:linear-gradient(135deg,#22d3ee,#10b981,#f59e0b);display:grid;place-items:center;color:#061018;font-weight:900}.muted{color:#98a9c4;line-height:1.6;margin:8px 0 22px}label{font-size:13px;color:#b8c7dd;font-weight:700;display:block;margin:14px 0 7px}input{width:100%;padding:15px 16px;border-radius:16px;border:1px solid #263852;background:#070d16;color:#fff;font:600 16px Inter;outline:none}input:focus{border-color:#38bdf8;box-shadow:0 0 0 4px rgba(56,189,248,.10)}button{width:100%;margin-top:18px;border:0;border-radius:16px;padding:15px;background:linear-gradient(135deg,#22d3ee,#10b981,#f6b21a);font-weight:900;color:#061018;font-size:16px}.err{display:%ERRDISPLAY%;margin-top:14px;color:#fecdd3;background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.3);padding:12px;border-radius:14px;font-weight:700}.foot{font-size:12px;color:#77859a;margin-top:16px;text-align:center}
</style></head><body><form class='login' method='post'><div class='brand'><div class='mark'>TG</div><div>TikGenius Admin</div></div><p class='muted'>Private dashboard.</p><label>Admin email</label><input name='email' type='email' required><label>Password</label><input name='password' type='password' required><button>Unlock Dashboard</button><div class='err'>%ERROR%</div></form></body></html>"""

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
            cur.execute("""SELECT COUNT(*) AS views, COUNT(DISTINCT ip_hash) AS visitors,
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
                FROM web_payments p LEFT JOIN web_users w ON w.id=p.user_id
                ORDER BY p.paid_at DESC LIMIT 20""")
            payments = cur.fetchall()
            cur.execute("""SELECT id, COALESCE(name, '') AS name, email, plan, expires, region, usage_count, created_at, last_login_at
                FROM web_users ORDER BY created_at DESC LIMIT 300""")
            users = cur.fetchall()
            cur.execute("""SELECT COUNT(*) AS total_downloads,
                COUNT(*) FILTER (WHERE plan='pro') AS pro_downloads,
                COUNT(*) FILTER (WHERE plan='free') AS free_downloads,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS downloads_24h
                FROM web_downloads""")
            dl_stats = cur.fetchone()
            # Referral stats
            cur.execute("SELECT COUNT(*) AS total FROM referrals")
            total_refs = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(*) AS paid FROM referrals WHERE status='paid'")
            paid_refs = cur.fetchone()["paid"]
            cur.execute("SELECT COALESCE(SUM(balance_kobo),0) AS pending_kobo FROM wallets")
            pending_payout = cur.fetchone()["pending_kobo"]
            cur.execute("""SELECT wd.requested_at, wd.amount_kobo, wd.account_name, wd.account_number,
                wd.status, wu.email
                FROM withdrawals wd JOIN web_users wu ON wu.id=wd.user_id
                ORDER BY wd.requested_at DESC LIMIT 20""")
            withdrawals = cur.fetchall()
    finally:
        release_db(conn)

    conversion = round((premium_users / total_users * 100), 1) if total_users else 0

    payment_rows = "".join(
        f"<tr><td>{escape(str(p['paid_at'] or ''))}</td><td>{escape(p['email'] or '')}</td><td>{money_ngn(p['amount_kobo'])}</td><td>{escape(p['source'] or '')}</td><td class='ref'>{escape(p['reference'] or '')}</td></tr>"
        for p in payments
    ) or "<tr><td colspan='5' class='muted'>No payments yet.</td></tr>"

    user_rows = "".join(
        f"<tr><td>{u['id']}</td><td>{escape(u['name'] or '')}</td><td>{escape(u['email'])}</td><td><span class='pill {('pro' if u['plan']=='pro' and u['expires'] else 'free')}'>{escape(u['plan'] or 'free')}</span></td><td>{escape(str(u['expires'] or ''))}</td><td>{escape(u['region'] or '')}</td><td>{u['usage_count'] or 0}</td><td>{escape(str(u['created_at'] or ''))}</td><td>{escape(str(u['last_login_at'] or ''))}</td></tr>"
        for u in users
    ) or "<tr><td colspan='9' class='muted'>No users yet.</td></tr>"

    withdrawal_rows = "".join(
        f"<tr><td>{escape(str(w['requested_at'] or ''))}</td><td>{escape(w['email'] or '')}</td><td>{escape(w['account_name'] or '')}</td><td>{escape(w['account_number'] or '')}</td><td>{money_ngn(w['amount_kobo'])}</td><td><span class='pill {w['status']}'>{escape(w['status'] or '')}</span></td></tr>"
        for w in withdrawals
    ) or "<tr><td colspan='6' class='muted'>No withdrawals yet.</td></tr>"

    top_page_rows = "".join(f"<tr><td>{escape(r['path'] or '')}</td><td>{r['views']}</td><td>{r['visitors']}</td></tr>" for r in top_pages)
    top_click_rows = "".join(f"<tr><td>{escape(r['label'] or '')}</td><td>{escape(r['element'] or '')}</td><td>{r['clicks']}</td></tr>" for r in top_clicks)

    return f"""<!doctype html>
<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>TikGenius Admin</title>
<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800;900&display=swap' rel='stylesheet'>
<style>
:root{{--bg:#060a12;--panel:#0d1525;--line:#243550;--text:#f3f7ff;--muted:#93a4bd;--cyan:#38bdf8;--green:#10b981;--gold:#f6b21a;--red:#fb7185}}
*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,system-ui,sans-serif;background:radial-gradient(circle at top left,#172b52 0,#081120 34%,#05070c 100%);color:var(--text);min-height:100vh}}.wrap{{max-width:1280px;margin:auto;padding:18px}}.hero{{background:linear-gradient(135deg,rgba(56,189,248,.14),rgba(16,185,129,.10));border:1px solid rgba(125,167,255,.22);border-radius:26px;padding:18px;margin-bottom:14px}}.top{{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;flex-wrap:wrap}}.brand{{display:flex;gap:12px;align-items:center}}.mark{{width:42px;height:42px;border-radius:15px;background:linear-gradient(135deg,var(--cyan),var(--green),var(--gold));display:grid;place-items:center;color:#061018;font-weight:900}}h1{{font-size:clamp(1.35rem,5vw,2.15rem);letter-spacing:-.055em;margin:0}}.muted{{color:var(--muted);font-size:.92rem}}.logout{{font-size:.84rem;color:#dbeafe;text-decoration:none;border:1px solid rgba(148,163,184,.25);padding:9px 12px;border-radius:999px;background:rgba(8,13,23,.56)}}.hero-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}}.mini{{padding:12px;border-radius:18px;background:rgba(5,10,18,.55);border:1px solid rgba(148,163,184,.17)}}.mini b{{display:block;font-size:1.08rem}}.mini span{{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:800}}.grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0}}.card{{background:linear-gradient(180deg,rgba(17,28,49,.92),rgba(10,17,30,.96));border:1px solid rgba(90,119,164,.45);border-radius:22px;padding:17px}}.label{{color:#a5b4fc;font-size:.74rem;text-transform:uppercase;letter-spacing:.11em;font-weight:900}}.num{{font-size:clamp(1.7rem,7vw,2.35rem);font-weight:900;letter-spacing:-.05em;margin-top:8px}}.section{{margin-top:14px}}h2{{margin:0 0 12px;font-size:1.05rem}}.tablebox{{overflow:auto;border-radius:18px;border:1px solid rgba(90,119,164,.38)}}table{{width:100%;border-collapse:collapse;min-width:700px;background:#0b1322}}th,td{{padding:12px 13px;border-bottom:1px solid #1e293b;text-align:left;font-size:.86rem;white-space:nowrap}}th{{color:#bfdbfe;background:#101b30;font-size:.72rem;text-transform:uppercase;letter-spacing:.075em}}.ref{{max-width:180px;overflow:hidden;text-overflow:ellipsis}}.pill{{padding:5px 9px;border-radius:999px;font-weight:900;font-size:.72rem}}.pill.pro,.pill.success{{background:rgba(16,185,129,.16);color:#6ee7b7;border:1px solid rgba(16,185,129,.32)}}.pill.free,.pill.pending{{background:rgba(99,102,241,.16);color:#c4b5fd;border:1px solid rgba(99,102,241,.32)}}.pill.failed{{background:rgba(251,113,133,.16);color:#fda4af;border:1px solid rgba(251,113,133,.32)}}.search{{width:100%;padding:13px 14px;border-radius:14px;border:1px solid #334155;background:#07101d;color:white;margin:4px 0 14px;outline:none}}.download-zone{{margin:18px 0 30px;padding:16px;border-radius:22px;border:1px dashed rgba(148,163,184,.35);background:rgba(8,13,23,.45)}}.download-row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}}a.smallbtn{{background:#17243a;color:#dbeafe;text-decoration:none;padding:8px 10px;border-radius:10px;font-weight:800;font-size:.78rem;display:inline-flex;gap:6px;align-items:center;border:1px solid rgba(148,163,184,.24)}}
</style></head>
<body><div class='wrap'>
<section class='hero'><div class='top'><div class='brand'><div class='mark'>TG</div><div><h1>TikGenius Admin</h1><div class='muted'>Revenue, users, referrals, withdrawals.</div></div></div><a class='logout' href='/admin/logout'>Log out</a></div>
<div class='hero-stats'><div class='mini'><span>Revenue</span><b>{money_ngn(pay_stats['revenue'])}</b></div><div class='mini'><span>Conversion</span><b>{conversion}%</b></div><div class='mini'><span>Today signups</span><b>{today_signups}</b></div></div></section>
<div class='grid'>
  <div class='card'><div class='label'>Total Revenue</div><div class='num'>{money_ngn(pay_stats['revenue'])}</div><div class='muted'>{pay_stats['count']} payments</div></div>
  <div class='card'><div class='label'>Premium Users</div><div class='num'>{premium_users}</div></div>
  <div class='card'><div class='label'>Free Users</div><div class='num'>{free_users}</div></div>
  <div class='card'><div class='label'>Total Signups</div><div class='num'>{total_users}</div><div class='muted'>{today_signups} today</div></div>
  <div class='card'><div class='label'>Generations</div><div class='num'>{total_generations}</div></div>
  <div class='card'><div class='label'>Total Referrals</div><div class='num'>{total_refs}</div><div class='muted'>{paid_refs} converted</div></div>
  <div class='card'><div class='label'>Pending Payouts</div><div class='num'>{money_ngn(pending_payout)}</div><div class='muted'>In user wallets</div></div>
  <div class='card'><div class='label'>Page Views</div><div class='num'>{traffic_stats['views'] or 0}</div><div class='muted'>{traffic_stats['views_24h'] or 0} today</div></div>
  <div class='card'><div class='label'>Downloads</div><div class='num'>{dl_stats['total_downloads'] or 0}</div><div class='muted'>{dl_stats['downloads_24h'] or 0} today</div></div>
</div>
<div class='section card'><h2>Recent Payments</h2><div class='tablebox'><table><thead><tr><th>Date</th><th>Email</th><th>Amount</th><th>Source</th><th>Reference</th></tr></thead><tbody>{payment_rows}</tbody></table></div></div>
<div class='section card'><h2>Withdrawals</h2><div class='tablebox'><table><thead><tr><th>Date</th><th>Email</th><th>Account Name</th><th>Account No.</th><th>Amount</th><th>Status</th></tr></thead><tbody>{withdrawal_rows}</tbody></table></div></div>
<div class='section card'><h2>Page Analytics</h2><div class='tablebox'><table><thead><tr><th>Page</th><th>Views</th><th>Visitors</th></tr></thead><tbody>{top_page_rows}</tbody></table></div></div>
<div class='section card'><h2>Click Tracking</h2><div class='tablebox'><table><thead><tr><th>Label</th><th>Element</th><th>Clicks</th></tr></thead><tbody>{top_click_rows}</tbody></table></div></div>
<div class='section card'><h2>All Users</h2><input class='search' id='search' placeholder='Search email, name, plan...' onkeyup='filterRows()'><div class='tablebox'><table id='users'><thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Plan</th><th>Expires</th><th>Region</th><th>Uses</th><th>Signup</th><th>Last Login</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
<div class='download-zone'><div class='label'>Exports</div><div class='download-row'><a class='smallbtn' href='/admin/emails.csv'>⬇ Emails CSV</a><a class='smallbtn' href='/admin/payments.csv'>⬇ Payments CSV</a></div></div>
<div class='section card' style='margin-bottom:20px'><h2>⚡ Admin Actions</h2>
<div style='display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end'>
  <div><div style='font-size:.72rem;color:#94a3b8;margin-bottom:5px;font-weight:700'>GRANT PREMIUM</div>
    <div style='display:flex;gap:6px'><input id='grantEmail' class='search' placeholder='user@email.com' style='margin:0;width:220px;padding:9px 12px'>
    <input id='grantDays' class='search' placeholder='Days (30)' style='margin:0;width:90px;padding:9px 12px'>
    <button onclick='grantPremium()' style='background:#10b981;color:#fff;border:none;border-radius:10px;padding:9px 14px;font-weight:800;cursor:pointer;white-space:nowrap'>Grant</button>
    <button onclick='revokePremium()' style='background:#f43f5e;color:#fff;border:none;border-radius:10px;padding:9px 14px;font-weight:800;cursor:pointer;white-space:nowrap'>Revoke</button></div>
  <div id='grantMsg' style='font-size:.8rem;margin-top:6px;color:#6ee7b7'></div></div>
</div></div>
</div><script>
function filterRows(){{let q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('#users tbody tr').forEach(r=>{{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'}})}}
async function grantPremium(){{
  const email=document.getElementById('grantEmail').value.trim();
  const days=parseInt(document.getElementById('grantDays').value)||30;
  const msg=document.getElementById('grantMsg');
  if(!email){{msg.style.color='#fb7185';msg.textContent='Enter an email first.';return;}}
  const res=await fetch('/api/admin/grant-premium',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,days}})}});
  const d=await res.json();
  if(d.error){{msg.style.color='#fb7185';msg.textContent=d.error;}}
  else{{msg.style.color='#6ee7b7';msg.textContent='✓ Premium granted to '+d.email+' until '+d.expires;setTimeout(()=>location.reload(),1500);}}
}}
async function revokePremium(){{
  const email=document.getElementById('grantEmail').value.trim();
  const msg=document.getElementById('grantMsg');
  if(!email){{msg.style.color='#fb7185';msg.textContent='Enter an email first.';return;}}
  if(!confirm('Revoke premium from '+email+'?'))return;
  const res=await fetch('/api/admin/revoke-premium',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email}})}});
  const d=await res.json();
  if(d.error){{msg.style.color='#fb7185';msg.textContent=d.error;}}
  else{{msg.style.color='#fb7185';msg.textContent='✓ Premium revoked from '+d.email;setTimeout(()=>location.reload(),1500);}}
}}
</script></body></html>"""

@app.route("/admin/emails.csv")
@admin_login_required
def admin_emails_csv():
    import csv, io
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, COALESCE(name,'') AS name, email, plan, expires, region, usage_count, created_at, last_login_at FROM web_users ORDER BY created_at DESC")
            users = cur.fetchall()
    finally:
        release_db(conn)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","name","email","plan","expires","region","usage_count","created_at","last_login_at"])
    for u in users:
        writer.writerow([u["id"],u["name"],u["email"],u["plan"],u["expires"] or "",u["region"],u["usage_count"] or 0,u["created_at"],u["last_login_at"] or ""])
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
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["reference","email","amount_naira","currency","status","source","paid_at"])
    for p in payments:
        writer.writerow([p["reference"],p["email"],int(p["amount_kobo"] or 0)/100,p["currency"],p["status"],p["source"],p["paid_at"]])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tikgenius_payments.csv"})

@app.route("/api/admin/audience-count")
def admin_audience_count():
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM web_users")
            return jsonify({"total": cur.fetchone()["total"]})
    finally:
        release_db(conn)

@app.route("/api/admin/grant-premium", methods=["POST"])
def admin_grant_premium():
    """Manually grant premium to a user by email."""
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    days = int(data.get("days", 30))
    if not email:
        return jsonify({"error": "Email required"}), 400
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM web_users WHERE email=%s", (email,))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404
        expires = (datetime.utcnow() + timedelta(days=days)).date()
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET plan='pro', expires=%s WHERE email=%s", (expires, email))
        conn.commit()
        return jsonify({"success": True, "email": email, "expires": expires.strftime("%Y-%m-%d"), "days": days})
    finally:
        release_db(conn)

@app.route("/api/admin/revoke-premium", methods=["POST"])
def admin_revoke_premium():
    """Revoke premium from a user by email."""
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE web_users SET plan='free', expires=NULL WHERE email=%s", (email,))
        conn.commit()
        return jsonify({"success": True, "email": email})
    finally:
        release_db(conn)

@app.route("/api/admin/user-stats")
def admin_user_stats():
    """Quick stats for a single user by email."""
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, plan, expires, region, usage_count, created_at, last_login_at FROM web_users WHERE email=%s", (email,))
            user = cur.fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS gens FROM web_generations WHERE user_id=%s", (user["id"],))
            gens = cur.fetchone()["gens"]
            cur.execute("SELECT balance_kobo, total_earned_kobo FROM wallets WHERE user_id=%s", (user["id"],))
            wallet = cur.fetchone()
        return jsonify({
            "user": dict(user),
            "total_generations": gens,
            "wallet_balance_ngn": int(wallet["balance_kobo"] if wallet else 0) / 100,
            "total_earned_ngn": int(wallet["total_earned_kobo"] if wallet else 0) / 100,
        })
    finally:
        release_db(conn)

# ========================= TELEGRAM BOT =========================
def send_telegram_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    return jsonify({"ok": True, "message": "Telegram bot disabled. Use the website."})

# ========================= HTML PAGES =========================

HOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius — Go Viral. In Your Voice.</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#03050a;--surface:#07111c;--card:#0b1928;--border:#14253a;--accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--text:#f0f8ff;--muted:#607a90}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;min-height:100vh;overflow-x:hidden;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:.85rem 1.4rem;position:sticky;top:0;z-index:100;background:rgba(3,5,10,.85);backdrop-filter:blur(18px);border-bottom:1px solid rgba(0,255,200,.07)}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:1.25rem;letter-spacing:-.03em;display:flex;align-items:center;gap:.5rem;text-decoration:none;color:var(--text)}
.logo em{color:var(--accent);font-style:normal}
.nav-btns{display:flex;gap:.65rem;align-items:center}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--muted);padding:.45rem 1.1rem;border-radius:8px;font-size:.875rem;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .2s;text-decoration:none;display:inline-block}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent)}
.btn-cta{background:var(--accent);border:none;color:#030e0a;padding:.5rem 1.2rem;border-radius:8px;cursor:pointer;font-family:'DM Sans',sans-serif;font-size:.875rem;font-weight:700;transition:all .2s;text-decoration:none;display:inline-block}
.btn-cta:hover{transform:translateY(-1px);box-shadow:0 0 24px rgba(0,255,200,.35)}
.hero-wrap{position:relative;padding:6rem 1.5rem 4rem;text-align:center;overflow:hidden}
.hero-glow{position:absolute;top:-180px;left:50%;transform:translateX(-50%);width:700px;height:700px;background:radial-gradient(circle,rgba(0,255,200,.09) 0,transparent 65%);pointer-events:none}
.hero-badge{display:inline-flex;align-items:center;gap:.45rem;background:rgba(0,255,200,.08);border:1px solid rgba(0,255,200,.2);color:var(--accent);padding:.35rem 1rem;border-radius:100px;font-size:.8rem;font-weight:600;margin-bottom:2.2rem;letter-spacing:.04em;text-transform:uppercase}
.hero-badge::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--accent);display:inline-block;animation:blink 1.6s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
h1.hero-title{font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(2.4rem,9vw,5rem);line-height:.98;letter-spacing:-.045em;margin-bottom:1.5rem;max-width:820px;margin-left:auto;margin-right:auto}
h1.hero-title .hl{display:inline-block;background:linear-gradient(130deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.hero-sub{color:var(--muted);font-size:1.1rem;line-height:1.7;max-width:520px;margin:0 auto 2.8rem}
.hero-btns{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn-hero{padding:.9rem 2.2rem;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .25s;text-decoration:none;display:inline-flex;align-items:center;gap:.5rem}
.btn-hero.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#030e0a;border:none}
.btn-hero.primary:hover{transform:translateY(-2px);box-shadow:0 8px 36px rgba(0,255,200,.3)}
.btn-hero.outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-hero.outline:hover{border-color:var(--accent);color:var(--accent)}
.hero-note{margin-top:1.6rem;color:var(--muted);font-size:.82rem}
.hero-note span{color:var(--accent)}
.ticker-wrap{overflow:hidden;border-top:1px solid var(--border);border-bottom:1px solid var(--border);background:rgba(7,17,28,.6);padding:.65rem 0;margin:2rem 0}
.ticker{display:flex;gap:2.5rem;animation:tick 28s linear infinite;white-space:nowrap}
.ticker span{color:var(--muted);font-size:.8rem;letter-spacing:.06em;text-transform:uppercase}
.ticker strong{color:var(--accent);font-weight:700}
@keyframes tick{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.section{padding:5rem 1.5rem;max-width:1150px;margin:0 auto}
.section-tag{display:block;text-align:center;color:var(--accent);font-size:.75rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:.9rem}
.section-title{text-align:center;font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(1.7rem,4vw,2.8rem);letter-spacing:-.04em;margin-bottom:.8rem}
.section-sub{text-align:center;color:var(--muted);max-width:480px;margin:0 auto 3rem;line-height:1.65}

/* EARN BAND */
.earn-band{background:linear-gradient(135deg,rgba(255,184,0,.1),rgba(0,255,200,.07));border:1px solid rgba(255,184,0,.25);border-radius:24px;padding:2rem;margin:2rem 1.5rem;max-width:1000px;margin-left:auto;margin-right:auto;display:flex;align-items:center;justify-content:space-between;gap:1.5rem;flex-wrap:wrap}
.earn-band-text strong{display:block;font-family:'Syne',sans-serif;font-weight:800;font-size:1.3rem;color:var(--gold);margin-bottom:.4rem}
.earn-band-text p{color:var(--muted);font-size:.9rem;line-height:1.6}
.earn-btn{background:linear-gradient(135deg,var(--gold),#ff8c00);color:#030e0a;border:none;border-radius:12px;padding:13px 24px;font-size:.95rem;font-weight:800;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;text-decoration:none;transition:all .2s}
.earn-btn:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(255,184,0,.35)}

.examples-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.25rem}
.ex-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:1.4rem;transition:border-color .2s,transform .2s}
.ex-card:hover{border-color:rgba(0,255,200,.3);transform:translateY(-3px)}
.ex-tag{display:inline-block;background:rgba(0,255,200,.1);color:var(--accent);padding:.2rem .7rem;border-radius:100px;font-size:.72rem;font-weight:600;margin-bottom:.9rem;letter-spacing:.05em;text-transform:uppercase}
.ex-prompt{color:var(--muted);font-size:.82rem;margin-bottom:.85rem;font-style:italic}
.ex-output{color:var(--text);font-size:.9rem;line-height:1.65}
.ex-output p{margin-bottom:.45rem;padding-left:.7rem;border-left:2px solid var(--accent)}
.steps-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:1.5rem;margin-top:3rem}
.step{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:1.6rem}
.step-num{font-family:'Syne',sans-serif;font-weight:800;font-size:2.2rem;color:rgba(0,255,200,.2);line-height:1;margin-bottom:.8rem}
.step h3{font-family:'Syne',sans-serif;font-size:1.05rem;font-weight:700;margin-bottom:.5rem}
.step p{color:var(--muted);font-size:.875rem;line-height:1.6}
.features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.25rem}
.feat{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:1.5rem;display:flex;gap:1rem;align-items:flex-start;transition:border-color .2s}
.feat:hover{border-color:rgba(0,255,200,.25)}
.feat-icon{font-size:1.6rem;flex-shrink:0}
.feat-body h3{font-family:'Syne',sans-serif;font-size:.95rem;font-weight:700;margin-bottom:.35rem}
.feat-body p{color:var(--muted);font-size:.83rem;line-height:1.55}
.pricing-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.5rem;max-width:720px;margin:3rem auto 0}
.plan{background:var(--card);border:1px solid var(--border);border-radius:22px;padding:2rem;position:relative}
.plan.pro{border-color:rgba(0,255,200,.4);background:linear-gradient(145deg,#0a1f2b,#0b1928)}
.plan-badge{position:absolute;top:-13px;left:50%;transform:translateX(-50%);background:var(--accent);color:#030e0a;font-size:.7rem;font-weight:800;padding:.25rem .85rem;border-radius:100px;letter-spacing:.06em;text-transform:uppercase;white-space:nowrap}
.plan h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1.2rem;margin-bottom:.3rem}
.plan .price{font-family:'Syne',sans-serif;font-size:2.5rem;font-weight:800;line-height:1;margin:.8rem 0 .3rem}
.plan .price span{font-size:1rem;color:var(--muted);font-family:'DM Sans',sans-serif;font-weight:400}
.plan .pdesc{color:var(--muted);font-size:.85rem;margin-bottom:1.4rem;line-height:1.5}
.plan ul{list-style:none;display:grid;gap:.55rem;margin-bottom:1.6rem}
.plan ul li{font-size:.875rem;color:var(--text);display:flex;align-items:center;gap:.5rem}
.plan ul li::before{content:'✓';color:var(--accent);font-weight:700;font-size:.8rem;flex-shrink:0}
.plan-btn{width:100%;padding:.8rem;border-radius:10px;font-size:.925rem;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .2s;border:none}
.plan-btn.free{background:transparent;border:1px solid var(--border);color:var(--muted)}
.plan-btn.free:hover{border-color:var(--accent);color:var(--accent)}
.plan-btn.prm{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#030e0a}
.plan-btn.prm:hover{transform:translateY(-1px);box-shadow:0 6px 28px rgba(0,255,200,.3)}
.cta-band{background:linear-gradient(135deg,rgba(0,255,200,.08),rgba(0,170,255,.05));border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:4rem 1.5rem;text-align:center;margin:4rem 0 0}
.cta-band h2{font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(1.8rem,5vw,3rem);letter-spacing:-.04em;margin-bottom:.8rem}
.cta-band p{color:var(--muted);margin-bottom:2rem;line-height:1.6}
footer{padding:2rem 1.5rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:1rem;border-top:1px solid var(--border);max-width:1150px;margin:0 auto}
footer .brand{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem}
footer .brand em{color:var(--accent);font-style:normal}
footer .links{display:flex;gap:1.5rem}
footer .links a{color:var(--muted);font-size:.8rem;text-decoration:none;transition:color .2s}
footer .links a:hover{color:var(--accent)}
footer .copy{color:var(--muted);font-size:.75rem}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
.modal-overlay.active{display:flex}
.modal{background:#0b1928;border:1px solid rgba(0,255,200,.2);border-radius:22px;padding:2rem;width:100%;max-width:420px;margin:1rem}
.modal h2{font-family:'Syne',sans-serif;font-weight:800;font-size:1.4rem;margin-bottom:.35rem}
.modal p{color:var(--muted);font-size:.875rem;margin-bottom:1.5rem;line-height:1.5}
.modal-tabs{display:flex;background:#071018;border-radius:10px;padding:3px;gap:3px;margin-bottom:1.4rem}
.modal-tab{flex:1;padding:.55rem;border:none;border-radius:8px;background:transparent;color:var(--muted);font-size:.875rem;font-weight:600;cursor:pointer;transition:all .2s;font-family:'DM Sans',sans-serif}
.modal-tab.active{background:#14253a;color:var(--text)}
.form-group{margin-bottom:1rem}
.form-group label{display:block;font-size:.8rem;font-weight:600;color:var(--muted);margin-bottom:.45rem;letter-spacing:.04em;text-transform:uppercase}
.form-group input,.form-group select{width:100%;background:#071018;border:1px solid var(--border);color:var(--text);padding:.75rem 1rem;border-radius:10px;font-size:.9rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(0,255,200,.08)}
.form-error{display:none;color:#fb7185;font-size:.82rem;margin-bottom:.85rem;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:.6rem .85rem;border-radius:8px}
.modal-btn{width:100%;padding:.85rem;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:10px;color:#030e0a;font-size:.95rem;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .2s;margin-bottom:.75rem}
.modal-btn:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(0,255,200,.25)}
.modal-cancel{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.83rem;font-family:'DM Sans',sans-serif;width:100%;padding:.4rem}
@media(max-width:600px){nav{padding:.7rem 1rem}.hero-wrap{padding:4rem 1rem 3rem}.section{padding:3.5rem 1rem}footer{flex-direction:column;text-align:center}footer .links{justify-content:center}.earn-band{flex-direction:column;text-align:center}}
</style>
</head>
<body>
<nav>
  <a class="logo" href="/"><svg width="26" height="26" viewBox="0 0 200 200" fill="none"><defs><linearGradient id="hG1" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00ffc8"/><stop offset="1" stop-color="rgba(0,255,200,.7)"/></linearGradient><linearGradient id="hG2" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00aaff"/><stop offset="1" stop-color="#00ffc8"/></linearGradient></defs><rect x="52" y="58" width="52" height="7" rx="2" fill="url(#hG1)"/><rect x="74" y="65" width="8" height="70" rx="2" fill="url(#hG1)"/><path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#hG2)" stroke-width="7" fill="none" stroke-linecap="round"/><rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#hG2)"/></svg>Tik<em>Genius</em></a>
  <div class="nav-btns">
    <a class="btn-ghost" href="/download">⬇ Downloader</a>
    <a class="btn-ghost" href="#" onclick="openModal('login');return false">Log In</a>
    <a class="btn-cta" href="#" onclick="openModal('signup');return false">Start Free →</a>
  </div>
</nav>
<div class="hero-wrap">
  <div class="hero-glow"></div>
  <div class="hero-badge">✦ AI-powered for TikTok &amp; X creators</div>
  <h1 class="hero-title">Go Viral.<br><span class="hl">In Your Voice.</span></h1>
  <p class="hero-sub">Generate captions, hooks, POVs, scripts, hashtags and X threads that actually perform — tuned to your culture and niche.</p>
  <div class="hero-btns">
    <a class="btn-hero primary" href="#" onclick="openModal('signup');return false">✦ Start for Free</a>
    <a class="btn-hero outline" href="/download">⬇ Download TikToks</a>
  </div>
  <p class="hero-note">Free forever · No credit card · <span>5 generations/day free</span></p>
</div>
<div class="ticker-wrap"><div class="ticker"><span>Viral Captions</span><strong>·</strong><span>Scroll-Stopping Hooks</span><strong>·</strong><span>POV Concepts</span><strong>·</strong><span>Full Video Scripts</span><strong>·</strong><span>Hashtag Sets</span><strong>·</strong><span>X Threads</span><strong>·</strong><span>Earn ₦500/Referral</span><strong>·</strong><span>7 Regions Supported</span><strong>·</strong><span>Viral Captions</span><strong>·</strong><span>Scroll-Stopping Hooks</span><strong>·</strong><span>POV Concepts</span><strong>·</strong><span>Full Video Scripts</span><strong>·</strong><span>Hashtag Sets</span><strong>·</strong><span>X Threads</span><strong>·</strong><span>Earn ₦500/Referral</span><strong>·</strong><span>7 Regions Supported</span><strong>·</strong></div></div>

<!-- EARN BAND -->
<div class="earn-band">
  <div class="earn-band-text">
    <strong>💰 Earn ₦500 for every person you refer</strong>
    <p>Share your unique link. When someone upgrades to Premium through your link, you earn ₦500 — automatically paid to your bank account. No limit on how many you refer.</p>
  </div>
  <a class="earn-btn" href="#" onclick="openModal('signup');return false">Start Earning →</a>
</div>

<div class="section">
  <span class="section-tag">Real outputs</span>
  <h2 class="section-title">Content that hits different</h2>
  <p class="section-sub">Every output is crafted for your niche, audience, and culture — not generic AI filler.</p>
  <div class="examples-grid">
    <div class="ex-card"><div class="ex-tag">TikTok Captions</div><div class="ex-prompt">Topic: "soft life" hustle balance for Nigerian women</div><div class="ex-output"><p>Nobody warned me that ambition and peace could coexist.</p><p>I chose both. I don't apologise for either.</p><p>Soft life isn't lazy. It's strategic.</p></div></div>
    <div class="ex-card"><div class="ex-tag">Viral Hooks</div><div class="ex-prompt">Topic: building a brand as a broke 22-year-old</div><div class="ex-output"><p>I had ₦4,000 and a borrowed laptop. Here's what happened in 90 days.</p><p>The algorithm doesn't care about your budget. It cares about your story.</p><p>Nobody tells you the first 100 posts feel like screaming into a void.</p></div></div>
    <div class="ex-card"><div class="ex-tag">X Thread</div><div class="ex-prompt">Topic: why consistency beats talent on TikTok</div><div class="ex-output"><p>Talented creators quit every week. Consistent ones get rich. Here's the math:</p><p>Post 365 times before judging your growth. Most people quit at day 12.</p><p>Every "viral" creator you envy has 300 forgotten videos you never saw.</p></div></div>
  </div>
</div>
<div class="section" style="padding-top:1rem">
  <span class="section-tag">How it works</span>
  <h2 class="section-title">Three steps to viral</h2>
  <div class="steps-grid">
    <div class="step"><div class="step-num">01</div><h3>Choose your mode</h3><p>Pick from captions, hooks, POVs, scripts, hashtags, X threads.</p></div>
    <div class="step"><div class="step-num">02</div><h3>Describe your topic</h3><p>Tell TikGenius your niche, audience, emotion and goal. More specific = better output.</p></div>
    <div class="step"><div class="step-num">03</div><h3>Copy and post</h3><p>Get 5–10 ready-to-post outputs. Pick the best one and watch the views come in.</p></div>
  </div>
</div>
<div class="section" style="padding-top:1rem">
  <span class="section-tag">Features</span>
  <h2 class="section-title">Everything a creator needs</h2>
  <div class="features-grid">
    <div class="feat"><div class="feat-icon">🌍</div><div class="feat-body"><h3>7 Cultural Voices</h3><p>Nigerian, American, British, Caribbean, East African, South African, or Global.</p></div></div>
    <div class="feat"><div class="feat-icon">⚡</div><div class="feat-body"><h3>Instant AI Generation</h3><p>10+ outputs per prompt in under 5 seconds.</p></div></div>
    <div class="feat"><div class="feat-icon">🎬</div><div class="feat-body"><h3>Full Video Scripts</h3><p>Hook, body, punchline and CTA — structured like viral TikToks.</p></div></div>
    <div class="feat"><div class="feat-icon">🐦</div><div class="feat-body"><h3>X / Twitter Tools</h3><p>Viral tweets, thread starters, and full multi-tweet threads.</p></div></div>
    <div class="feat"><div class="feat-icon">📥</div><div class="feat-body"><h3>TikTok Downloader</h3><p>Download any TikTok video in HD, no watermark. Free users get 3/day.</p></div></div>
    <div class="feat"><div class="feat-icon">💰</div><div class="feat-body"><h3>Earn ₦500/Referral</h3><p>Share your link. Every Premium upgrade through your link pays you automatically.</p></div></div>
  </div>
</div>
<div class="section" style="padding-top:1rem">
  <span class="section-tag">Pricing</span>
  <h2 class="section-title">Simple, creator-friendly pricing</h2>
  <p class="section-sub">Start free. Upgrade when you're ready to go all-in.</p>
  <div class="pricing-grid">
    <div class="plan"><h3>Free</h3><div class="price">₦0 <span>/forever</span></div><p class="pdesc">Perfect for creators just starting out.</p><ul><li>5 AI generations per day</li><li>3 TikTok downloads per day</li><li>All 7 content modes</li><li>All 7 cultural regions</li><li>Referral earnings</li></ul><button class="plan-btn free" onclick="openModal('signup')">Get Started Free</button></div>
    <div class="plan pro"><div class="plan-badge">✦ Most Popular</div><h3>Premium</h3><div class="price">₦2,000 <span>/month</span></div><p class="pdesc">For serious creators who post daily.</p><ul><li>Unlimited AI generations</li><li>Unlimited TikTok downloads</li><li>All 7 content modes</li><li>All 7 cultural regions</li><li>Full generation history</li><li>Priority response speed</li></ul><button class="plan-btn prm" onclick="openModal('signup')">Upgrade to Premium →</button></div>
  </div>
</div>
<div class="cta-band">
  <h2>Ready to go viral?</h2>
  <p>Join creators who use TikGenius every day to stay consistent and grow faster.</p>
  <div class="hero-btns">
    <a class="btn-hero primary" href="#" onclick="openModal('signup');return false">✦ Start for Free</a>
    <a class="btn-hero outline" href="/refer">💰 Earn with Referrals</a>
  </div>
</div>
<footer>
  <div class="brand">Tik<em>Genius</em></div>
  <div class="links"><a href="/dashboard">AI Studio</a><a href="/download">Downloader</a><a href="/refer">Earn</a></div>
  <div class="copy">© 2025 TikGenius</div>
</footer>
<div class="modal-overlay" id="authModal">
  <div class="modal">
    <h2 id="modalTitle">Create your account</h2>
    <p id="modalSub">Start generating viral content — 5 free generations per day</p>
    <div class="modal-tabs">
      <button class="modal-tab active" id="signupTab" onclick="switchTab('signup')">Sign Up</button>
      <button class="modal-tab" id="loginTab" onclick="switchTab('login')">Log In</button>
    </div>
    <div id="signupForm">
      <div class="form-group"><label>Email</label><input type="email" id="signupEmail" placeholder="you@example.com"></div>
      <div class="form-group"><label>Password</label><input type="password" id="signupPassword" placeholder="Min 6 characters"></div>
      <div class="form-group"><label>Your Region</label><select id="signupRegion"><option value="global">🌍 Global</option><option value="nigeria">🇳🇬 Nigerian</option><option value="usa">🇺🇸 American</option><option value="uk">🇬🇧 British</option><option value="caribbean">🇯🇲 Caribbean</option><option value="eastafrica">🇰🇪 East African</option><option value="southafrica">🇿🇦 South African</option></select></div>
      <div class="form-error" id="signupError"></div>
      <button class="modal-btn" onclick="doSignup()">Create Account →</button>
    </div>
    <div id="loginForm" style="display:none">
      <div class="form-group"><label>Email</label><input type="email" id="loginEmail" placeholder="you@example.com"></div>
      <div class="form-group"><label>Password</label><input type="password" id="loginPassword" placeholder="Your password"></div>
      <div class="form-error" id="loginError"></div>
      <button class="modal-btn" onclick="doLogin()">Log In →</button>
    </div>
    <button class="modal-cancel" onclick="closeModal()">Cancel</button>
  </div>
</div>
<script>
// Capture ref code from URL
var urlRef=(new URLSearchParams(window.location.search)).get('ref')||'';
function openModal(tab){document.getElementById('authModal').classList.add('active');switchTab(tab||'signup')}
function closeModal(){document.getElementById('authModal').classList.remove('active')}
function switchTab(tab){
  document.getElementById('signupForm').style.display=tab==='signup'?'block':'none';
  document.getElementById('loginForm').style.display=tab==='login'?'block':'none';
  document.getElementById('signupTab').classList.toggle('active',tab==='signup');
  document.getElementById('loginTab').classList.toggle('active',tab==='login');
  document.getElementById('modalTitle').textContent=tab==='signup'?'Create your account':'Welcome back';
  document.getElementById('modalSub').textContent=tab==='signup'?'Start generating viral content — 5 free generations per day':'Log in to your TikGenius account';
}
async function doSignup(){
  var email=document.getElementById('signupEmail').value;
  var password=document.getElementById('signupPassword').value;
  var region=document.getElementById('signupRegion').value;
  var err=document.getElementById('signupError');
  err.style.display='none';
  var res=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,region,ref_code:urlRef})});
  var data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return}
  window.location.href=data.redirect;
}
async function doLogin(){
  var email=document.getElementById('loginEmail').value;
  var password=document.getElementById('loginPassword').value;
  var err=document.getElementById('loginError');
  err.style.display='none';
  var res=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
  var data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return}
  window.location.href=data.redirect;
}
document.getElementById('authModal').addEventListener('click',function(e){if(e.target===this)closeModal()});
</script>
</body>
</html>"""


REFER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius — Earn with Referrals</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#03050a;--card:#0b1928;--border:#14253a;--text:#f0f8ff;--muted:#607a90;--accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--green:#22c55e;--danger:#fb7185}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(ellipse at 60% -10%,rgba(255,184,0,.08),transparent 40%),var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:.85rem 1.4rem;position:sticky;top:0;z-index:100;background:rgba(3,5,10,.9);backdrop-filter:blur(18px);border-bottom:1px solid rgba(0,255,200,.07)}
.logo{display:flex;align-items:center;gap:.5rem;text-decoration:none;color:var(--text);font-family:'Syne',sans-serif;font-weight:800;font-size:1.15rem;letter-spacing:-.03em}
.logo em{color:var(--accent);font-style:normal}
.nav-right{display:flex;gap:.65rem;align-items:center}
.nav-link{text-decoration:none;color:var(--muted);font-size:.875rem;font-weight:600;padding:.4rem .7rem;border-radius:8px;transition:color .2s}
.nav-link:hover{color:var(--accent)}
.btn-nav{background:var(--accent);color:#030e0a;border:none;padding:.45rem 1.1rem;border-radius:8px;font-size:.875rem;font-weight:700;cursor:pointer;text-decoration:none;font-family:'DM Sans',sans-serif;transition:all .2s}
.wrap{max-width:780px;margin:0 auto;padding:2rem 1.25rem 5rem}
.page-hero{text-align:center;padding:3rem 0 2rem}
.page-badge{display:inline-flex;align-items:center;gap:.45rem;background:rgba(255,184,0,.1);border:1px solid rgba(255,184,0,.3);color:var(--gold);padding:.35rem 1rem;border-radius:100px;font-size:.78rem;font-weight:700;margin-bottom:1.5rem;letter-spacing:.05em;text-transform:uppercase}
.page-hero h1{font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(2rem,7vw,3.2rem);letter-spacing:-.045em;margin-bottom:.8rem}
.page-hero h1 em{color:var(--gold);font-style:normal}
.page-hero p{color:var(--muted);font-size:1rem;line-height:1.7;max-width:520px;margin:0 auto}

/* STATS ROW */
.stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:16px;text-align:center}
.stat-card .val{font-family:'Syne',sans-serif;font-weight:800;font-size:1.8rem;letter-spacing:-.04em;margin-bottom:4px}
.stat-card .lbl{font-size:.72rem;color:var(--muted);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.stat-card.highlight{border-color:rgba(255,184,0,.35);background:linear-gradient(135deg,rgba(255,184,0,.08),rgba(0,255,200,.05))}
.stat-card.highlight .val{color:var(--gold)}

/* LINK CARD */
.link-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.link-card h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.5rem}
.link-card p{color:var(--muted);font-size:.82rem;line-height:1.5;margin-bottom:1rem}
.link-box{display:flex;gap:8px}
.link-input{flex:1;background:#050e18;border:1px solid var(--border);color:var(--accent);padding:11px 14px;border-radius:11px;font-size:.82rem;font-family:'DM Sans',sans-serif;outline:none;min-width:0;cursor:text}
.copy-link-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:11px;color:#030e0a;font-weight:800;padding:11px 18px;font-size:.85rem;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
.copy-link-btn:hover{transform:translateY(-1px);box-shadow:0 5px 20px rgba(0,255,200,.3)}
.share-btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.share-btn{background:#07111c;border:1px solid var(--border);color:var(--text);padding:9px 14px;border-radius:10px;font-size:.8rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.4rem;transition:all .2s}
.share-btn:hover{border-color:rgba(0,255,200,.3);color:var(--accent)}

/* HOW IT WORKS */
.how-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.how-card h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin-bottom:1rem}
.how-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.how-step{text-align:center;padding:12px 8px}
.how-step .num{font-family:'Syne',sans-serif;font-size:1.8rem;font-weight:800;color:rgba(255,184,0,.3);line-height:1;margin-bottom:8px}
.how-step b{display:block;font-size:.85rem;margin-bottom:4px}
.how-step p{color:var(--muted);font-size:.75rem;line-height:1.5}

/* WITHDRAW CARD */
.withdraw-card{background:var(--card);border:1px solid rgba(255,184,0,.25);border-radius:20px;padding:20px;margin-bottom:14px}
.withdraw-card h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.4rem;color:var(--gold)}
.withdraw-card .sub{color:var(--muted);font-size:.82rem;margin-bottom:1.2rem;line-height:1.5}
.fg{margin-bottom:12px}
.fg label{display:block;font-size:.75rem;font-weight:700;color:var(--muted);margin-bottom:.4rem;letter-spacing:.04em;text-transform:uppercase}
.fg input,.fg select{width:100%;background:#050e18;border:1px solid var(--border);color:var(--text);padding:12px 14px;border-radius:11px;font-size:.9rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s}
.fg input:focus,.fg select:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(255,184,0,.07)}
.verify-row{display:flex;gap:8px}
.verify-row input{flex:1}
.verify-btn{background:#0b1928;color:var(--muted);border:1px solid var(--border);border-radius:11px;padding:12px 14px;font-size:.82rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
.verify-btn:hover{border-color:rgba(0,255,200,.3);color:var(--accent)}
.account-name-display{font-size:.82rem;color:var(--green);font-weight:700;margin-top:5px;min-height:18px}
.withdraw-btn{width:100%;padding:14px;background:linear-gradient(135deg,var(--gold),#ff8c00);border:none;border-radius:12px;color:#030e0a;font-weight:800;font-size:.95rem;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .2s;margin-top:4px}
.withdraw-btn:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(255,184,0,.35)}
.withdraw-btn:disabled{opacity:.5;transform:none;box-shadow:none}
.locked-msg{background:rgba(255,184,0,.06);border:1px solid rgba(255,184,0,.2);border-radius:12px;padding:14px;font-size:.85rem;color:var(--muted);line-height:1.6;text-align:center}
.locked-msg strong{color:var(--gold);display:block;margin-bottom:4px}

/* HISTORY */
.history-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.history-card h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin-bottom:1rem}
.ref-item{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-radius:11px;background:#050e18;margin-bottom:7px;font-size:.82rem}
.ref-item .email{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.ref-item .badge{padding:3px 8px;border-radius:6px;font-weight:700;font-size:.7rem;white-space:nowrap;margin-left:8px}
.badge.paid{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(34,197,94,.25)}
.badge.pending{background:rgba(255,184,0,.12);color:#fde68a;border:1px solid rgba(255,184,0,.25)}
.empty-state{color:var(--muted);font-size:.85rem;text-align:center;padding:20px;line-height:1.6}

/* ERRORS & NOTICES */
.err-box{display:none;color:#fecdd3;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:10px 14px;border-radius:10px;font-size:.83rem;margin-bottom:10px}
.err-box.show{display:block}
.ok-box{display:none;color:#86efac;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);padding:10px 14px;border-radius:10px;font-size:.83rem;margin-bottom:10px}
.ok-box.show{display:block}
.loader{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.2);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}

@media(max-width:600px){.stats-row{grid-template-columns:1fr 1fr}.how-steps{grid-template-columns:1fr}.share-btns{flex-direction:column}}
</style>
</head>
<body>
<nav>
  <a class="logo" href="/"><svg width="22" height="22" viewBox="0 0 200 200" fill="none"><defs><linearGradient id="rG1" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00ffc8"/><stop offset="1" stop-color="rgba(0,255,200,.7)"/></linearGradient><linearGradient id="rG2" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00aaff"/><stop offset="1" stop-color="#00ffc8"/></linearGradient></defs><rect x="52" y="58" width="52" height="7" rx="2" fill="url(#rG1)"/><rect x="74" y="65" width="8" height="70" rx="2" fill="url(#rG1)"/><path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#rG2)" stroke-width="7" fill="none" stroke-linecap="round"/><rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#rG2)"/></svg>Tik<em>Genius</em></a>
  <div class="nav-right">
    <a class="nav-link" href="/dashboard">AI Studio</a>
    <a class="btn-nav" href="/dashboard">Dashboard</a>
  </div>
</nav>

<div class="wrap">
  <div class="page-hero">
    <div class="page-badge">💰 Referral Program</div>
    <h1>Refer friends.<br><em>Earn real money.</em></h1>
    <p>Share your unique link. Every time someone upgrades to Premium through your link, ₦500 lands in your wallet — automatically paid to your bank account.</p>
  </div>

  <!-- STATS -->
  <div class="stats-row">
    <div class="stat-card highlight">
      <div class="val" id="balanceVal">₦0</div>
      <div class="lbl">Wallet Balance</div>
    </div>
    <div class="stat-card">
      <div class="val" id="paidVal">0</div>
      <div class="lbl">Paid Referrals</div>
    </div>
    <div class="stat-card">
      <div class="val" id="totalEarnedVal">₦0</div>
      <div class="lbl">Total Earned</div>
    </div>
  </div>

  <!-- REFERRAL LINK -->
  <div class="link-card">
    <h3>Your referral link</h3>
    <p>Share this link anywhere — TikTok bio, WhatsApp, X, Instagram. When someone signs up and goes Premium, you earn.</p>
    <div class="link-box">
      <input class="link-input" id="refLinkInput" readonly value="Loading...">
      <button class="copy-link-btn" onclick="copyRefLink()">Copy Link</button>
    </div>
    <div class="share-btns">
      <button class="share-btn" onclick="shareWhatsApp()">📱 WhatsApp</button>
      <button class="share-btn" onclick="shareX()">🐦 Post on X</button>
      <button class="share-btn" onclick="shareTikTok()">🎵 TikTok Bio</button>
      <button class="share-btn" onclick="shareNative()">↗ Share</button>
    </div>
  </div>

  <!-- HOW IT WORKS -->
  <div class="how-card">
    <h3>How it works</h3>
    <div class="how-steps">
      <div class="how-step"><div class="num">01</div><b>Share your link</b><p>Post it on TikTok, WhatsApp, X — anywhere your audience is</p></div>
      <div class="how-step"><div class="num">02</div><b>They upgrade</b><p>When they sign up and pay for Premium, it's automatically tracked</p></div>
      <div class="how-step"><div class="num">03</div><b>You get paid</b><p>₦500 added to your wallet instantly. Withdraw to your bank anytime</p></div>
    </div>
  </div>

  <!-- WITHDRAW -->
  <div class="withdraw-card">
    <h3>💳 Withdraw to bank</h3>
    <div class="sub" id="withdrawSub">Minimum withdrawal: ₦2,000 (4 referrals). Paid instantly via Paystack.</div>
    <div class="err-box" id="wErr"></div>
    <div class="ok-box" id="wOk"></div>
    <div id="withdrawForm">
      <div class="fg">
        <label>Select Bank</label>
        <select id="bankSelect" onchange="onBankChange()"><option value="">Loading banks...</option></select>
      </div>
      <div class="fg">
        <label>Account Number</label>
        <div class="verify-row">
          <input type="text" id="accountNumber" placeholder="0123456789" maxlength="10" oninput="clearAccountName()">
          <button class="verify-btn" id="verifyBtn" onclick="verifyAccount()">Verify</button>
        </div>
        <div class="account-name-display" id="accountNameDisplay"></div>
      </div>
      <button class="withdraw-btn" id="withdrawBtn" onclick="doWithdraw()" disabled>Withdraw ₦0 to Bank</button>
    </div>
    <div class="locked-msg" id="lockedMsg" style="display:none">
      <strong>Keep referring to unlock withdrawal</strong>
      You need ₦2,000 in your wallet to withdraw. You currently have <span id="currentBal">₦0</span>. Keep sharing your link!
    </div>
  </div>

  <!-- RECENT REFERRALS -->
  <div class="history-card">
    <h3>Recent referrals</h3>
    <div id="refList"><div class="empty-state">No referrals yet. Share your link to start earning!</div></div>
  </div>

  <!-- WITHDRAWAL HISTORY -->
  <div class="history-card">
    <h3>Withdrawal history</h3>
    <div id="wdList"><div class="empty-state">No withdrawals yet.</div></div>
  </div>
</div>

<script>
var stats={}, banks=[], verifiedName='', verifiedBankCode='', verifiedAccountNumber='';

async function loadStats(){
  try{
    var res=await fetch('/api/referral/stats');
    if(res.status===401){window.location.href='/';return;}
    stats=await res.json();
    document.getElementById('balanceVal').textContent='₦'+stats.balance_ngn.toLocaleString();
    document.getElementById('paidVal').textContent=stats.paid_referrals;
    document.getElementById('totalEarnedVal').textContent='₦'+stats.total_earned_ngn.toLocaleString();
    document.getElementById('refLinkInput').value=stats.referral_link||'';
    updateWithdrawUI();
    renderReferrals(stats.recent||[]);
  }catch(e){console.error(e)}
}

async function loadBanks(){
  try{
    var res=await fetch('/api/referral/banks');
    var data=await res.json();
    banks=data.banks||[];
    var sel=document.getElementById('bankSelect');
    sel.innerHTML='<option value="">Select your bank</option>';
    banks.forEach(b=>{sel.innerHTML+=`<option value="${b.code}">${b.name}</option>`});
  }catch(e){console.error(e)}
}

async function loadWithdrawalHistory(){
  try{
    var res=await fetch('/api/referral/withdrawal-history');
    var data=await res.json();
    var items=data.items||[];
    var el=document.getElementById('wdList');
    if(!items.length){el.innerHTML='<div class="empty-state">No withdrawals yet.</div>';return;}
    el.innerHTML=items.map(w=>`<div class="ref-item">
      <span class="email">₦${(w.amount_kobo/100).toLocaleString()} → ${escHtml(w.account_name||'')} (${escHtml(w.account_number||'')})</span>
      <span class="badge ${w.status}">${w.status}</span>
    </div>`).join('');
  }catch(e){}
}

function updateWithdrawUI(){
  var canWithdraw=stats.can_withdraw;
  var form=document.getElementById('withdrawForm');
  var locked=document.getElementById('lockedMsg');
  document.getElementById('currentBal').textContent='₦'+(stats.balance_ngn||0).toLocaleString();
  if(canWithdraw){
    form.style.display='block';locked.style.display='none';
    document.getElementById('withdrawBtn').textContent='Withdraw ₦'+(stats.balance_ngn||0).toLocaleString()+' to Bank';
  } else {
    form.style.display='none';locked.style.display='block';
  }
}

function renderReferrals(refs){
  var el=document.getElementById('refList');
  if(!refs.length){el.innerHTML='<div class="empty-state">No referrals yet. Share your link to start earning!</div>';return;}
  el.innerHTML=refs.map(r=>`<div class="ref-item">
    <span class="email">${escHtml(maskEmail(r.email||''))}</span>
    <span class="badge ${r.status}">${r.status==='paid'?'✓ Earned ₦'+(r.commission_kobo/100).toLocaleString():'Pending'}</span>
  </div>`).join('');
}

function maskEmail(email){
  var parts=email.split('@');
  if(parts.length<2)return email;
  var name=parts[0];
  return name.slice(0,2)+'***@'+parts[1];
}

function onBankChange(){clearAccountName();}
function clearAccountName(){
  document.getElementById('accountNameDisplay').textContent='';
  verifiedName='';verifiedAccountNumber='';verifiedBankCode='';
  document.getElementById('withdrawBtn').disabled=true;
}

async function verifyAccount(){
  var accountNumber=document.getElementById('accountNumber').value.trim();
  var bankCode=document.getElementById('bankSelect').value;
  var display=document.getElementById('accountNameDisplay');
  var btn=document.getElementById('verifyBtn');
  if(!accountNumber||!bankCode){showWErr('Please select a bank and enter your account number.');return;}
  if(accountNumber.length!==10){showWErr('Account number must be exactly 10 digits.');return;}
  btn.innerHTML='<span class="loader"></span>';btn.disabled=true;
  hideWErr();
  try{
    var res=await fetch('/api/referral/verify-account',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_number:accountNumber,bank_code:bankCode})});
    var data=await res.json();
    if(data.error){display.textContent='';showWErr(data.error);return;}
    verifiedName=data.account_name;
    verifiedAccountNumber=accountNumber;
    verifiedBankCode=bankCode;
    display.textContent='✓ '+data.account_name;
    if(stats.can_withdraw){
      document.getElementById('withdrawBtn').disabled=false;
      document.getElementById('withdrawBtn').textContent='Withdraw ₦'+(stats.balance_ngn||0).toLocaleString()+' → '+data.account_name;
    }
  }catch(e){showWErr('Could not verify account. Please try again.');}
  finally{btn.textContent='Verify';btn.disabled=false;}
}

async function doWithdraw(){
  if(!verifiedName||!verifiedAccountNumber||!verifiedBankCode){showWErr('Please verify your account number first.');return;}
  var btn=document.getElementById('withdrawBtn');
  btn.innerHTML='<span class="loader"></span>Processing...';btn.disabled=true;
  hideWErr();hideWOk();
  try{
    var res=await fetch('/api/referral/withdraw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_number:verifiedAccountNumber,bank_code:verifiedBankCode,account_name:verifiedName})});
    var data=await res.json();
    if(data.error){showWErr(data.error);return;}
    showWOk(data.message||'Withdrawal initiated successfully!');
    await loadStats();await loadWithdrawalHistory();
  }catch(e){showWErr('Network error. Please try again.');}
  finally{btn.disabled=false;btn.textContent='Withdraw to Bank';}
}

function copyRefLink(){
  var link=document.getElementById('refLinkInput').value;
  navigator.clipboard.writeText(link).then(()=>{
    var btn=document.querySelector('.copy-link-btn');
    btn.textContent='Copied!';setTimeout(()=>btn.textContent='Copy Link',2000);
  });
}

function shareWhatsApp(){
  var link=document.getElementById('refLinkInput').value;
  var msg=encodeURIComponent('I use TikGenius to generate viral TikTok captions, hooks and scripts in seconds. Try it free: '+link);
  window.open('https://wa.me/?text='+msg,'_blank');
}

function shareX(){
  var link=document.getElementById('refLinkInput').value;
  var msg=encodeURIComponent('I use TikGenius to generate viral TikTok content with AI. Try it free 👇 '+link);
  window.open('https://twitter.com/intent/tweet?text='+msg,'_blank');
}

function shareTikTok(){
  var link=document.getElementById('refLinkInput').value;
  navigator.clipboard.writeText(link).then(()=>{alert('Link copied! Add it to your TikTok bio.');});
}

function shareNative(){
  var link=document.getElementById('refLinkInput').value;
  if(navigator.share){navigator.share({title:'TikGenius',text:'Generate viral TikTok content with AI',url:link}).catch(()=>{});}
  else{copyRefLink();}
}

function showWErr(msg){var e=document.getElementById('wErr');e.textContent=msg;e.classList.add('show');}
function hideWErr(){document.getElementById('wErr').classList.remove('show');}
function showWOk(msg){var e=document.getElementById('wOk');e.textContent=msg;e.classList.add('show');}
function hideWOk(){document.getElementById('wOk').classList.remove('show');}
function escHtml(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

loadStats();loadBanks();loadWithdrawalHistory();
</script>
</body>
</html>"""


# Keep original DASHBOARD_HTML and DOWNLOAD_HTML exactly as provided
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius Studio</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#03050a;--panel:#07111c;--panel2:#0b1928;--line:#14253a;
  --text:#f0f8ff;--muted:#607a90;
  --accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--danger:#fb7185;
}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(ellipse at 80% 0%,rgba(0,170,255,.08),transparent 45%),var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
h1,h2,h3{font-family:'Syne',sans-serif;letter-spacing:-.04em}
.app{display:grid;grid-template-columns:300px 1fr;min-height:100vh}
.side{background:rgba(7,17,28,.96);border-right:1px solid var(--line);padding:16px;position:sticky;top:0;height:100vh;overflow-y:auto;display:flex;flex-direction:column;gap:0}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:1.3rem;letter-spacing:-.04em;margin-bottom:12px;display:flex;align-items:center;gap:.4rem;color:var(--text)}
.logo em{color:var(--accent);font-style:normal}
.user-chip{padding:9px 12px;background:rgba(11,25,40,.8);border:1px solid var(--line);border-radius:12px;font-size:.8rem;color:var(--muted);margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.usage-box{padding:14px;background:linear-gradient(135deg,#071828,#0b1928);border:1px solid var(--line);border-radius:16px;margin-bottom:12px}
.usage-box strong{display:block;font-size:.875rem;font-weight:600;margin-bottom:8px}
.bar-bg{height:5px;background:#0f2030;border-radius:99px;overflow:hidden;margin-bottom:8px}
.bar-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:99px;transition:width .4s}
.upgrade-btn{display:none;width:100%;padding:10px;background:linear-gradient(135deg,var(--accent),var(--gold));border:none;border-radius:10px;color:#030e0a;font-weight:800;font-size:.875rem;font-family:'DM Sans',sans-serif;cursor:pointer;transition:opacity .2s}
.upgrade-btn.show{display:block}
.upgrade-btn:hover{opacity:.88}
.side-label{font-size:.7rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:16px 4px 8px;padding:0}
.modes{display:grid;gap:5px}
.mode{border:1px solid transparent;background:transparent;color:var(--muted);text-align:left;padding:10px 12px;border-radius:11px;font-size:.875rem;font-weight:500;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .15s;display:flex;align-items:center;gap:.5rem}
.mode:hover{background:#0b1928;color:var(--text);border-color:var(--line)}
.mode.active{background:#0f2234;color:var(--accent);border-color:rgba(0,255,200,.25)}
.dl-link{display:block;text-decoration:none;margin-top:6px;padding:10px 12px;border-radius:11px;background:rgba(0,255,200,.07);border:1px solid rgba(0,255,200,.15);color:var(--accent);font-size:.875rem;font-weight:700;text-align:center;transition:background .2s}
.dl-link:hover{background:rgba(0,255,200,.13)}
.earn-link{display:block;text-decoration:none;margin-top:6px;padding:10px 12px;border-radius:11px;background:rgba(255,184,0,.07);border:1px solid rgba(255,184,0,.2);color:var(--gold);font-size:.875rem;font-weight:700;text-align:center;transition:background .2s}
.earn-link:hover{background:rgba(255,184,0,.13)}
.hist-head{display:flex;align-items:center;justify-content:space-between;margin:16px 4px 8px}
.hist-head .side-label{margin:0;padding:0}
.clear-btn{background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:99px;padding:5px 8px;font-size:.7rem;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .2s}
.clear-btn:hover{color:var(--accent);border-color:rgba(0,255,200,.3)}
.history{display:grid;gap:6px;flex:1;overflow-y:auto}
.hist-item{padding:9px 10px;background:#07111c;border:1px solid var(--line);border-radius:10px;cursor:pointer;transition:border-color .15s}
.hist-item:hover{border-color:rgba(0,255,200,.25)}
.hist-item b{display:block;font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}
.hist-item span{font-size:.72rem;color:var(--muted);margin-top:2px;display:block}
.empty-hist{color:var(--muted);font-size:.8rem;line-height:1.5;padding:9px 10px;background:#07111c;border:1px dashed var(--line);border-radius:10px}
.logout-btn{margin-top:12px;width:100%;background:transparent;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:9px;font-family:'DM Sans',sans-serif;font-size:.8rem;cursor:pointer;transition:all .2s}
.logout-btn:hover{color:var(--danger);border-color:rgba(251,113,133,.3)}
.main{padding:24px;max-width:960px;width:100%;margin:0 auto}
.top-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;gap:12px}
.mobile-logo{display:none;font-family:'Syne',sans-serif;font-weight:800;font-size:1.2rem}
.mobile-logo em{color:var(--accent);font-style:normal}
.drawer-btn{display:none;background:#0b1928;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:.85rem;cursor:pointer;font-family:'DM Sans',sans-serif}
.region-select{background:#07111c;color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 12px;font-size:.875rem;font-family:'DM Sans',sans-serif;outline:none;cursor:pointer}
.mobile-history{display:none;margin-bottom:16px}
.mob-hist-scroll{display:flex;overflow-x:auto;gap:8px;padding-bottom:3px}
.mob-hist-scroll .hist-item{min-width:180px;flex-shrink:0}
.guide{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.tip{background:#071018;border:1px solid var(--line);border-radius:14px;padding:12px}
.tip b{font-size:.82rem;font-weight:700;color:var(--text);display:block;margin-bottom:4px}
.tip p{color:var(--muted);font-size:.77rem;line-height:1.45;margin:0}
.studio-card{background:rgba(7,17,28,.9);border:1px solid var(--line);border-radius:22px;padding:20px;box-shadow:0 24px 64px rgba(0,0,0,.3)}
.prompt-input{width:100%;min-height:145px;background:#050e18;color:var(--text);border:1px solid var(--line);border-radius:16px;padding:15px;font:500 15px/1.6 'DM Sans',sans-serif;resize:vertical;outline:none;transition:border-color .2s}
.prompt-input:focus{border-color:rgba(0,255,200,.4);box-shadow:0 0 0 3px rgba(0,255,200,.06)}
.prompt-input::placeholder{color:var(--muted)}
.actions-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:12px}
.hint{font-size:.77rem;color:var(--muted)}
.gen-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:12px;color:#030e0a;font-weight:800;padding:13px 22px;font-size:.95rem;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .2s;white-space:nowrap}
.gen-btn:hover{transform:translateY(-1px);box-shadow:0 6px 28px rgba(0,255,200,.3)}
.gen-btn:disabled{opacity:.5;transform:none;box-shadow:none}
.err-box{display:none;margin-top:12px;color:#fecdd3;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:11px 14px;border-radius:12px;font-size:.875rem}
.premium-lock{display:none;margin-top:14px;padding:16px;border-radius:16px;background:linear-gradient(135deg,rgba(0,255,200,.08),rgba(255,184,0,.06));border:1px solid rgba(255,184,0,.25)}
.premium-lock.show{display:block}
.premium-lock h3{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin:0 0 5px}
.premium-lock p{margin:0 0 12px;color:var(--muted);font-size:.85rem;line-height:1.5}
.output-section{display:none;margin-top:20px}
.output-section.show{display:block}
.output-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.output-title{font-family:'Syne',sans-serif;font-weight:800;font-size:1.05rem}
.copy-all-btn{background:#0b1928;color:var(--accent);border:1px solid rgba(0,255,200,.25);border-radius:9px;padding:7px 13px;font-size:.8rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .2s}
.copy-all-btn:hover{background:rgba(0,255,200,.1)}
.result-list{display:grid;gap:10px}
.result-item{background:#050e18;border:1px solid var(--line);border-radius:14px;padding:14px 16px;position:relative;transition:border-color .2s}
.result-item:hover{border-color:rgba(0,255,200,.2)}
.result-item-text{white-space:pre-wrap;line-height:1.7;color:#d0e8ff;font-size:.9rem;padding-right:80px}
.item-copy-btn{position:absolute;top:10px;right:10px;background:#0b1928;color:var(--muted);border:1px solid var(--line);border-radius:7px;padding:5px 10px;font-size:.72rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;transition:all .2s;white-space:nowrap}
.item-copy-btn:hover{color:var(--accent);border-color:rgba(0,255,200,.3);background:rgba(0,255,200,.06)}
.item-copy-btn.copied{color:var(--accent);border-color:var(--accent)}
.result-raw{white-space:pre-wrap;line-height:1.75;color:#d0e8ff;background:#050e18;border:1px solid var(--line);border-radius:16px;padding:16px;font-size:.9rem}
.drawer-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:60;backdrop-filter:blur(4px)}
.drawer-overlay.show{display:block}
.drawer-panel{position:absolute;left:0;top:0;bottom:0;width:88%;max-width:300px;background:#07111c;border-right:1px solid var(--line);padding:16px;overflow-y:auto;display:flex;flex-direction:column;gap:0}
@media(max-width:820px){
  .app{display:block}.side{display:none}.main{padding:14px}.mobile-logo{display:block}.drawer-btn{display:block}
  .top-bar{position:sticky;top:0;z-index:10;background:rgba(3,5,10,.95);padding:12px 0;border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}
  .guide{grid-template-columns:1fr}.studio-card{padding:14px;border-radius:18px}.prompt-input{min-height:120px}
  .actions-row{flex-direction:column;align-items:stretch}.gen-btn{width:100%}.mobile-history{display:block}
}
</style>
</head>
<body>
<div class="app">
<aside class="side" id="desktopSide">
  <div class="logo"><svg width="22" height="22" viewBox="0 0 200 200" fill="none"><defs><linearGradient id="dG1" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00ffc8"/><stop offset="1" stop-color="rgba(0,255,200,.7)"/></linearGradient><linearGradient id="dG2" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00aaff"/><stop offset="1" stop-color="#00ffc8"/></linearGradient></defs><rect x="52" y="58" width="52" height="7" rx="2" fill="url(#dG1)"/><rect x="74" y="65" width="8" height="70" rx="2" fill="url(#dG1)"/><path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#dG2)" stroke-width="7" fill="none" stroke-linecap="round"/><rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#dG2)"/></svg>Tik<em>Genius</em></div>
  <div class="user-chip" id="userEmail">Loading...</div>
  <div class="usage-box">
    <strong id="usesLabel">5/5 free generations left</strong>
    <div class="bar-bg"><div class="bar-fill" id="barFill" style="width:100%"></div></div>
    <button type="button" class="upgrade-btn" id="upgradeBtn" data-upgrade onclick="doUpgrade(event)">✦ Upgrade to Premium</button>
  </div>
  <div class="side-label">Create Content</div>
  <div class="modes" id="modes"></div>
  <a class="dl-link" href="/download">⬇ TikTok Downloader</a>
  <a class="earn-link" href="/refer">💰 Earn ₦500/Referral</a>
  <div class="hist-head"><div class="side-label">Recent History</div><button class="clear-btn" onclick="clearHistory()">Clear</button></div>
  <div class="history" id="historyList"><div class="empty-hist">Your content history will appear here.</div></div>
  <button class="logout-btn" onclick="doLogout()">Log out</button>
</aside>
<div class="drawer-overlay" id="drawer" onclick="closeDrawer(event)"><div class="drawer-panel" id="drawerPanel"></div></div>
<main class="main">
  <div class="top-bar">
    <div class="mobile-logo">Tik<em>Genius</em></div>
    <button class="drawer-btn" onclick="openDrawer()">☰ Menu</button>
    <a href="/download" style="text-decoration:none"><button class="mode" style="padding:8px 12px;border-radius:10px;font-size:.8rem;white-space:nowrap">⬇ Downloader</button></a>
    <select class="region-select" id="regionSelect" onchange="changeRegion(this.value)">
      <option value="global">🌍 Global</option><option value="nigeria">🇳🇬 Nigerian</option><option value="usa">🇺🇸 American</option><option value="uk">🇬🇧 British</option><option value="caribbean">🇯🇲 Caribbean</option><option value="eastafrica">🇰🇪 East African</option><option value="southafrica">🇿🇦 South African</option>
    </select>
  </div>
  <div class="mobile-history">
    <div class="hist-head"><div class="side-label">Recent History</div><button class="clear-btn" onclick="clearHistory()">Clear</button></div>
    <div class="mob-hist-scroll" id="historyMobile"><div class="empty-hist">No history yet.</div></div>
  </div>
  <div class="guide">
    <div class="tip"><b>1. Choose a mode</b><p>Pick captions, hooks, scripts, hashtags, POVs, or X content from the sidebar.</p></div>
    <div class="tip"><b>2. Be specific</b><p>Include your niche, target audience, emotion, and goal for the best results.</p></div>
    <div class="tip"><b>3. Copy &amp; post</b><p>Each result has its own copy button — grab the best one and post it today.</p></div>
  </div>
  <section class="studio-card">
    <textarea class="prompt-input" id="topicInput" placeholder="Example: Give me 5 TikTok captions for a skincare video targeting young women who want clear skin.&#10;&#10;Or: Write an X thread about building discipline as a young creator."></textarea>
    <div class="actions-row">
      <div class="hint">Minimum 3 words · Works for TikTok and X</div>
      <button class="gen-btn" id="generateBtn" onclick="generate()">✦ Generate</button>
    </div>
    <div class="err-box" id="errorMsg"></div>
    <div class="premium-lock" id="premiumLock">
      <h3>You used your 5 free generations</h3>
      <p>Upgrade to Premium for unlimited content every day.</p>
      <button type="button" class="upgrade-btn show" data-upgrade onclick="doUpgrade(event)">✦ Upgrade to Premium</button>
    </div>
  </section>
  <section class="output-section" id="outputCard">
    <div class="output-header">
      <div class="output-title" id="outputTitle">Ready to post</div>
      <button class="copy-all-btn" onclick="copyOutput()">Copy All</button>
    </div>
    <div id="outputBody"></div>
  </section>
</main>
</div>
<script>
let currentMode='captions',currentPlatform='tiktok',userData={};
const modes=[['captions','📝 TikTok Captions','tiktok'],['hooks','🪝 Viral Hooks','tiktok'],['pov','🎭 POV Ideas','tiktok'],['script','🎬 Video Script','tiktok'],['hashtags','# Hashtags','tiktok'],['captions','🐦 X Posts','x'],['hooks','🧵 X Hooks','x'],['threads','📖 X Thread','x']];
const modeTitles={captions:'Captions',hooks:'Hooks',pov:'POV Ideas',script:'Video Script',hashtags:'Hashtags',threads:'X Thread'};
function renderModes(targetId='modes'){
  const el=document.getElementById(targetId);if(!el)return;
  let tiktok=modes.filter(m=>m[2]==='tiktok'),x=modes.filter(m=>m[2]==='x');
  let html='<div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:5px">TikTok</div>';
  tiktok.forEach((m,i)=>{html+=`<button class="mode ${i==0?'active':''}" onclick="setMode('${m[0]}','${m[2]}',this)">${m[1]}</button>`});
  html+='<div style="font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:10px 0 5px">X / Twitter</div>';
  x.forEach(m=>{html+=`<button class="mode" onclick="setMode('${m[0]}','${m[2]}',this)">${m[1]}</button>`});
  el.innerHTML=html;
}
function setMode(m,p,btn){currentMode=m;currentPlatform=p;document.querySelectorAll('.mode').forEach(x=>x.classList.remove('active'));if(btn)btn.classList.add('active');document.getElementById('outputCard').classList.remove('show');}
async function loadUser(){
  const res=await fetch('/api/me');if(res.status===401){location.href='/';return}
  userData=await res.json();
  document.querySelectorAll('#userEmail').forEach(e=>e.textContent=userData.email);
  document.getElementById('regionSelect').value=userData.region||'global';
  updateUsage(userData.uses_remaining,userData.unlimited);loadHistory();
}
function updateUsage(rem,unlimited){
  const label=document.getElementById('usesLabel'),fill=document.getElementById('barFill'),up=document.getElementById('upgradeBtn');
  if(unlimited){label.textContent='✦ Premium: unlimited generations';fill.style.width='100%';up.classList.remove('show');return}
  label.textContent=rem+'/5 free generations left';fill.style.width=(rem/5*100)+'%';
  if(rem<=0){up.classList.add('show');document.getElementById('premiumLock').classList.add('show')}else{up.classList.remove('show')}
}
async function loadHistory(){
  const res=await fetch('/api/history');const data=await res.json();
  const html=(data.items&&data.items.length)?data.items.map(i=>{const plat=(i.platform==='x')?'X':'TikTok';return `<div class="hist-item" onclick='showHistory(${JSON.stringify(i).replace(/'/g,"&#39;")})'><b>${escapeHtml(i.topic||'Untitled')}</b><span>${plat} · ${i.mode} · ${new Date(i.created_at).toLocaleDateString()}</span></div>`;}).join(''):'<div class="empty-hist">Your content history will appear here.</div>';
  ['historyList','historyMobile','drawerHistory'].forEach(id=>{const el=document.getElementById(id);if(el)el.innerHTML=html});
}
async function clearHistory(){if(!confirm('Clear all your generation history?'))return;const res=await fetch('/api/history/clear',{method:'POST'});if(res.ok){document.getElementById('outputCard').classList.remove('show');loadHistory()}else showError('Could not clear history.');}
function showHistory(i){document.getElementById('topicInput').value=i.topic||'';document.getElementById('outputTitle').textContent=(modeTitles[i.mode]||i.mode)+' · from history';renderOutputItems(i.result||'');document.getElementById('outputCard').classList.add('show');document.getElementById('outputCard').scrollIntoView({behavior:'smooth'});document.getElementById('drawer')&&document.getElementById('drawer').classList.remove('show');}
function escapeHtml(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function changeRegion(region){await fetch('/api/set-region',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region})});}
function parseResultItems(text){const parts=text.split(/\n(?=\d+[.)\s])/);if(parts.length>1)return parts.map(p=>p.trim()).filter(Boolean);const paras=text.split(/\n\n+/);if(paras.length>1)return paras.map(p=>p.trim()).filter(Boolean);return null;}
function renderOutputItems(text){
  const body=document.getElementById('outputBody');const items=parseResultItems(text);
  if(items&&items.length>1){body.innerHTML='<div class="result-list">'+items.map((item,idx)=>`<div class="result-item"><div class="result-item-text">${escapeHtml(item)}</div><button class="item-copy-btn" onclick="copyItem(this,'${escapeHtml(item).replace(/'/g,"&#39;").replace(/\n/g,'\\n')}')" title="Copy">Copy</button></div>`).join('')+'</div>';}
  else{body.innerHTML=`<div style="position:relative"><div class="result-raw">${escapeHtml(text)}</div></div>`;}
  body.dataset.raw=text;
}
function copyItem(btn,text){const raw=text.replace(/&#39;/g,"'").replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/\\n/g,'\n');navigator.clipboard.writeText(raw).then(()=>{btn.textContent='Copied!';btn.classList.add('copied');setTimeout(()=>{btn.textContent='Copy';btn.classList.remove('copied')},1600);});}
function copyOutput(){const raw=document.getElementById('outputBody').dataset.raw||'';navigator.clipboard.writeText(raw).then(()=>{const btn=document.querySelector('.copy-all-btn');btn.textContent='Copied!';setTimeout(()=>btn.textContent='Copy All',1600);});}
async function generate(){
  const topic=document.getElementById('topicInput').value.trim(),btn=document.getElementById('generateBtn');
  hideError();if(!topic)return showError('Please enter your prompt.');if(topic.split(/\s+/).length<3)return showError('Please add at least 3 words.');
  btn.disabled=true;btn.textContent='Generating...';
  const res=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:currentMode,platform:currentPlatform,topic})});
  const data=await res.json();btn.disabled=false;btn.textContent='✦ Generate';
  if(data.error){showError(data.error);if(res.status===429)document.getElementById('premiumLock').classList.add('show');return}
  document.getElementById('outputTitle').textContent=(modeTitles[currentMode]||currentMode)+' — ready to post';
  renderOutputItems(data.result);document.getElementById('outputCard').classList.add('show');document.getElementById('outputCard').scrollIntoView({behavior:'smooth'});
  if(data.uses_remaining!==undefined)updateUsage(data.uses_remaining,false);loadHistory();
}
function showError(m){const e=document.getElementById('errorMsg');e.textContent=m;e.style.display='block'}
function hideError(){document.getElementById('errorMsg').style.display='none'}
let upgradeInProgress=false;
async function doUpgrade(event){
  if(event&&event.preventDefault)event.preventDefault();if(upgradeInProgress)return false;upgradeInProgress=true;hideError();
  const buttons=Array.from(document.querySelectorAll('[data-upgrade],.upgrade-btn'));
  buttons.forEach(b=>{b.disabled=true;b.dataset.oldText=b.textContent;b.textContent='Opening payment...'});
  try{const res=await fetch('/api/upgrade',{method:'POST',credentials:'same-origin',cache:'no-store'});let d={};try{d=await res.json()}catch(e){}
  if(res.status===401){location.href='/';return false}if(d.url){window.location.assign(d.url);return false}showError(d.error||'Could not open payment. Please try again.');}
  catch(e){showError('Network error. Please try again.')}
  finally{upgradeInProgress=false;buttons.forEach(b=>{b.disabled=false;b.textContent=b.dataset.oldText||'✦ Upgrade to Premium'})}return false;
}
document.addEventListener('click',function(e){const btn=e.target.closest('[data-upgrade]');if(btn)doUpgrade(e)},false);
async function doLogout(){await fetch('/api/logout',{method:'POST'});location.href='/'}
function openDrawer(){const p=document.getElementById('drawerPanel');p.innerHTML=document.getElementById('desktopSide').innerHTML;const h=p.querySelector('#historyList');if(h)h.id='drawerHistory';const m=p.querySelector('#modes');if(m)m.id='drawerModes';document.getElementById('drawer').classList.add('show');renderModes('drawerModes');loadHistory();}
function closeDrawer(e){if(e.target.id==='drawer')document.getElementById('drawer').classList.remove('show')}
renderModes();loadUser();
</script>
</body>
</html>"""


DOWNLOAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius — Download TikTok Videos (No Watermark)</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#03050a;--surface:#07111c;--card:#0b1928;--border:#14253a;--text:#f0f8ff;--muted:#607a90;--accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--green:#22c55e;--danger:#fb7185}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(ellipse at 70% -10%,rgba(0,170,255,.1),transparent 38%),radial-gradient(ellipse at 10% 85%,rgba(0,255,200,.07),transparent 35%),var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:.85rem 1.4rem;position:sticky;top:0;z-index:100;background:rgba(3,5,10,.85);backdrop-filter:blur(18px);border-bottom:1px solid rgba(0,255,200,.07)}
.logo{display:flex;align-items:center;gap:.5rem;text-decoration:none;color:var(--text);font-family:'Syne',sans-serif;font-weight:800;font-size:1.15rem;letter-spacing:-.03em}
.logo em{color:var(--accent);font-style:normal}
.nav-right{display:flex;align-items:center;gap:.65rem}
.nav-link{text-decoration:none;color:var(--muted);font-size:.875rem;font-weight:600;transition:color .2s;padding:.4rem .6rem;border-radius:7px}
.nav-link:hover,.nav-link.active{color:var(--accent)}
.btn-nav{background:var(--accent);color:#030e0a;border:none;padding:.45rem 1.1rem;border-radius:8px;font-size:.875rem;font-weight:700;cursor:pointer;text-decoration:none;font-family:'DM Sans',sans-serif;transition:all .2s}
.btn-nav:hover{transform:translateY(-1px);box-shadow:0 0 20px rgba(0,255,200,.3)}
.hero{text-align:center;padding:5rem 1.5rem 2.5rem;max-width:680px;margin:0 auto}
.hero-badge{display:inline-flex;align-items:center;gap:.45rem;background:rgba(0,255,200,.08);border:1px solid rgba(0,255,200,.2);color:var(--accent);padding:.35rem 1rem;border-radius:100px;font-size:.75rem;font-weight:700;margin-bottom:1.8rem;letter-spacing:.05em;text-transform:uppercase}
.hero h1{font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(2rem,7vw,3.6rem);line-height:1.0;letter-spacing:-.045em;margin-bottom:1rem}
.hero h1 em{color:var(--accent);font-style:normal}
.hero p{color:var(--muted);font-size:1rem;line-height:1.7;max-width:500px;margin:0 auto}
.main-wrap{max-width:700px;margin:0 auto;padding:0 1.25rem 5rem}
.dl-card{background:rgba(7,17,28,.9);border:1px solid var(--border);border-radius:24px;padding:24px;box-shadow:0 32px 80px rgba(0,0,0,.35)}
.how-tip{background:#050e18;border:1px solid var(--border);border-radius:12px;padding:11px 14px;font-size:.8rem;color:var(--muted);margin-bottom:16px;line-height:1.5}
.how-tip strong{color:var(--text)}
.input-row{display:flex;gap:10px;margin-bottom:8px}
.url-input{flex:1;background:#050e18;color:var(--text);border:1px solid var(--border);border-radius:13px;padding:13px 16px;font-size:.95rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s;min-width:0}
.url-input:focus{border-color:rgba(0,255,200,.4);box-shadow:0 0 0 3px rgba(0,255,200,.06)}
.url-input::placeholder{color:var(--muted)}
.fetch-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:13px;color:#030e0a;font-weight:800;padding:13px 20px;font-size:.9rem;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
.fetch-btn:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(0,255,200,.3)}
.fetch-btn:disabled{opacity:.55;transform:none;box-shadow:none}
.input-hint{font-size:.75rem;color:var(--muted);margin-bottom:12px}
.err-box{display:none;margin-bottom:10px;color:#fecdd3;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:10px 14px;border-radius:11px;font-size:.85rem}
.err-box.show{display:block}
.loader{display:none;flex-direction:column;align-items:center;gap:10px;padding:24px 0;color:var(--muted);font-size:.875rem}
.loader.show{display:flex}
.spin{width:32px;height:32px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.preview{display:none}
.preview.show{display:block;margin-top:14px}
.vid-card{background:#050e18;border:1px solid var(--border);border-radius:18px;overflow:hidden}
.vid-top{display:flex;gap:14px;padding:16px}
.vid-cover{width:70px;height:70px;object-fit:cover;border-radius:10px;flex-shrink:0;background:#0b1928}
.vid-info{flex:1;min-width:0}
.vid-title{font-size:.9rem;font-weight:700;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.4;margin-bottom:4px}
.vid-author{font-size:.8rem;color:var(--accent);font-weight:600;margin-bottom:6px}
.vid-meta{display:flex;flex-wrap:wrap;gap:5px}
.meta-tag{background:#0b1928;border:1px solid var(--border);border-radius:6px;padding:2px 8px;font-size:.7rem;color:var(--muted);font-weight:600}
.ad-gate{padding:18px 16px;border-top:1px solid var(--border)}
.ad-gate-title{font-family:'Syne',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.4rem}
.ad-gate-sub{color:var(--muted);font-size:.82rem;line-height:1.55;margin-bottom:14px}
.choices{display:grid;gap:10px}
.choice-btn{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:15px;border-radius:14px;font-family:'DM Sans',sans-serif;cursor:pointer;font-size:.9rem;font-weight:700;transition:all .2s;border:2px solid transparent}
.choice-btn .choice-label{font-size:.73rem;font-weight:500;opacity:.75}
.choice-free{background:#0b1928;border-color:var(--border);color:var(--text)}
.choice-free:hover{border-color:rgba(0,255,200,.35);color:var(--accent)}
.choice-pro{background:linear-gradient(135deg,rgba(0,255,200,.12),rgba(255,184,0,.08));border-color:rgba(255,184,0,.35);color:var(--gold)}
.choice-pro:hover{border-color:var(--gold);transform:translateY(-1px)}
.pro-skip{display:none;padding:12px 16px;border-top:1px solid var(--border);background:rgba(0,255,200,.05)}
.pro-skip.show{display:flex;align-items:center;gap:10px}
.pro-skip-text{font-size:.85rem;font-weight:600;color:var(--accent)}
.pro-skip-sub{font-size:.75rem;color:var(--muted);margin-top:2px}
.dl-panel{display:none;padding:16px;border-top:1px solid var(--border)}
.dl-panel.show{display:block}
.dl-panel-title{font-size:.8rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.dl-buttons{display:grid;gap:9px}
.dl-btn{display:flex;justify-content:space-between;align-items:center;padding:13px 16px;border-radius:12px;text-decoration:none;font-size:.875rem;font-weight:700;font-family:'DM Sans',sans-serif;transition:all .2s;border:1px solid transparent}
.dl-btn-meta{font-size:.73rem;font-weight:500;opacity:.75}
.dl-primary{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#030e0a}
.dl-primary:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(0,255,200,.3)}
.dl-secondary{background:#0b1928;color:var(--text);border-color:var(--border)}
.dl-secondary:hover{border-color:rgba(0,255,200,.25)}
.dl-audio{background:#0b1928;color:var(--muted);border-color:var(--border)}
.dl-audio:hover{border-color:rgba(0,170,255,.3);color:var(--accent2)}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1rem;margin-top:2.5rem}
.feat{background:rgba(7,17,28,.8);border:1px solid var(--border);border-radius:16px;padding:1.25rem;display:flex;flex-direction:column;gap:.5rem}
.feat-icon{font-size:1.5rem}
.feat h3{font-family:'Syne',sans-serif;font-weight:700;font-size:.9rem}
.feat p{color:var(--muted);font-size:.8rem;line-height:1.55}
.also-try{margin-top:2rem;background:linear-gradient(135deg,rgba(0,255,200,.07),rgba(0,170,255,.05));border:1px solid rgba(0,255,200,.15);border-radius:18px;padding:1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.also-try-text strong{display:block;font-family:'Syne',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:.25rem;color:var(--accent)}
.also-try-text span{color:var(--muted);font-size:.82rem}
.also-try-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#030e0a;border:none;border-radius:10px;padding:10px 18px;font-size:.875rem;font-weight:700;font-family:'DM Sans',sans-serif;cursor:pointer;white-space:nowrap;text-decoration:none;transition:all .2s}
.also-try-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,255,200,.3)}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
.modal-overlay.active{display:flex}
.modal{background:#0b1928;border:1px solid rgba(0,255,200,.2);border-radius:22px;padding:2rem;width:100%;max-width:400px;margin:1rem}
.modal h2{font-family:'Syne',sans-serif;font-weight:800;font-size:1.3rem;margin-bottom:.3rem}
.modal p{color:var(--muted);font-size:.85rem;margin-bottom:1.3rem;line-height:1.5}
.modal-tabs{display:flex;background:#050e18;border-radius:10px;padding:3px;gap:3px;margin-bottom:1.3rem}
.modal-tab{flex:1;padding:.5rem;border:none;border-radius:7px;background:transparent;color:var(--muted);font-size:.85rem;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif;transition:all .2s}
.modal-tab.active{background:#14253a;color:var(--text)}
.fg{margin-bottom:.9rem}
.fg label{display:block;font-size:.75rem;font-weight:700;color:var(--muted);margin-bottom:.4rem;letter-spacing:.04em;text-transform:uppercase}
.fg input{width:100%;background:#050e18;border:1px solid var(--border);color:var(--text);padding:.7rem 1rem;border-radius:10px;font-size:.875rem;font-family:'DM Sans',sans-serif;outline:none;transition:border-color .2s}
.fg input:focus{border-color:var(--accent)}
.ferr{display:none;color:#fb7185;font-size:.8rem;margin-bottom:.8rem;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:.55rem .85rem;border-radius:8px}
.modal-btn{width:100%;padding:.8rem;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:10px;color:#030e0a;font-size:.9rem;font-weight:700;cursor:pointer;font-family:'DM Sans',sans-serif;margin-bottom:.7rem;transition:all .2s}
.modal-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,255,200,.25)}
.modal-cancel{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.8rem;font-family:'DM Sans',sans-serif;width:100%;padding:.4rem}
@media(max-width:600px){.hero{padding:3.5rem 1rem 2rem}.main-wrap{padding:0 1rem 4rem}.dl-card{padding:16px;border-radius:18px}.input-row{flex-direction:column}.fetch-btn{width:100%}.nav-link{display:none}.choices{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav>
  <a class="logo" href="/"><svg width="26" height="26" viewBox="0 0 200 200" fill="none"><defs><linearGradient id="dlG1" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00ffc8"/><stop offset="1" stop-color="rgba(0,255,200,.7)"/></linearGradient><linearGradient id="dlG2" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00aaff"/><stop offset="1" stop-color="#00ffc8"/></linearGradient></defs><rect x="52" y="58" width="52" height="7" rx="2" fill="url(#dlG1)"/><rect x="74" y="65" width="8" height="70" rx="2" fill="url(#dlG1)"/><path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#dlG2)" stroke-width="7" fill="none" stroke-linecap="round"/><rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#dlG2)"/></svg>Tik<em>Genius</em></a>
  <div class="nav-right">
    <a class="nav-link" href="/dashboard">✦ AI Studio</a>
    <a class="nav-link active" href="/download">⬇ Downloader</a>
    <a class="btn-nav" href="/dashboard">Open Studio</a>
  </div>
</nav>
<div class="hero">
  <div class="hero-badge"><svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M19.59 6.69a4.83 4.83 0 01-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 01-2.88 2.5 2.89 2.89 0 01-2.89-2.89 2.89 2.89 0 012.89-2.89c.28 0 .54.04.79.1V9.01a6.33 6.33 0 00-.79-.05 6.34 6.34 0 00-6.34 6.34 6.34 6.34 0 006.34 6.34 6.34 6.34 0 006.33-6.34V8.69a8.19 8.19 0 004.79 1.54V6.78a4.85 4.85 0 01-1.02-.09z"/></svg>TikTok Video Downloader</div>
  <h1>Download TikToks<br><em>No watermark.</em></h1>
  <p>Paste any TikTok link and save the video in HD — clean, no watermark. Free users get 3 downloads/day. Premium unlocks unlimited.</p>
</div>
<div class="main-wrap">
  <div class="dl-card" id="downloaderCard">
    <div class="how-tip"><strong>How to get the link:</strong> Open TikTok → tap Share → Copy Link → paste below.</div>
    <div class="input-row">
      <input type="url" class="url-input" id="urlInput" placeholder="https://www.tiktok.com/@user/video/..." autocomplete="off" autocorrect="off" spellcheck="false">
      <button class="fetch-btn" id="fetchBtn" onclick="fetchVideo()">Fetch Video</button>
    </div>
    <div class="input-hint">Works with tiktok.com, vm.tiktok.com and /t/ short links.</div>
    <div class="err-box" id="errorBox"></div>
    <div class="loader" id="loader"><div class="spin"></div><div>Fetching video info...</div></div>
    <div class="preview" id="previewSection">
      <div class="vid-card">
        <div class="vid-top">
          <img class="vid-cover" id="vidCover" src="" alt="cover">
          <div class="vid-info"><div class="vid-title" id="vidTitle"></div><div class="vid-author" id="vidAuthor"></div><div class="vid-meta" id="vidMeta"></div></div>
        </div>
        <div class="ad-gate" id="adGate">
          <div class="ad-gate-title">🎬 One quick step to unlock your download</div>
          <div class="ad-gate-sub">3 free TikTok downloads per day — or go Premium for instant, unlimited downloads.</div>
          <div class="choices">
            <button class="choice-btn choice-free" onclick="useFreeDownload()">⬇ Use Free Download<span class="choice-label">3 free per day</span></button>
            <button class="choice-btn choice-pro" onclick="upgradeToPro()">⚡ Go Premium<span class="choice-label">₦2,000/month · Instant always</span></button>
          </div>
        </div>
        <div class="pro-skip" id="proSkip"><span style="font-size:1.2rem">✅</span><div><div class="pro-skip-text">Premium — instant download</div><div class="pro-skip-sub">Unlimited downloads included</div></div></div>
        <div class="dl-panel" id="dlPanel">
          <div class="dl-panel-title">Choose your format</div>
          <div class="dl-buttons">
            <a class="dl-btn dl-primary" id="dlNoWatermark" href="#" download onclick="confirmDownload(event,'nowm')"><span>⬇ No Watermark — HD</span><span class="dl-btn-meta">Clean · MP4</span></a>
            <a class="dl-btn dl-secondary" id="dlWatermark" href="#" download onclick="confirmDownload(event,'wm')"><span>⬇ Original with Watermark</span><span class="dl-btn-meta">MP4</span></a>
            <a class="dl-btn dl-audio" id="dlAudio" href="#" download onclick="confirmDownload(event,'audio')"><span>⬇ Audio Only</span><span class="dl-btn-meta">MP3</span></a>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="features">
    <div class="feat"><div class="feat-icon">🚫</div><h3>No Watermark</h3><p>Clean HD video — no TikTok logo burned in.</p></div>
    <div class="feat"><div class="feat-icon">⚡</div><h3>Instant for Premium</h3><p>Premium users skip every gate and download instantly.</p></div>
    <div class="feat"><div class="feat-icon">🎵</div><h3>Audio Extraction</h3><p>Save the background music as a standalone MP3.</p></div>
  </div>
  <div class="also-try">
    <div class="also-try-text"><strong>✦ Also try the TikGenius AI Studio</strong><span>Write viral captions, hooks, POVs, scripts, and X threads</span></div>
    <a class="also-try-btn" href="/dashboard">Open AI Studio →</a>
  </div>
</div>
<div class="modal-overlay" id="authModal">
  <div class="modal">
    <h2 id="modalTitle">Create your account</h2>
    <p id="modalSub">Sign up to start downloading — 3 free downloads per day</p>
    <div class="modal-tabs">
      <button class="modal-tab active" id="tabSignup" onclick="switchAuthTab('signup')">Sign Up</button>
      <button class="modal-tab" id="tabLogin" onclick="switchAuthTab('login')">Log In</button>
    </div>
    <div id="fSignup">
      <div class="fg"><label>Email</label><input type="email" id="sEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="sPass" placeholder="Min 6 characters"></div>
      <div class="ferr" id="sErr"></div>
      <button class="modal-btn" onclick="doSignup()">Create Account & Continue</button>
    </div>
    <div id="fLogin" style="display:none">
      <div class="fg"><label>Email</label><input type="email" id="lEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="lPass" placeholder="Your password"></div>
      <div class="ferr" id="lErr"></div>
      <button class="modal-btn" onclick="doLogin()">Log In & Continue</button>
    </div>
    <button class="modal-cancel" onclick="closeAuthModal()">Cancel</button>
  </div>
</div>
<script>
var videoData=null,userLoggedIn=false,userIsPro=false;
window.addEventListener('DOMContentLoaded',async function(){try{var res=await fetch('/api/me');if(res.ok){var d=await res.json();userLoggedIn=true;userIsPro=(d.plan==='pro');}}catch(e){}});
async function fetchVideo(){
  var url=document.getElementById('urlInput').value.trim();hideError();resetPreview();
  if(!url){showError('Please paste a TikTok link first.');return;}
  if(!userLoggedIn){openAuthModal('signup','download');return;}
  setLoading(true);
  try{var res=await fetch('/api/download/fetch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});var data=await res.json();setLoading(false);
  if(!res.ok||data.error){showError(data.error||'Could not fetch this video.');return;}videoData=data;renderPreview(data);}
  catch(e){setLoading(false);showError('Network error — please try again.');}
}
function renderPreview(d){
  document.getElementById('vidCover').src=d.cover||'';document.getElementById('vidTitle').textContent=d.title||'TikTok Video';document.getElementById('vidAuthor').textContent=d.author?'@'+d.author:'';
  var meta=document.getElementById('vidMeta');meta.innerHTML='';if(d.duration){meta.innerHTML+='<span class="meta-tag">'+Math.round(d.duration)+'s</span>';}meta.innerHTML+='<span class="meta-tag">No Watermark</span><span class="meta-tag">HD</span>';
  document.getElementById('previewSection').classList.add('show');
  if(d.is_pro){document.getElementById('proSkip').classList.add('show');revealDownloads(d);}else{document.getElementById('adGate').style.display='block';}
}
async function useFreeDownload(){
  hideError();if(!videoData){showError('Please fetch a TikTok video first.');return;}
  try{var res=await fetch('/api/download/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('urlInput').value.trim(),title:videoData?videoData.title:''})});var d=await res.json();
  if(!res.ok||d.error){showError(d.error||'Could not unlock download.');return;}document.getElementById('adGate').style.display='none';revealDownloads(videoData);}
  catch(e){showError('Network error — please try again.');}
}
async function upgradeToPro(){
  try{var res=await fetch('/api/upgrade',{method:'POST',credentials:'same-origin'});var d=await res.json();
  if(res.status===401){openAuthModal('login','upgrade');return;}if(d.url){window.location.assign(d.url);return;}showError(d.error||'Could not open payment.');}
  catch(e){showError('Network error.');}
}
function proxyDownloadUrl(fileUrl,filename){return '/api/download/file?url='+encodeURIComponent(fileUrl)+'&filename='+encodeURIComponent(filename);}
function revealDownloads(d){
  var panel=document.getElementById('dlPanel');panel.classList.add('show');var base=sanitizeFilename((d&&d.title)||'tiktok');
  var dlNW=document.getElementById('dlNoWatermark');if(d&&d.play_url){dlNW.href=proxyDownloadUrl(d.play_url,base+'_nowm.mp4');dlNW.setAttribute('download',base+'_nowm.mp4');}else{dlNW.style.display='none';}
  var dlWM=document.getElementById('dlWatermark');if(d&&d.wmplay_url){dlWM.href=proxyDownloadUrl(d.wmplay_url,base+'_wm.mp4');dlWM.setAttribute('download',base+'_wm.mp4');}else{dlWM.style.display='none';}
  var dlAU=document.getElementById('dlAudio');if(d&&d.music_url){dlAU.href=proxyDownloadUrl(d.music_url,base+'_audio.mp3');dlAU.setAttribute('download',base+'_audio.mp3');}else{dlAU.style.display='none';}
  panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function confirmDownload(e,type){if(!userIsPro)return;try{fetch('/api/download/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('urlInput').value.trim(),title:videoData?videoData.title:''})});}catch(err){}}
var _postAuthAction=null;
function openAuthModal(tab,action){_postAuthAction=action;document.getElementById('authModal').classList.add('active');switchAuthTab(tab||'signup');}
function closeAuthModal(){document.getElementById('authModal').classList.remove('active');}
function switchAuthTab(tab){document.getElementById('fSignup').style.display=tab==='signup'?'block':'none';document.getElementById('fLogin').style.display=tab==='login'?'block':'none';document.getElementById('tabSignup').classList.toggle('active',tab==='signup');document.getElementById('tabLogin').classList.toggle('active',tab==='login');document.getElementById('modalTitle').textContent=tab==='signup'?'Create your account':'Welcome back';document.getElementById('modalSub').textContent=tab==='signup'?'Sign up to start downloading — 3 free downloads per day':'Log in to your TikGenius account';}
async function doSignup(){var email=document.getElementById('sEmail').value.trim(),pass=document.getElementById('sPass').value,err=document.getElementById('sErr');err.style.display='none';var res=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pass,region:'global'})});var data=await res.json();if(data.error){err.textContent=data.error;err.style.display='block';return;}userLoggedIn=true;userIsPro=false;closeAuthModal();if(_postAuthAction==='upgrade'){upgradeToPro();}else{fetchVideo();}}
async function doLogin(){var email=document.getElementById('lEmail').value.trim(),pass=document.getElementById('lPass').value,err=document.getElementById('lErr');err.style.display='none';var res=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pass})});var data=await res.json();if(data.error){err.textContent=data.error;err.style.display='block';return;}userLoggedIn=true;try{var me=await(await fetch('/api/me')).json();userIsPro=me.plan==='pro';}catch(e){}closeAuthModal();if(_postAuthAction==='upgrade'){upgradeToPro();}else{fetchVideo();}}
document.getElementById('authModal').addEventListener('click',function(e){if(e.target===this)closeAuthModal();});
function setLoading(show){document.getElementById('loader').classList.toggle('show',show);document.getElementById('fetchBtn').disabled=show;document.getElementById('fetchBtn').textContent=show?'Fetching...':'Fetch Video';}
function resetPreview(){if(document.getElementById('adGate'))document.getElementById('adGate').style.display='';document.getElementById('previewSection').classList.remove('show');document.getElementById('proSkip').classList.remove('show');document.getElementById('dlPanel').classList.remove('show');}
function showError(msg){var b=document.getElementById('errorBox');b.textContent=msg;b.classList.add('show');}
function hideError(){document.getElementById('errorBox').classList.remove('show');}
function sanitizeFilename(s){return s.replace(/[^a-z0-9_\-]/gi,'_').slice(0,60);}
document.getElementById('urlInput').addEventListener('keydown',function(e){if(e.key==='Enter')fetchVideo();});
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
