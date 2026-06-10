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

# Referral config
REFERRAL_COMMISSION_KOBO = int(os.getenv("REFERRAL_COMMISSION_KOBO", "50000"))   # 500 default
REFERRAL_MIN_WITHDRAW_KOBO = 200000   # 2,000

app = Flask(__name__)
app.secret_key = SECRET_KEY
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "3650"))  # permanent ~10 years
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = os.getenv("SESSION_COOKIE_HTTPONLY", "true").lower() == "true"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set. Add it in Railway Variables.")
    db_url = DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    db_pool = pool.SimpleConnectionPool(1, 10, db_url, cursor_factory=RealDictCursor)

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
            cur.execute("""CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                balance_kobo INTEGER DEFAULT 0,
                total_earned_kobo INTEGER DEFAULT 0,
                total_withdrawn_kobo INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT NOW()
            )""")
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
            cur.execute("""CREATE TABLE IF NOT EXISTS web_conversations (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
                title TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("""CREATE TABLE IF NOT EXISTS web_chat_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES web_conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )""")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_web_conversations_user ON web_conversations(user_id, updated_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_web_chat_messages_conv ON web_chat_messages(conversation_id, id)")
        conn.commit()
    finally:
        release_db(conn)

init_db()

# ========================= REFERRAL HELPERS =========================

def generate_referral_code():
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

# ========================= PAYSTACK TRANSFER =========================

def paystack_get_banks():
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get("https://api.paystack.co/bank?currency=NGN&perPage=100", headers=headers, timeout=15)
        data = res.json()
        if data.get("status"):
            return data["data"]
    except Exception as e:
        print(f"Paystack banks error: {e}")
    return []

def paystack_resolve_account(account_number, bank_code):
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
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {"type": "nuban", "name": account_name, "account_number": account_number, "bank_code": bank_code, "currency": "NGN"}
    try:
        res = http_session.post("https://api.paystack.co/transferrecipient", json=payload, headers=headers, timeout=15)
        data = res.json()
        if data.get("status"):
            return data["data"]["recipient_code"], None
        return None, data.get("message", "Could not create recipient")
    except Exception as e:
        return None, str(e)

def paystack_get_balance():
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = http_session.get("https://api.paystack.co/balance", headers=headers, timeout=15)
        data = res.json()
        if data.get("status") and data.get("data"):
            for b in data["data"]:
                if b.get("currency") == "NGN":
                    return int(b.get("balance", 0)), None
        return 0, data.get("message", "Could not fetch balance")
    except Exception as e:
        return 0, str(e)

def paystack_get_total_pending_wallets():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(balance_kobo), 0) AS total FROM wallets")
            return int(cur.fetchone()["total"])
    finally:
        release_db(conn)

def paystack_initiate_transfer(amount_kobo, recipient_code, reference, reason="TikGenius referral earnings"):
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {"source": "balance", "amount": amount_kobo, "recipient": recipient_code, "reason": reason, "reference": reference}
    try:
        res = http_session.post("https://api.paystack.co/transfer", json=payload, headers=headers, timeout=20)
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
            if request.path.startswith('/api/'):
                return jsonify({"error": "Please log in"}), 401
            return redirect("/?login=1")
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
    "nigeria": "Nigerian",
    "usa": "American",
    "uk": "British",
    "caribbean": "Caribbean",
    "eastafrica": "East African",
    "southafrica": "South African",
    "global": "Global"
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

# ===== Conversational chat system prompt (free chat, content creators only) =====
CHAT_SYSTEM = """You are TikGenius — a friendly, sharp AI assistant built ONLY for content creators. You chat naturally, like a smart creative partner the user can talk to about anything related to making content.

YOUR SCOPE — you help with anything in the world of content creation:
- TikTok, Instagram, YouTube, Twitter/X, Facebook content
- Captions, hooks, video scripts, POV concepts, bios, hashtags, thread ideas
- Content ideas, niche selection, posting strategy, going viral, trends
- Audience growth, engagement, monetization as a creator, brand deals
- Feedback on the user's drafts, captions, or ideas
- Planning content calendars, repurposing content across platforms

{region_voice}

HOW TO BEHAVE:
- Be conversational and natural. Answer the user's question directly first.
- If they ask for content (captions, hooks, a script), generate it immediately — do NOT interrogate them with questions first. If one short clarifying question would massively improve the result, you may ask it, but never more than one.
- Remember the conversation context — build on what was said earlier.
- When delivering a content pack or list, label sections in bold like **5 HOOKS** and number items 1) 2) 3).
- Keep advice specific and practical, never generic motivational fluff.

STRICT SCOPE RULE: You ONLY discuss content creation and creator growth. If the user asks about anything unrelated — homework, coding, politics, medical or legal advice, relationships, general knowledge — politely say you are built only for content creators, and invite them back to their content. Do not answer off-topic questions, not even partially.

PUNCTUATION RULE: Never use "..." (ellipsis). Use a dash ( — ), a line break, or a full stop instead.

LANGUAGE: Clean modern English. Casual, real, emotional, and sharp. Never sound like a motivational poster or a robotic AI."""

TIKTOK_PROMPTS = {
"hooks": """Write 10 TikTok hooks for a creator posting about: {topic}

A hook stops the scroll in under 2 seconds. Study these viral hooks and WHY they work:

"Nobody is coming to save you. Build yourself." — Direct, activates the ego
"The version of me from 2 years ago would not recognise me." — Curiosity + transformation
"I used to be so easy to lose. Not anymore." — Short, personal, empowering
"Tell me why I worked this hard just to still be stressed" — Funny + relatable frustration
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

"God will give you the life you prayed for. Just not in the timeline you imagined."
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
"building the life I used to dream about | tips + real talk"
"I left the 9-5. Now I film my life. | come along"
"healing out loud so you don't have to do it alone"
"I document real life, not the highlight reel"

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
      else{fetch('/api/track-click',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:payload,keepalive:true});}
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
    return with_analytics(STUDIO_HTML)

@app.route("/dashboard")
def dashboard():
    return redirect("/")

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

            if referrer_id and referrer_id != user_id:
                cur.execute("""INSERT INTO referrals (referrer_id, referred_id, status)
                    VALUES (%s, %s, 'pending') ON CONFLICT (referred_id) DO NOTHING""",
                    (referrer_id, user_id))

            cur.execute("""INSERT INTO wallets (user_id) VALUES (%s) ON CONFLICT DO NOTHING""", (user_id,))

        conn.commit()
        session.pop("pending_ref", None)
        keep_user_signed_in(user_id, email)
        return jsonify({"success": True, "redirect": "/"})
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
            cur.execute("INSERT INTO wallets (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user["id"],))
        conn.commit()
        keep_user_signed_in(user["id"], user["email"])
        return jsonify({"success": True, "redirect": "/"})
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

@app.route("/api/conversations")
@login_required
def list_conversations():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, title, created_at, updated_at
                FROM web_conversations WHERE user_id=%s
                ORDER BY updated_at DESC LIMIT 30""", (session["user_id"],))
            rows = cur.fetchall()
        return jsonify({"items": [dict(r) for r in rows]})
    finally:
        release_db(conn)

@app.route("/api/conversations/<int:conv_id>")
@login_required
def get_conversation(conv_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title FROM web_conversations WHERE id=%s AND user_id=%s",
                (conv_id, session["user_id"]))
            conv = cur.fetchone()
            if not conv:
                return jsonify({"error": "Conversation not found"}), 404
            cur.execute("""SELECT role, content FROM web_chat_messages
                WHERE conversation_id=%s ORDER BY id ASC LIMIT 200""", (conv_id,))
            msgs = cur.fetchall()
        return jsonify({"id": conv["id"], "title": conv["title"],
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs]})
    finally:
        release_db(conn)

@app.route("/api/conversations/clear", methods=["POST"])
@login_required
def clear_conversations():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM web_conversations WHERE user_id=%s", (session["user_id"],))
        conn.commit()
        return jsonify({"success": True})
    finally:
        release_db(conn)

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    """Free-flowing conversational AI for content creators.
    Accepts the full conversation history and replies naturally,
    while staying strictly scoped to content creation topics."""
    data = request.json or {}
    raw_msgs = data.get("messages") or []
    region = data.get("region", "global")
    conversation_id = data.get("conversation_id")

    # Sanitize and cap the conversation history
    clean = []
    for m in raw_msgs[-12:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()[:4000]
        if role in ("user", "assistant") and content:
            clean.append({"role": role, "content": content})

    if not clean or clean[-1]["role"] != "user":
        return jsonify({"error": "Please type a message first"}), 400

    user_id = session["user_id"]
    if not check_and_increment_web_usage(user_id):
        return jsonify({"error": "You have used all 5 free messages today. Upgrade to Premium for unlimited access."}), 429

    region_voice = REGION_VOICES.get(region, REGION_VOICES["global"])
    system = CHAT_SYSTEM.format(region_voice=region_voice)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "system", "content": system}] + clean,
        "temperature": 0.85,
        "max_tokens": 2000,
        "top_p": 0.95
    }
    try:
        res = http_session.post(url, json=payload, headers=headers, timeout=40)
        api_data = res.json()
        if "choices" not in api_data or not api_data["choices"]:
            print(f"Groq chat error: {api_data}")
            return jsonify({"error": "The AI could not respond. Please try again."}), 500
        reply = api_data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"error": "Could not reach the AI. Please try again."}), 500

    # Save the exchange to permanent conversation history
    last_user_msg = clean[-1]["content"]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            conv_id = None
            if conversation_id:
                try:
                    cur.execute("SELECT id FROM web_conversations WHERE id=%s AND user_id=%s",
                        (int(conversation_id), user_id))
                    row = cur.fetchone()
                    if row:
                        conv_id = row["id"]
                except (ValueError, TypeError):
                    conv_id = None
            if not conv_id:
                cur.execute("""INSERT INTO web_conversations (user_id, title)
                    VALUES (%s, %s) RETURNING id""", (user_id, last_user_msg[:80]))
                conv_id = cur.fetchone()["id"]
            cur.execute("""INSERT INTO web_chat_messages (conversation_id, role, content)
                VALUES (%s, 'user', %s)""", (conv_id, last_user_msg))
            cur.execute("""INSERT INTO web_chat_messages (conversation_id, role, content)
                VALUES (%s, 'assistant', %s)""", (conv_id, reply))
            cur.execute("UPDATE web_conversations SET updated_at=NOW() WHERE id=%s", (conv_id,))
            cur.execute("""INSERT INTO web_generations (user_id, mode, platform, topic, result)
                VALUES (%s, %s, %s, %s, %s)""", (user_id, "chat", "auto", last_user_msg[:500], reply))
        conn.commit()
    finally:
        release_db(conn)

    pro = is_web_pro(user_id)
    return jsonify({
        "reply": reply,
        "conversation_id": conv_id,
        "uses_remaining": web_uses_remaining(user_id) if not pro else None,
        "unlimited": pro
    })


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
        return redirect("/")
    link = create_payment_link(user["email"], session["user_id"], "web")
    return redirect(link or "/")

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


# ========================= DIAGNOSTICS =========================
@app.route("/diag")
def diag_page():
    return DIAG_HTML

@app.route("/api/diag/run")
def diag_run():
    import time
    results = []

    def check(name, fn):
        t0 = time.time()
        try:
            ok, detail = fn()
            results.append({"name": name, "ok": ok, "detail": detail, "ms": round((time.time()-t0)*1000)})
        except Exception as e:
            results.append({"name": name, "ok": False, "detail": str(e), "ms": round((time.time()-t0)*1000)})

    # 1. DATABASE
    def test_db():
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM web_users")
                row = cur.fetchone()
            return True, f"{row['c']} users in DB"
        finally:
            release_db(conn)
    check("Database connection", test_db)

    # 2. SESSION / SECRET_KEY
    def test_secret():
        key = app.secret_key or ""
        if not key:
            return False, "SECRET_KEY is empty"
        if key == "None" or len(key) < 8:
            return False, f"SECRET_KEY looks invalid: {key[:10]}..."
        env_key = os.getenv("SECRET_KEY", "")
        if not env_key:
            return False, "SECRET_KEY env var NOT SET — sessions will reset on every deploy! Set it in Railway variables."
        return True, f"SECRET_KEY env var is set ({len(env_key)} chars)"
    check("SECRET_KEY / Sessions", test_secret)

    # 3. GROQ API KEY
    def test_groq():
        if not GROQ_API_KEY:
            return False, "GROQ_API_KEY env var is missing — AI generation will fail"
        if len(GROQ_API_KEY) < 20:
            return False, f"GROQ_API_KEY looks too short: {GROQ_API_KEY[:8]}..."
        return True, f"GROQ_API_KEY is set ({len(GROQ_API_KEY)} chars)"
    check("Groq API Key (AI generation)", test_groq)

    # 4. PAYSTACK
    def test_paystack():
        if not PAYSTACK_SECRET_KEY:
            return False, "PAYSTACK_SECRET_KEY env var is missing — payments will fail"
        if not PAYSTACK_PUBLIC_KEY:
            return False, "PAYSTACK_PUBLIC_KEY env var is missing"
        return True, f"Paystack keys set (secret: {len(PAYSTACK_SECRET_KEY)} chars)"
    check("Paystack Keys (payments)", test_paystack)

    # 5. DATABASE_URL format
    def test_db_url():
        if not DATABASE_URL:
            return False, "DATABASE_URL env var is not set"
        if DATABASE_URL.startswith("postgres://"):
            return True, "DATABASE_URL uses postgres:// (auto-converted to postgresql://)"
        if DATABASE_URL.startswith("postgresql://"):
            return True, "DATABASE_URL format is correct (postgresql://)"
        return False, f"DATABASE_URL has unexpected format: {DATABASE_URL[:20]}..."
    check("DATABASE_URL format", test_db_url)

    # 6. GROQ LIVE CALL TEST
    def test_groq_live():
        if not GROQ_API_KEY:
            return False, "GROQ_API_KEY not set — skipping live test"
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 5
        }
        res = http_session.post(url, json=payload, headers=headers, timeout=15)
        data = res.json()
        if "choices" in data:
            return True, f"Groq API responded: {data['choices'][0]['message']['content'].strip()}"
        return False, f"Groq error: {data.get('error', {}).get('message', str(data))}"
    check("Groq live API call", test_groq_live)

    # 7. SESSION COOKIE CONFIG
    def test_cookies():
        issues = []
        if app.config.get("SESSION_COOKIE_SECURE"):
            issues.append("SESSION_COOKIE_SECURE=True — may block cookies on HTTP/proxy")
        samesite = app.config.get("SESSION_COOKIE_SAMESITE", "")
        if samesite not in ("Lax", "None", "Strict"):
            issues.append(f"SESSION_COOKIE_SAMESITE is unusual: {samesite}")
        if issues:
            return False, " | ".join(issues)
        return True, f"Secure={app.config.get('SESSION_COOKIE_SECURE')}, SameSite={samesite}"
    check("Session cookie config", test_cookies)

    # 8. SESSION READ/WRITE TEST
    def test_session():
        uid = session.get("user_id")
        if uid:
            return True, f"Active session found — user_id={uid}"
        return False, "No active session on this request (not logged in, or cookies not sent)"
    check("Active session (are you logged in?)", test_session)

    # 9. DB TABLES EXIST
    def test_tables():
        conn = get_db()
        try:
            with conn.cursor() as cur:
                cur.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public' ORDER BY table_name""")
                tables = [r["table_name"] for r in cur.fetchall()]
            expected = ["web_users","web_generations","web_payments","referrals","wallets","withdrawals"]
            missing = [t for t in expected if t not in tables]
            if missing:
                return False, f"Missing tables: {', '.join(missing)}"
            return True, f"All tables exist: {', '.join(tables)}"
        finally:
            release_db(conn)
    check("Database tables", test_tables)

    # 10. RAPIDAPI KEY
    def test_rapidapi():
        if not RAPIDAPI_KEY:
            return False, "RAPIDAPI_KEY not set — TikTok downloader will fail"
        return True, f"RAPIDAPI_KEY is set ({len(RAPIDAPI_KEY)} chars)"
    check("RapidAPI Key (TikTok downloader)", test_rapidapi)

    all_ok = all(r["ok"] for r in results)
    return jsonify({"results": results, "all_ok": all_ok, "timestamp": datetime.utcnow().isoformat()})

@app.route("/health")
def health():
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

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT balance_kobo FROM wallets WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
        balance = int(row["balance_kobo"]) if row else 0
    finally:
        release_db(conn)

    if balance < REFERRAL_MIN_WITHDRAW_KOBO:
        return jsonify({"error": f"Minimum withdrawal is N{REFERRAL_MIN_WITHDRAW_KOBO//100:,}. Your balance is N{balance//100:,}."}), 400

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id FROM withdrawals WHERE user_id=%s AND status='pending'""", (user_id,))
            if cur.fetchone():
                return jsonify({"error": "You already have a pending withdrawal. Please wait for it to complete."}), 400
    finally:
        release_db(conn)

    ADMIN_RESERVE_KOBO = int(os.getenv("ADMIN_RESERVE_KOBO", "50000"))
    paystack_balance, bal_err = paystack_get_balance()
    if bal_err:
        print(f"Paystack balance check failed: {bal_err}")
    else:
        total_owed = paystack_get_total_pending_wallets()
        remaining_after = paystack_balance - balance
        still_owed_others = total_owed - balance
        if remaining_after < (still_owed_others + ADMIN_RESERVE_KOBO):
            print(f"Balance guard: paystack={paystack_balance} balance={balance} total_owed={total_owed} reserve={ADMIN_RESERVE_KOBO}")
            return jsonify({
                "error": "Withdrawal temporarily unavailable — please try again in a few hours. Your balance is safe and has not been touched."
            }), 503

    recipient_code, err = paystack_create_recipient(account_name, account_number, bank_code)
    if err:
        return jsonify({"error": f"Could not create transfer recipient: {err}"}), 400

    transfer_ref = f"TIKW-{user_id}-{int(datetime.utcnow().timestamp())}"
    transfer_data, err = paystack_initiate_transfer(balance, recipient_code, transfer_ref)
    if err:
        return jsonify({"error": f"Transfer failed: {err}"}), 400

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
        "message": f"N{balance//100:,} withdrawal initiated. It will arrive in your account within minutes.",
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

# ========================= PAYSTACK WEBHOOK =========================
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
                    send_telegram_message(telegram_id, f"Payment confirmed. Welcome to Pro.\n\nAccess active till {expires}\n\nEverything unlocked.")

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
        return redirect("/?payment=success")
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
    return f"N{(int(kobo or 0) / 100):,.0f}"

ADMIN_LOGIN_HTML = """<!doctype html>
<html><head><meta name='viewport' content='width=device-width, initial-scale=1'><title>TikGenius Admin Login</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;font-family:'Inter',system-ui,sans-serif;background:#060a12;color:#f8fbff;display:grid;place-items:center;padding:18px}.login{width:min(440px,100%);background:rgba(10,18,32,.82);border:1px solid rgba(125,167,255,.22);border-radius:28px;padding:26px}.brand{font-weight:900;font-size:24px;margin-bottom:12px}label{font-size:13px;color:#b8c7dd;font-weight:700;display:block;margin:14px 0 7px}input{width:100%;padding:15px 16px;border-radius:16px;border:1px solid #263852;background:#070d16;color:#fff;font:600 16px sans-serif;outline:none}button{width:100%;margin-top:18px;border:0;border-radius:16px;padding:15px;background:linear-gradient(135deg,#22d3ee,#10b981,#f6b21a);font-weight:900;color:#061018;font-size:16px;cursor:pointer}.err{display:%ERRDISPLAY%;margin-top:14px;color:#fecdd3;background:rgba(244,63,94,.12);border:1px solid rgba(244,63,94,.3);padding:12px;border-radius:14px;font-weight:700}</style></head>
<body><form class='login' method='post'><div class='brand'>TikGenius Admin</div><label>Admin email</label><input name='email' type='email' required><label>Password</label><input name='password' type='password' required><button>Unlock Dashboard</button><div class='err'>%ERROR%</div></form></body></html>"""

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
        f"<tr><td>{escape(str(p['paid_at'] or ''))}</td><td>{escape(p['email'] or '')}</td><td>{money_ngn(p['amount_kobo'])}</td><td>{escape(p['source'] or '')}</td><td>{escape(p['reference'] or '')}</td></tr>"
        for p in payments
    ) or "<tr><td colspan='5'>No payments yet.</td></tr>"

    user_rows = "".join(
        f"<tr><td>{u['id']}</td><td>{escape(u['name'] or '')}</td><td>{escape(u['email'])}</td><td>{escape(u['plan'] or 'free')}</td><td>{escape(str(u['expires'] or ''))}</td><td>{escape(u['region'] or '')}</td><td>{u['usage_count'] or 0}</td><td>{escape(str(u['created_at'] or ''))}</td><td>{escape(str(u['last_login_at'] or ''))}</td></tr>"
        for u in users
    ) or "<tr><td colspan='9'>No users yet.</td></tr>"

    withdrawal_rows = "".join(
        f"<tr><td>{escape(str(w['requested_at'] or ''))}</td><td>{escape(w['email'] or '')}</td><td>{escape(w['account_name'] or '')}</td><td>{escape(w['account_number'] or '')}</td><td>{money_ngn(w['amount_kobo'])}</td><td>{escape(w['status'] or '')}</td></tr>"
        for w in withdrawals
    ) or "<tr><td colspan='6'>No withdrawals yet.</td></tr>"

    top_page_rows = "".join(f"<tr><td>{escape(r['path'] or '')}</td><td>{r['views']}</td><td>{r['visitors']}</td></tr>" for r in top_pages)
    top_click_rows = "".join(f"<tr><td>{escape(r['label'] or '')}</td><td>{escape(r['element'] or '')}</td><td>{r['clicks']}</td></tr>" for r in top_clicks)

    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>TikGenius Admin</title>
<style>*{{box-sizing:border-box}}body{{margin:0;font-family:'Inter',system-ui,sans-serif;background:#060a12;color:#f3f7ff;min-height:100vh}}.wrap{{max-width:1280px;margin:auto;padding:18px}}h1{{font-size:1.5rem;margin:0 0 18px}}h2{{font-size:1rem;margin:0 0 10px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:18px}}.card{{background:#0d1525;border:1px solid #243550;border-radius:16px;padding:14px}}.num{{font-size:1.8rem;font-weight:900}}.lbl{{font-size:.72rem;color:#93a4bd;text-transform:uppercase;letter-spacing:.08em}}.section{{margin-bottom:18px;background:#0d1525;border:1px solid #243550;border-radius:16px;padding:16px}}table{{width:100%;border-collapse:collapse;font-size:.82rem}}th,td{{padding:9px 10px;border-bottom:1px solid #1e293b;text-align:left;white-space:nowrap}}th{{color:#bfdbfe;background:#101b30;font-size:.7rem;text-transform:uppercase}}.search{{width:100%;padding:10px;border-radius:10px;border:1px solid #334155;background:#07101d;color:white;margin-bottom:10px;outline:none}}a.logout{{color:#94a3b8;text-decoration:none;font-size:.82rem}}.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}}input.inp{{background:#07101d;border:1px solid #334155;color:white;padding:8px 10px;border-radius:8px;font-size:.82rem;outline:none}}button.btn{{border:none;border-radius:8px;padding:8px 12px;font-size:.82rem;font-weight:700;cursor:pointer}}.btn-green{{background:#10b981;color:#fff}}.btn-red{{background:#f43f5e;color:#fff}}</style></head>
<body><div class='wrap'>
<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:16px'><h1>TikGenius Admin</h1><a class='logout' href='/admin/logout'>Log out</a></div>
<div class='grid'>
<div class='card'><div class='lbl'>Revenue</div><div class='num'>{money_ngn(pay_stats['revenue'])}</div></div>
<div class='card'><div class='lbl'>Premium Users</div><div class='num'>{premium_users}</div></div>
<div class='card'><div class='lbl'>Free Users</div><div class='num'>{free_users}</div></div>
<div class='card'><div class='lbl'>Total Signups</div><div class='num'>{total_users}</div></div>
<div class='card'><div class='lbl'>Today Signups</div><div class='num'>{today_signups}</div></div>
<div class='card'><div class='lbl'>Generations</div><div class='num'>{total_generations}</div></div>
<div class='card'><div class='lbl'>Total Referrals</div><div class='num'>{total_refs}</div></div>
<div class='card'><div class='lbl'>Paid Referrals</div><div class='num'>{paid_refs}</div></div>
<div class='card'><div class='lbl'>Pending Payouts</div><div class='num'>{money_ngn(pending_payout)}</div></div>
<div class='card'><div class='lbl'>Page Views</div><div class='num'>{traffic_stats['views'] or 0}</div></div>
<div class='card'><div class='lbl'>Downloads</div><div class='num'>{dl_stats['total_downloads'] or 0}</div></div>
<div class='card'><div class='lbl'>Conversion</div><div class='num'>{conversion}%</div></div>
</div>
<div class='section'><h2>Admin Actions</h2>
<div class='actions'>
<input class='inp' id='grantEmail' placeholder='user@email.com' style='width:220px'>
<input class='inp' id='grantDays' placeholder='Days (30)' style='width:90px'>
<button class='btn btn-green' onclick='grantPremium()'>Grant Premium</button>
<button class='btn btn-red' onclick='revokePremium()'>Revoke Premium</button>
</div>
<div id='grantMsg' style='font-size:.82rem;color:#6ee7b7'></div>
</div>
<div class='section'><h2>Recent Payments</h2><div style='overflow:auto'><table><thead><tr><th>Date</th><th>Email</th><th>Amount</th><th>Source</th><th>Reference</th></tr></thead><tbody>{payment_rows}</tbody></table></div></div>
<div class='section'><h2>Withdrawals</h2><div style='overflow:auto'><table><thead><tr><th>Date</th><th>Email</th><th>Account Name</th><th>Account No.</th><th>Amount</th><th>Status</th></tr></thead><tbody>{withdrawal_rows}</tbody></table></div></div>
<div class='section'><h2>Top Pages</h2><div style='overflow:auto'><table><thead><tr><th>Page</th><th>Views</th><th>Visitors</th></tr></thead><tbody>{top_page_rows}</tbody></table></div></div>
<div class='section'><h2>All Users</h2><input class='search' id='search' placeholder='Search...' onkeyup='filterRows()'><div style='overflow:auto'><table id='users'><thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Plan</th><th>Expires</th><th>Region</th><th>Uses</th><th>Signup</th><th>Last Login</th></tr></thead><tbody>{user_rows}</tbody></table></div></div>
<div class='section'><h2>Exports</h2><a href='/admin/emails.csv' style='color:#38bdf8;font-size:.85rem;margin-right:14px'>Download Emails CSV</a><a href='/admin/payments.csv' style='color:#38bdf8;font-size:.85rem'>Download Payments CSV</a></div>
</div>
<script>
function filterRows(){{var q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('#users tbody tr').forEach(r=>{{r.style.display=r.innerText.toLowerCase().includes(q)?'':'none'}})}}
async function grantPremium(){{var email=document.getElementById('grantEmail').value.trim();var days=parseInt(document.getElementById('grantDays').value)||30;var msg=document.getElementById('grantMsg');if(!email){{msg.textContent='Enter an email first.';return;}}var res=await fetch('/api/admin/grant-premium',{{credentials:'include',method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email,days}})}});var d=await res.json();if(d.error){{msg.style.color='#fb7185';msg.textContent=d.error;}}else{{msg.style.color='#6ee7b7';msg.textContent='Premium granted to '+d.email+' until '+d.expires;setTimeout(()=>location.reload(),1500);}}}}
async function revokePremium(){{var email=document.getElementById('grantEmail').value.trim();var msg=document.getElementById('grantMsg');if(!email){{msg.textContent='Enter an email first.';return;}}if(!confirm('Revoke premium from '+email+'?'))return;var res=await fetch('/api/admin/revoke-premium',{{credentials:'include',method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email}})}});var d=await res.json();if(d.error){{msg.style.color='#fb7185';msg.textContent=d.error;}}else{{msg.style.color='#fb7185';msg.textContent='Premium revoked from '+d.email;setTimeout(()=>location.reload(),1500);}}}}
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

@app.route("/api/admin/balance")
def admin_balance():
    if not admin_allowed():
        return jsonify({"error": "Unauthorized"}), 401
    paystack_bal, err = paystack_get_balance()
    total_owed = paystack_get_total_pending_wallets()
    reserve = int(os.getenv("ADMIN_RESERVE_KOBO", "50000"))
    safe_to_withdraw = max(0, paystack_bal - total_owed - reserve)
    return jsonify({
        "paystack_balance_ngn": paystack_bal / 100,
        "total_owed_referrers_ngn": total_owed / 100,
        "admin_reserve_ngn": reserve / 100,
        "safe_to_withdraw_ngn": safe_to_withdraw / 100,
        "error": err
    })

@app.route("/api/admin/grant-premium", methods=["POST"])
def admin_grant_premium():
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
# NOTE: All HTML constants below are RAW strings (r""") so that JavaScript
# escape sequences like \n inside the embedded <script> blocks are served
# to the browser exactly as written. Without the r prefix, Python converts
# \n into a real newline, which breaks the JavaScript with a syntax error
# and silently kills every button on the page.


DIAG_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TikGenius Diagnostics</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#060a12;color:#e8f0fe;font-family:'Inter',system-ui,sans-serif;min-height:100vh;padding:20px}
.wrap{max-width:720px;margin:0 auto}
.header{margin-bottom:24px}
.header h1{font-size:1.5rem;font-weight:800;margin-bottom:4px}
.header p{color:#607898;font-size:.875rem}
.run-btn{background:linear-gradient(135deg,#00ffcc,#00aaff);border:none;color:#040e18;font-weight:800;font-size:.95rem;padding:12px 28px;border-radius:12px;cursor:pointer;font-family:inherit;margin-bottom:24px;transition:all .2s}
.run-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,255,200,.3)}
.run-btn:disabled{opacity:.5;transform:none}
.timestamp{font-size:.75rem;color:#607898;margin-bottom:16px;display:none}
.card{background:#0b1526;border:1px solid #162235;border-radius:14px;overflow:hidden;margin-bottom:10px}
.card-row{display:flex;align-items:center;gap:12px;padding:14px 16px}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot.ok{background:#00d68f}
.dot.fail{background:#ff4d6d}
.dot.spin{background:#ffb800;animation:pulse .8s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}
.check-name{font-size:.9rem;font-weight:600;flex:1}
.check-ms{font-size:.72rem;color:#607898;white-space:nowrap}
.check-detail{padding:0 16px 14px 38px;font-size:.8rem;line-height:1.6}
.check-detail.ok{color:#4dd9a8}
.check-detail.fail{color:#ff8099}
.summary{background:linear-gradient(135deg,rgba(0,255,200,.08),rgba(0,170,255,.05));border:1px solid rgba(0,255,200,.2);border-radius:14px;padding:18px;margin-bottom:16px;display:none}
.summary.show{display:block}
.summary h3{font-weight:800;font-size:1rem;margin-bottom:6px}
.summary p{color:#a0b8d0;font-size:.85rem;line-height:1.6}
.summary.all-ok{border-color:rgba(0,214,143,.3);background:rgba(0,214,143,.06)}
.summary.has-fail{border-color:rgba(255,77,109,.3);background:rgba(255,77,109,.06)}
.tip-box{background:#0b1526;border:1px solid #162235;border-radius:14px;padding:16px;margin-bottom:10px}
.tip-box h4{font-size:.8rem;font-weight:700;color:#ffb800;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px}
.tip-box p{font-size:.82rem;color:#a0b8d0;line-height:1.6}
.tip-box code{background:#060a12;padding:2px 7px;border-radius:5px;font-family:monospace;font-size:.8rem;color:#00ffcc}
.nav-links{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.nav-link{text-decoration:none;color:#607898;font-size:.8rem;font-weight:600;padding:6px 12px;border:1px solid #162235;border-radius:8px;transition:all .2s}
.nav-link:hover{border-color:rgba(0,255,200,.3);color:#00ffcc}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>TikGenius Diagnostics</h1>
    <p>Runs live checks on your server — database, API keys, sessions, config.</p>
  </div>
  <div class="nav-links">
    <a class="nav-link" href="/">Back to Studio</a>
    <a class="nav-link" href="/health">Health Check</a>
    <a class="nav-link" href="/admin">Admin Panel</a>
  </div>
  <button class="run-btn" id="runBtn" onclick="runDiag()">Run Diagnostics</button>
  <div class="summary" id="summary"></div>
  <div class="timestamp" id="ts"></div>
  <div id="results"></div>
  <div class="tip-box" style="margin-top:16px">
    <h4>Most common fix needed</h4>
    <p>If diagnostics pass but the site still feels broken, the #1 cause is <code>SECRET_KEY</code> not set in Railway. Add it in Railway > Variables: <code>SECRET_KEY = any-long-random-string</code>. Without it, every deploy logs everyone out.</p>
  </div>
</div>
<script>
async function runDiag() {
  var btn = document.getElementById('runBtn');
  var res = document.getElementById('results');
  var sum = document.getElementById('summary');
  btn.disabled = true; btn.textContent = 'Running...';
  sum.className = 'summary'; sum.style.display = 'none';
  res.innerHTML = '';

  // Show placeholder cards
  var checks = [
    'Database connection','SECRET_KEY / Sessions','Groq API Key (AI generation)',
    'Paystack Keys (payments)','DATABASE_URL format','Groq live API call',
    'Session cookie config','Active session (are you logged in?)',
    'Database tables','RapidAPI Key (TikTok downloader)'
  ];
  checks.forEach(function(name) {
    res.innerHTML += '<div class="card"><div class="card-row"><div class="dot spin"></div><span class="check-name">'+name+'</span><span class="check-ms">running...</span></div></div>';
  });

  try {
    var r = await fetch('/api/diag/run', {credentials:'include'});
    var d = await r.json();
    res.innerHTML = '';
    d.results.forEach(function(c) {
      var cls = c.ok ? 'ok' : 'fail';
      res.innerHTML += '<div class="card">'+
        '<div class="card-row">'+
        '<div class="dot '+cls+'"></div>'+
        '<span class="check-name">'+c.name+'</span>'+
        '<span class="check-ms">'+c.ms+'ms</span>'+
        '</div>'+
        '<div class="check-detail '+cls+'">'+escHtml(c.detail)+'</div>'+
        '</div>';
    });
    var fails = d.results.filter(function(r){return !r.ok;});
    sum.style.display = 'block';
    if (d.all_ok) {
      sum.className = 'summary show all-ok';
      sum.innerHTML = '<h3>All checks passed</h3><p>Everything looks good. If the site is still broken, check that SECRET_KEY is set in Railway variables and try clearing your browser cookies.</p>';
    } else {
      sum.className = 'summary show has-fail';
      sum.innerHTML = '<h3>'+fails.length+' issue'+(fails.length>1?'s':'')+' found</h3><p>'+
        fails.map(function(f){return '<strong>'+f.name+':</strong> '+escHtml(f.detail);}).join('<br>')+
        '</p>';
    }
    document.getElementById('ts').style.display = 'block';
    document.getElementById('ts').textContent = 'Last run: '+new Date(d.timestamp+'Z').toLocaleString();
  } catch(e) {
    res.innerHTML = '<div class="card"><div class="card-row"><div class="dot fail"></div><span class="check-name">Could not reach /api/diag/run</span></div><div class="check-detail fail">'+e.message+'</div></div>';
  }
  btn.disabled = false; btn.textContent = 'Run Again';
}

function escHtml(s){return String(s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

runDiag();
</script>
</body>
</html>"""

STUDIO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>TikGenius - AI Content Studio</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--sidebar:#111118;--card:#16161e;--border:#1e1e2e;
  --text:#f4f4f8;--muted:#6b6b80;--accent:#00ffcc;--accent2:#00aaff;
  --gold:#ffb800;--danger:#ff4d6d;--green:#00c896;
  --radius:14px;--font:'Inter',system-ui,sans-serif;--font-h:'Space Grotesk',system-ui,sans-serif;
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--font);-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
.nav{height:52px;min-height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;background:rgba(10,10,15,.95);border-bottom:1px solid var(--border);position:relative;z-index:100;flex-shrink:0}
.logo{font-family:var(--font-h);font-weight:800;font-size:1.15rem;letter-spacing:-.03em;display:flex;align-items:center;gap:8px;color:var(--text);text-decoration:none}
.logo em{color:var(--accent);font-style:normal}
.logo svg{flex-shrink:0}
.nav-center{position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:6px}
.nav-pill{background:rgba(0,255,204,.07);border:1px solid rgba(0,255,204,.15);color:var(--accent);padding:5px 12px;border-radius:100px;font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.nav-right{display:flex;gap:8px;align-items:center}
.nav-btn{padding:7px 14px;border-radius:10px;font-size:.8rem;font-weight:700;cursor:pointer;font-family:var(--font);border:none;transition:all .18s;text-decoration:none;white-space:nowrap}
.nav-btn.primary{background:var(--accent);color:#050a08}
.nav-btn.primary:hover{box-shadow:0 0 18px rgba(0,255,204,.35)}
.nav-btn.ghost{background:transparent;border:1px solid var(--border);color:var(--muted)}
.nav-btn.ghost:hover{border-color:var(--accent);color:var(--accent)}
.nav-btn.earn{background:linear-gradient(135deg,#ffb800,#ff8c00);color:#050a08;border:none}
.nav-btn.earn:hover{box-shadow:0 0 18px rgba(255,184,0,.4)}
.body{display:flex;flex:1;overflow:hidden}
.sidebar{width:240px;min-width:240px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0}
.sidebar-inner{padding:14px;display:flex;flex-direction:column;gap:8px;flex:1}
.plan-box{background:linear-gradient(135deg,#0d1f2d,#0a1520);border:1px solid rgba(0,255,204,.15);border-radius:var(--radius);padding:14px}
.plan-box .plan-name{font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin-bottom:4px}
.plan-box .plan-email{font-size:.78rem;color:var(--muted);margin-bottom:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.usage-bar-bg{height:4px;background:rgba(255,255,255,.08);border-radius:99px;overflow:hidden;margin-bottom:6px}
.usage-bar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:99px;transition:width .4s}
.usage-text{font-size:.72rem;color:var(--muted)}
.upgrade-box{background:linear-gradient(135deg,rgba(0,255,204,.1),rgba(255,184,0,.07));border:1px solid rgba(255,184,0,.25);border-radius:var(--radius);padding:12px;display:none}
.upgrade-box.show{display:block}
.upgrade-box p{font-size:.78rem;color:var(--muted);line-height:1.5;margin-bottom:8px}
.upgrade-box button{width:100%;padding:9px;background:linear-gradient(135deg,var(--accent),var(--gold));border:none;border-radius:9px;color:#050a08;font-weight:800;font-size:.82rem;font-family:var(--font);cursor:pointer}
.s-divider{height:1px;background:var(--border);margin:4px 0}
.s-item{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:10px;text-decoration:none;font-size:.85rem;font-weight:500;color:var(--muted);border:1px solid transparent;transition:all .15s;cursor:pointer;background:transparent;font-family:var(--font);width:100%;text-align:left}
.s-item:hover{background:var(--card);color:var(--text);border-color:var(--border)}
.s-item.accent{color:var(--accent);background:rgba(0,255,204,.05);border-color:rgba(0,255,204,.12)}
.s-item.gold{color:var(--gold);background:rgba(255,184,0,.05);border-color:rgba(255,184,0,.15)}
.s-label{font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding:4px 10px 2px}
.hist-scroll{flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:4px;max-height:220px}
.hist-item{padding:8px 10px;background:var(--card);border:1px solid var(--border);border-radius:9px;cursor:pointer;transition:border-color .15s}
.hist-item:hover{border-color:rgba(0,255,204,.2)}
.hist-item b{display:block;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600;color:var(--text)}
.hist-item span{font-size:.68rem;color:var(--muted);margin-top:1px;display:block}
.hist-empty{font-size:.75rem;color:var(--muted);text-align:center;padding:12px 8px;line-height:1.5}
.sidebar-footer{padding:12px 14px;border-top:1px solid var(--border)}
.logout-btn{width:100%;padding:8px;background:transparent;border:1px solid var(--border);border-radius:9px;color:var(--muted);font-family:var(--font);font-size:.78rem;cursor:pointer;transition:all .2s}
.logout-btn:hover{color:var(--danger);border-color:rgba(255,77,109,.3)}
.chat-main{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative}
.chat-messages{flex:1;overflow-y:auto;padding:20px 16px;display:flex;flex-direction:column;gap:12px;scroll-behavior:smooth}
.chat-messages::-webkit-scrollbar{width:4px}
.chat-messages::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px}
.welcome{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:32px 20px;text-align:center;gap:16px}
.welcome-icon{width:56px;height:56px;background:linear-gradient(135deg,rgba(0,255,204,.15),rgba(0,170,255,.1));border:1px solid rgba(0,255,204,.25);border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:1.6rem}
.welcome h2{font-family:var(--font-h);font-weight:800;font-size:clamp(1.4rem,4vw,1.9rem);letter-spacing:-.04em}
.welcome h2 span{background:linear-gradient(130deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.welcome p{color:var(--muted);font-size:.9rem;line-height:1.65;max-width:420px}
.welcome-pills{display:flex;flex-wrap:wrap;gap:7px;justify-content:center;max-width:480px}
.wpill{background:var(--card);border:1px solid var(--border);border-radius:100px;padding:5px 12px;font-size:.75rem;color:var(--muted);cursor:pointer;transition:all .18s}
.wpill:hover{border-color:var(--accent);color:var(--accent);background:rgba(0,255,204,.05)}
.msg{display:flex;gap:10px;max-width:800px;width:100%;animation:fadeUp .25s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.msg.user{align-self:flex-end;flex-direction:row-reverse}
.msg-avatar{width:30px;height:30px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:800;flex-shrink:0;margin-top:2px}
.msg.ai .msg-avatar{background:linear-gradient(135deg,rgba(0,255,204,.2),rgba(0,170,255,.15));border:1px solid rgba(0,255,204,.2);color:var(--accent)}
.msg.user .msg-avatar{background:rgba(255,255,255,.08);border:1px solid var(--border);color:var(--muted)}
.msg-body{flex:1;min-width:0}
.msg-name{font-size:.68rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;margin-bottom:5px}
.msg.ai .msg-name{color:var(--accent)}
.msg.user .msg-name{color:var(--muted);text-align:right}
.msg-bubble{padding:12px 15px;border-radius:14px;font-size:.9rem;line-height:1.65}
.msg.ai .msg-bubble{background:var(--card);border:1px solid var(--border);color:var(--text);border-top-left-radius:4px;word-wrap:break-word}
.msg.user .msg-bubble{background:rgba(0,255,204,.09);border:1px solid rgba(0,255,204,.18);color:var(--text);border-top-right-radius:4px}
.msg-copy{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:7px;padding:4px 10px;font-size:.7rem;font-weight:700;font-family:var(--font);cursor:pointer;transition:all .2s;margin-top:6px}
.msg-copy:hover{border-color:var(--accent);color:var(--accent)}
.thinking-msg{display:none;align-items:center;gap:10px;padding:12px 15px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);color:var(--muted);font-size:.85rem;max-width:800px}
.thinking-msg.show{display:flex}
.dots{display:flex;gap:4px}
.dots span{width:6px;height:6px;border-radius:50%;background:var(--accent);animation:bounce .9s ease-in-out infinite}
.dots span:nth-child(2){animation-delay:.15s}
.dots span:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,80%,100%{transform:scale(.5);opacity:.3}40%{transform:scale(1);opacity:1}}
.err-msg{display:none;padding:10px 14px;background:rgba(255,77,109,.08);border:1px solid rgba(255,77,109,.2);border-radius:10px;font-size:.83rem;color:#ff8099;max-width:800px}
.err-msg.show{display:block}
.upgrade-banner{display:none;padding:14px 16px;background:linear-gradient(135deg,rgba(0,255,204,.07),rgba(255,184,0,.05));border:1px solid rgba(255,184,0,.25);border-radius:var(--radius);max-width:800px}
.upgrade-banner.show{display:block}
.upgrade-banner h4{font-family:var(--font-h);font-weight:800;font-size:.9rem;margin-bottom:4px}
.upgrade-banner p{color:var(--muted);font-size:.8rem;margin-bottom:10px;line-height:1.5}
.upgrade-banner button{padding:9px 18px;background:linear-gradient(135deg,var(--accent),var(--gold));border:none;border-radius:9px;color:#050a08;font-weight:800;font-size:.85rem;font-family:var(--font);cursor:pointer}
.input-bar{padding:12px 16px;border-top:1px solid var(--border);background:rgba(10,10,15,.98);flex-shrink:0}
.input-inner{max-width:800px;margin:0 auto;display:flex;flex-direction:column;gap:8px}
.input-row{display:flex;gap:8px;align-items:flex-end}
.chat-input{flex:1;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:12px;padding:12px 15px;font-size:.9rem;font-family:var(--font);outline:none;resize:none;min-height:48px;max-height:160px;line-height:1.5;transition:border-color .2s}
.chat-input:focus{border-color:rgba(0,255,204,.35);box-shadow:0 0 0 3px rgba(0,255,204,.05)}
.chat-input::placeholder{color:var(--muted)}
.submit-btn{width:44px;height:44px;background:var(--accent);border:none;border-radius:11px;color:#050a08;font-size:1.1rem;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:all .18s}
.submit-btn:hover{box-shadow:0 0 16px rgba(0,255,204,.35)}
.submit-btn:disabled{opacity:.4}
.input-meta{display:flex;justify-content:space-between;align-items:center}
.input-hint{font-size:.72rem;color:var(--muted)}
.input-meta-right{display:flex;gap:12px;align-items:center}
.newchat-mini{background:transparent;border:none;color:var(--muted);font-size:.72rem;font-weight:700;cursor:pointer;font-family:var(--font);padding:2px 4px;transition:color .2s}
.newchat-mini:hover{color:var(--accent)}
.region-sel{background:transparent;border:none;color:var(--muted);font-size:.72rem;font-family:var(--font);cursor:pointer;outline:none;padding:2px 4px}
.region-sel option{background:var(--card)}
.profile-btn{width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#050a08;font-weight:800;font-size:.8rem;cursor:pointer;display:none;align-items:center;justify-content:center;flex-shrink:0;transition:all .2s;font-family:var(--font)}
.profile-btn:hover{box-shadow:0 0 14px rgba(0,255,204,.4);transform:scale(1.05)}
.profile-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:200;backdrop-filter:blur(4px)}
.profile-backdrop.open{display:block}
.profile-panel{position:fixed;right:0;top:0;bottom:0;width:88%;max-width:340px;background:var(--sidebar);border-left:1px solid var(--border);z-index:210;overflow-y:auto;padding:0;display:flex;flex-direction:column;transform:translateX(100%);transition:transform .28s ease}
.profile-panel.open{transform:translateX(0)}
.profile-head{padding:20px 18px 16px;background:linear-gradient(135deg,#0d1f2d,#0a1520);border-bottom:1px solid var(--border)}
.profile-avatar{width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-weight:800;font-size:1.2rem;color:#050a08;margin-bottom:12px}
.profile-name{font-family:var(--font-h);font-weight:700;font-size:1rem;margin-bottom:2px}
.profile-email{font-size:.78rem;color:var(--muted);margin-bottom:10px;word-break:break-all}
.profile-plan{display:inline-flex;align-items:center;gap:5px;background:rgba(0,255,204,.1);border:1px solid rgba(0,255,204,.2);color:var(--accent);padding:4px 10px;border-radius:100px;font-size:.72rem;font-weight:700}
.profile-plan.pro{background:rgba(255,184,0,.1);border-color:rgba(255,184,0,.3);color:var(--gold)}
.profile-body{padding:16px 18px;display:flex;flex-direction:column;gap:12px;flex:1}
.profile-section{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}
.profile-section-head{padding:10px 14px;background:rgba(0,0,0,.2);border-bottom:1px solid var(--border);font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.profile-section-body{padding:14px}
.stat-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.stat-row:last-child{border-bottom:none;padding-bottom:0}
.stat-row .stat-label{font-size:.8rem;color:var(--muted)}
.stat-row .stat-val{font-size:.9rem;font-weight:700;color:var(--text)}
.stat-row .stat-val.green{color:var(--accent)}
.stat-row .stat-val.gold{color:var(--gold)}
.earn-highlight{background:linear-gradient(135deg,rgba(255,184,0,.12),rgba(255,140,0,.08));border:1px solid rgba(255,184,0,.3);border-radius:12px;padding:14px;margin-bottom:4px}
.earn-highlight .earn-amount{font-family:var(--font-h);font-size:2rem;font-weight:800;color:var(--gold);line-height:1}
.earn-highlight .earn-label{font-size:.72rem;color:var(--muted);margin-top:4px;font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.ref-link-box{display:flex;gap:6px;margin-top:8px}
.ref-link-input{flex:1;background:#0d0d14;border:1px solid var(--border);color:var(--accent);padding:8px 10px;border-radius:8px;font-size:.72rem;font-family:var(--font);outline:none;min-width:0}
.ref-copy-btn{background:var(--accent);border:none;border-radius:8px;color:#050a08;font-weight:800;font-size:.72rem;padding:8px 10px;cursor:pointer;white-space:nowrap;font-family:var(--font);transition:all .2s}
.ref-copy-btn:hover{box-shadow:0 0 12px rgba(0,255,204,.3)}
.profile-action-btn{width:100%;padding:11px;border-radius:10px;font-size:.85rem;font-weight:700;cursor:pointer;font-family:var(--font);transition:all .2s;text-decoration:none;display:block;text-align:center;margin-bottom:4px}
.profile-action-btn.earn{background:linear-gradient(135deg,rgba(255,184,0,.15),rgba(255,184,0,.08));border:1px solid rgba(255,184,0,.3);color:var(--gold)}
.profile-action-btn.upgrade{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#050a08}
.profile-action-btn.danger{background:transparent;border:1px solid rgba(255,77,109,.25);color:var(--danger)}
.profile-action-btn:hover{transform:translateY(-1px)}
.profile-close{position:absolute;top:16px;right:16px;background:rgba(255,255,255,.07);border:none;color:var(--muted);border-radius:8px;padding:6px 10px;cursor:pointer;font-size:.9rem;font-family:var(--font)}
.modal-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:300;align-items:center;justify-content:center;backdrop-filter:blur(8px);padding:16px}
.modal-backdrop.open{display:flex}
.modal{background:#13131a;border:1px solid rgba(0,255,204,.15);border-radius:20px;padding:22px;width:100%;max-width:380px}
.modal h2{font-family:var(--font-h);font-weight:800;font-size:1.25rem;margin-bottom:.25rem}
.modal-sub{color:var(--muted);font-size:.8rem;margin-bottom:1.1rem;line-height:1.5}
.modal-tabs{display:flex;background:#0d0d14;border-radius:9px;padding:3px;gap:3px;margin-bottom:1.1rem}
.modal-tab{flex:1;padding:.45rem;border:none;border-radius:7px;background:transparent;color:var(--muted);font-size:.8rem;font-weight:600;cursor:pointer;transition:all .2s;font-family:var(--font)}
.modal-tab.on{background:var(--card);color:var(--text)}
.fg{margin-bottom:.85rem}
.fg label{display:block;font-size:.7rem;font-weight:700;color:var(--muted);margin-bottom:.35rem;letter-spacing:.04em;text-transform:uppercase}
.fg input,.fg select{width:100%;background:#0d0d14;border:1px solid var(--border);color:var(--text);padding:.65rem .85rem;border-radius:8px;font-size:.85rem;font-family:var(--font);outline:none;transition:border-color .2s}
.fg input:focus,.fg select:focus{border-color:var(--accent)}
.modal-err{display:none;color:#ff8099;font-size:.76rem;margin-bottom:.75rem;background:rgba(255,77,109,.07);border:1px solid rgba(255,77,109,.2);padding:.5rem .8rem;border-radius:7px}
.modal-btn{width:100%;padding:.75rem;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:9px;color:#050a08;font-size:.875rem;font-weight:800;cursor:pointer;font-family:var(--font);margin-bottom:.55rem;transition:all .2s}
.modal-btn:hover{transform:translateY(-1px);box-shadow:0 5px 18px rgba(0,255,204,.25)}
.modal-cancel{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.76rem;font-family:var(--font);width:100%;padding:.3rem}
@media(max-width:768px){
  html,body{overflow:auto}
  .body{flex-direction:column;overflow:visible}
  .sidebar{display:none}
  .chat-main{overflow:visible}
  .chat-messages{overflow:visible;min-height:40vh;padding:14px 12px}
  .input-bar{position:sticky;bottom:0;z-index:50}
  .nav-center{display:none}
  .welcome{padding:20px 16px;gap:12px}
  .welcome h2{font-size:1.3rem}
}
</style>
</head>
<body>

<nav class="nav">
  <a class="logo" href="/">
    <svg width="24" height="24" viewBox="0 0 200 200" fill="none"><defs><linearGradient id="lg1" x1="60" y1="50" x2="100" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00ffcc"/><stop offset="1" stop-color="rgba(0,255,200,.7)"/></linearGradient><linearGradient id="lg2" x1="100" y1="55" x2="145" y2="155" gradientUnits="userSpaceOnUse"><stop stop-color="#00aaff"/><stop offset="1" stop-color="#00ffcc"/></linearGradient></defs><rect x="52" y="58" width="52" height="7" rx="2" fill="url(#lg1)"/><rect x="74" y="65" width="8" height="70" rx="2" fill="url(#lg1)"/><path d="M120 72 Q148 58 155 85 Q158 100 152 115 Q144 138 120 142 Q96 146 88 125 Q82 110 88 95 Q94 78 110 72" stroke="url(#lg2)" stroke-width="7" fill="none" stroke-linecap="round"/><rect x="118" y="104" width="28" height="6.5" rx="2" fill="url(#lg2)"/></svg>
    Tik<em>Genius</em>
  </a>
  <div class="nav-right">
    <a class="nav-btn ghost" href="/download">Downloader</a>
    <button class="nav-btn ghost" id="navLogin" onclick="openModal('login')" style="display:none">Log In</button>
    <button class="nav-btn primary" id="navSignup" onclick="openModal('signup')" style="display:none">Sign Up Free</button>
    <button class="nav-btn earn" id="navEarn" onclick="handleEarnClick()">Start Earning</button>
  </div>
</nav>

<div class="body">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-inner">
      <div class="plan-box" id="planBox" style="display:none">
        <div class="plan-name" id="planName">Free Plan</div>
        <div class="plan-email" id="planEmail"></div>
        <div class="usage-bar-bg"><div class="usage-bar" id="usageBar" style="width:100%"></div></div>
        <div class="usage-text" id="usageText">5 / 5 left today</div>
      </div>
      <div class="upgrade-box" id="upgradeBox">
        <p>You have used all your free messages. Upgrade for unlimited.</p>
        <button onclick="doUpgrade()">Upgrade - N2,000/mo</button>
      </div>
      <div class="s-divider"></div>
      <button class="s-item" onclick="resetChat()">+ New Chat</button>
      <a class="s-item accent" href="/download">TikTok Downloader</a>
      <button class="s-item gold" id="earnLink" style="display:none" onclick="openProfile()">Earn N500/Referral</button>
      <div class="s-divider"></div>
      <div class="s-label" style="display:flex;justify-content:space-between;align-items:center;padding-right:4px">
        <span>History</span>
        <button onclick="clearHistory()" style="background:transparent;border:none;color:var(--muted);font-size:.68rem;cursor:pointer;font-family:var(--font)">Clear</button>
      </div>
      <div class="hist-scroll" id="histScroll">
        <div class="hist-empty">Chat with TikGenius to see history</div>
      </div>
    </div>
    <div class="sidebar-footer">
      <button class="logout-btn" id="logoutBtn" style="display:none" onclick="doLogout()">Log out</button>
    </div>
  </aside>

  <div class="chat-main">
    <div class="chat-messages" id="chatMessages">
      <div class="welcome" id="welcomeState">
        <div class="welcome-icon">*</div>
        <h2>What do you want to <span>create today?</span></h2>
        <p>Chat with TikGenius like a creative partner. Ask for captions, hooks, scripts, hashtags, content ideas, or growth advice — anything content.</p>
        <div id="guestPrompt" style="display:none;background:rgba(0,255,204,.06);border:1px solid rgba(0,255,204,.2);border-radius:12px;padding:14px 18px;text-align:center;margin-top:4px">
          <p style="color:var(--muted);font-size:.85rem;margin-bottom:10px;line-height:1.5">Create a free account to start chatting</p>
          <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
            <button onclick="openModal('signup')" style="padding:9px 20px;background:var(--accent);border:none;border-radius:9px;color:#050a08;font-weight:800;font-size:.85rem;font-family:var(--font);cursor:pointer">Sign Up Free</button>
            <button onclick="openModal('login')" style="padding:9px 20px;background:transparent;border:1px solid var(--border);border-radius:9px;color:var(--muted);font-weight:700;font-size:.85rem;font-family:var(--font);cursor:pointer">Log In</button>
          </div>
        </div>
        <div class="welcome-pills">
          <span class="wpill" onclick="fillExample(this)">Give me 10 hooks for my fitness journey</span>
          <span class="wpill" onclick="fillExample(this)">Write a TikTok script about making money online</span>
          <span class="wpill" onclick="fillExample(this)">What should I post to grow my food page?</span>
          <span class="wpill" onclick="fillExample(this)">Captions for motivational content for students</span>
          <span class="wpill" onclick="fillExample(this)">How do I get my first 1,000 followers?</span>
          <span class="wpill" onclick="fillExample(this)">Hashtags for fashion and style content</span>
        </div>
      </div>
    </div>

    <div style="padding:0 16px 8px;max-width:832px;margin:0 auto;width:100%">
      <div class="thinking-msg" id="thinkingMsg">
        <div class="dots"><span></span><span></span><span></span></div>
        <span id="thinkingText">TikGenius is thinking...</span>
      </div>
      <div class="err-msg" id="errMsg"></div>
      <div class="upgrade-banner" id="upgradeBanner">
        <h4>Free messages used up</h4>
        <p>Upgrade to Premium for unlimited messages, every day.</p>
        <button onclick="doUpgrade()">Upgrade to Premium - N2,000/mo</button>
      </div>
    </div>
  </div>
</div>

<div class="input-bar">
  <div class="input-inner">
    <div class="input-row">
      <textarea class="chat-input" id="chatInput" rows="1"
        placeholder="Ask TikGenius anything about your content..."
        onkeydown="onKey(event)" oninput="autoResize(this)"></textarea>
      <button class="submit-btn" id="submitBtn" onclick="handleSend()" title="Send">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z"/></svg>
      </button>
    </div>
    <div class="input-meta">
      <span class="input-hint" id="inputHint">Type a message and press Enter</span>
      <div class="input-meta-right">
        <button class="newchat-mini" onclick="resetChat()">New Chat</button>
        <select class="region-sel" id="regionSel" onchange="saveRegion(this.value)">
          <option value="global">Global</option>
          <option value="nigeria">Nigeria</option>
          <option value="usa">USA</option>
          <option value="uk">UK</option>
          <option value="caribbean">Caribbean</option>
          <option value="eastafrica">East Africa</option>
          <option value="southafrica">South Africa</option>
        </select>
      </div>
    </div>
  </div>
</div>

<div class="profile-backdrop" id="profileBackdrop" onclick="closeProfile()"></div>
<div class="profile-panel" id="profilePanel">
  <button class="profile-close" onclick="closeProfile()">X</button>
  <div class="profile-head">
    <div class="profile-avatar" id="profAvatar">?</div>
    <div class="profile-name" id="profName">Your Account</div>
    <div class="profile-email" id="profEmail"></div>
    <div class="profile-plan" id="profPlan">Free Plan</div>
  </div>
  <div class="profile-body">
    <div class="earn-highlight">
      <div class="earn-amount" id="profBalance">N0</div>
      <div class="earn-label">Wallet Balance</div>
    </div>
    <div class="profile-section">
      <div class="profile-section-head">Referral Earnings</div>
      <div class="profile-section-body">
        <div class="stat-row"><span class="stat-label">Total earned</span><span class="stat-val gold" id="profTotalEarned">N0</span></div>
        <div class="stat-row"><span class="stat-label">Total withdrawn</span><span class="stat-val" id="profWithdrawn">N0</span></div>
        <div class="stat-row"><span class="stat-label">Paid referrals</span><span class="stat-val green" id="profPaidRefs">0 people</span></div>
        <div class="stat-row"><span class="stat-label">Pending referrals</span><span class="stat-val" id="profPendingRefs">0 pending</span></div>
        <div style="margin-top:12px;font-size:.72rem;color:var(--muted);margin-bottom:6px;font-weight:600;letter-spacing:.06em;text-transform:uppercase">Your Referral Link</div>
        <div class="ref-link-box">
          <input class="ref-link-input" id="profRefLink" readonly value="Loading...">
          <button class="ref-copy-btn" onclick="copyRefLink()">Copy</button>
        </div>
        <p style="font-size:.72rem;color:var(--muted);margin-top:8px;line-height:1.5">Share this link. When someone upgrades to Premium through your link, N500 is added to your wallet instantly.</p>
      </div>
    </div>
    <div class="profile-section">
      <div class="profile-section-head">Daily Usage</div>
      <div class="profile-section-body">
        <div class="stat-row"><span class="stat-label">Messages today</span><span class="stat-val" id="profUsage">-</span></div>
        <div class="stat-row"><span class="stat-label">Plan</span><span class="stat-val green" id="profPlanTxt">Free (5/day)</span></div>
      </div>
    </div>
    <a class="profile-action-btn earn" href="/refer">Withdraw Earnings</a>
    <a class="profile-action-btn" href="/download" style="background:transparent;border:1px solid var(--border);color:var(--muted)">TikTok Downloader</a>
    <button class="profile-action-btn upgrade" id="profUpgradeBtn" onclick="doUpgrade()" style="display:none">Upgrade to Premium - N2,000/mo</button>
    <button class="profile-action-btn danger" onclick="doLogout()">Log out</button>
  </div>
</div>

<div class="modal-backdrop" id="authBackdrop">
  <div class="modal">
    <h2 id="modalH">Create your account</h2>
    <p class="modal-sub" id="modalSub">5 free AI messages per day, no card needed.</p>
    <div class="modal-tabs">
      <button class="modal-tab on" id="tabA" onclick="switchTab('signup')">Sign Up</button>
      <button class="modal-tab" id="tabB" onclick="switchTab('login')">Log In</button>
    </div>
    <div id="fmSignup">
      <div class="fg"><label>Email</label><input type="email" id="sEmail" placeholder="you@example.com" autocomplete="email"></div>
      <div class="fg"><label>Password</label><input type="password" id="sPass" placeholder="Min 6 characters" autocomplete="new-password"></div>
      <div class="fg"><label>Your Region</label>
        <select id="sRegion">
          <option value="global">Global</option>
          <option value="nigeria">Nigerian</option>
          <option value="usa">American</option>
          <option value="uk">British</option>
          <option value="caribbean">Caribbean</option>
          <option value="eastafrica">East African</option>
          <option value="southafrica">South African</option>
        </select>
      </div>
      <div class="modal-err" id="sErr"></div>
      <button class="modal-btn" onclick="doSignup()">Create Account</button>
    </div>
    <div id="fmLogin" style="display:none">
      <div class="fg"><label>Email</label><input type="email" id="lEmail" placeholder="you@example.com" autocomplete="email"></div>
      <div class="fg"><label>Password</label><input type="password" id="lPass" placeholder="Your password" autocomplete="current-password"></div>
      <div class="modal-err" id="lErr"></div>
      <button class="modal-btn" onclick="doLogin()">Log In</button>
    </div>
    <button class="modal-cancel" onclick="closeModal()">Cancel</button>
  </div>
</div>

<script>
var user = null;
var chatHistory = [];
var conversationId = null;
var sending = false;
var urlRef = new URLSearchParams(location.search).get('ref') || '';

async function init() {
  try {
    var r = await fetch('/api/me', {credentials:'include'});
    if (r.ok) {
      user = await r.json();
      applyUser();
      loadConversations(true);
    } else {
      showGuest();
    }
  } catch(e) {
    console.error('[TikGenius] init() fetch error:', e);
    showGuest();
  }
  if (location.search.includes('payment=success')) {
    setTimeout(async function(){ user=null; await init(); }, 600);
  }
}

function applyUser() {
  var isPro = user.plan === 'pro';
  document.getElementById('navLogin').style.display = 'none';
  document.getElementById('navSignup').style.display = 'none';
  document.getElementById('planBox').style.display = 'block';
  document.getElementById('planName').textContent = isPro ? 'Premium' : 'Free Plan';
  document.getElementById('planEmail').textContent = user.email;
  document.getElementById('logoutBtn').style.display = 'block';
  document.getElementById('earnLink').style.display = 'flex';
  document.getElementById('regionSel').value = user.region || 'global';
  updateUsage(user.uses_remaining, user.unlimited);
}

function showGuest() {
  document.getElementById('navLogin').style.display = 'inline-flex';
  document.getElementById('navSignup').style.display = 'inline-flex';
  var gp = document.getElementById('guestPrompt');
  if (gp) gp.style.display = 'block';
}

function handleEarnClick() {
  if (!user) { openModal('signup'); return; }
  openProfile();
}

function updateUsage(rem, unlimited) {
  var pct = unlimited ? 100 : ((rem||0)/5*100);
  var txt = unlimited ? 'Unlimited' : ((rem||0) + ' / 5 left today');
  var ub = document.getElementById('usageBar'); if(ub) ub.style.width=pct+'%';
  var ut = document.getElementById('usageText'); if(ut) ut.textContent=txt;
  var show = !unlimited && (rem||0)<=0;
  var ub2 = document.getElementById('upgradeBox'); if(ub2) ub2.classList.toggle('show', show);
  var pu = document.getElementById('profUsage');
  if(pu) pu.textContent = unlimited ? 'Unlimited' : ((5-(rem||0)) + ' / 5 used');
  var pp = document.getElementById('profPlanTxt');
  if(pp) pp.textContent = unlimited ? 'Premium - Unlimited' : 'Free (5/day)';
  var pup = document.getElementById('profUpgradeBtn');
  if(pup) pup.style.display = (!unlimited) ? 'block' : 'none';
}

function handleSend() {
  var text = (document.getElementById('chatInput').value || '').trim();
  if (!text || sending) return;
  if (!user) {
    showErr('Please log in first to send a message.');
    sessionStorage.setItem('pendingIdea', text);
    openModal('signup');
    return;
  }
  sendMessage(text);
}

async function sendMessage(text) {
  hideErr(); hideBanner();
  var input = document.getElementById('chatInput');
  input.value = '';
  autoResize(input);
  var w = document.getElementById('welcomeState');
  if (w) w.style.display = 'none';
  addMsg('user', text);
  chatHistory.push({ role: 'user', content: text });
  sending = true;
  document.getElementById('submitBtn').disabled = true;
  setThinking(true, 'TikGenius is thinking...');
  try {
    var r = await fetch('/api/chat', { credentials:'include', method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ messages: chatHistory, region: document.getElementById('regionSel').value, conversation_id: conversationId }) });
    var d = await r.json();
    setThinking(false);
    sending = false;
    document.getElementById('submitBtn').disabled = false;
    if (d.error) {
      chatHistory.pop();
      showErr(d.error);
      if (r.status === 429) showBanner();
      return;
    }
    if (d.conversation_id) conversationId = d.conversation_id;
    chatHistory.push({ role: 'assistant', content: d.reply });
    addAiReply(d.reply);
    if (d.unlimited) updateUsage(null, true);
    else if (d.uses_remaining !== undefined && d.uses_remaining !== null) updateUsage(d.uses_remaining, false);
    loadConversations(false);
    document.getElementById('inputHint').textContent = 'Reply to keep the conversation going';
  } catch(e) {
    setThinking(false);
    sending = false;
    document.getElementById('submitBtn').disabled = false;
    chatHistory.pop();
    console.error('[TikGenius] sendMessage error:', e);
    showErr('Network error: ' + e.message + '. Check your connection and try again.');
  }
}

function formatAI(s) {
  var e = escHtml(s);
  e = e.replace(/\*\*(.+?)\*\*/g, '<b style="color:var(--accent)">$1</b>');
  return e.replace(/\n/g, '<br>');
}

function addAiReply(text) {
  var msgs = document.getElementById('chatMessages');
  var div = document.createElement('div');
  div.className = 'msg ai';
  div.innerHTML = '<div class="msg-avatar">TG</div>' +
    '<div class="msg-body"><div class="msg-name">TikGenius</div>' +
    '<div class="msg-bubble">' + formatAI(text) + '</div>' +
    '<button class="msg-copy">Copy</button></div>';
  div.querySelector('.msg-copy').addEventListener('click', function(){ copyRaw(this, text); });
  msgs.appendChild(div);
  div.scrollIntoView({ behavior:'smooth', block:'start' });
}

function resetChat() {
  chatHistory = [];
  conversationId = null;
  var msgs = document.getElementById('chatMessages');
  msgs.innerHTML = '<div class="welcome" id="welcomeState">' +
    '<div class="welcome-icon">*</div>' +
    '<h2>What do you want to <span>create today?</span></h2>' +
    '<p>Chat with TikGenius like a creative partner. Ask for captions, hooks, scripts, hashtags, content ideas, or growth advice — anything content.</p>' +
    '<div class="welcome-pills">' +
    '<span class="wpill" onclick="fillExample(this)">Give me 10 hooks for my fitness journey</span>' +
    '<span class="wpill" onclick="fillExample(this)">Write a TikTok script about making money online</span>' +
    '<span class="wpill" onclick="fillExample(this)">What should I post to grow my food page?</span>' +
    '<span class="wpill" onclick="fillExample(this)">Captions for motivational content for students</span>' +
    '<span class="wpill" onclick="fillExample(this)">How do I get my first 1,000 followers?</span>' +
    '<span class="wpill" onclick="fillExample(this)">Hashtags for fashion and style content</span>' +
    '</div></div>';
  document.getElementById('chatInput').value = '';
  document.getElementById('inputHint').textContent = 'Type a message and press Enter';
  document.getElementById('submitBtn').disabled = false;
  sending = false;
  hideErr(); hideBanner();
}

async function loadConversations(autoResume) {
  try {
    var r = await fetch('/api/conversations', {credentials:'include'});
    var d = await r.json();
    var items = d.items || [];
    var hs = document.getElementById('histScroll');
    if (hs) {
      if (!items.length) {
        hs.innerHTML = '<div class="hist-empty">Chat with TikGenius to see history</div>';
      } else {
        hs.innerHTML = '';
        items.forEach(function(item) {
          var div = document.createElement('div');
          div.className = 'hist-item';
          div.innerHTML = '<b>' + escHtml((item.title||'Untitled').slice(0,40)) + '</b>' +
            '<span>' + new Date(item.updated_at).toLocaleDateString() + '</span>';
          div.addEventListener('click', function() { loadConversation(item.id); });
          hs.appendChild(div);
        });
      }
    }
    if (autoResume && items.length && chatHistory.length === 0) {
      loadConversation(items[0].id);
    }
  } catch(e) {}
}

async function loadConversation(convId) {
  try {
    var r = await fetch('/api/conversations/' + convId, {credentials:'include'});
    var d = await r.json();
    if (d.error) return;
    conversationId = d.id;
    chatHistory = d.messages || [];
    var msgs = document.getElementById('chatMessages');
    msgs.innerHTML = '';
    chatHistory.forEach(function(m) {
      if (m.role === 'user') addMsg('user', m.content);
      else addAiReply(m.content);
    });
    msgs.scrollTop = msgs.scrollHeight;
    document.getElementById('inputHint').textContent = 'Reply to keep the conversation going';
    closeProfile();
  } catch(e) {}
}

async function clearHistory() {
  if (!confirm('Clear all history?')) return;
  await fetch('/api/conversations/clear', { method:'POST', credentials:'include' });
  conversationId = null;
  resetChat();
  loadConversations(false);
}

function addMsg(type, text) {
  var msgs = document.getElementById('chatMessages');
  var w = document.getElementById('welcomeState');
  if (w) w.style.display = 'none';
  var div = document.createElement('div');
  div.className = 'msg ' + type;
  var av = type==='ai' ? 'TG' : 'You';
  var name = type==='ai' ? 'TikGenius' : 'You';
  div.innerHTML = '<div class="msg-avatar">' + av + '</div>' +
    '<div class="msg-body"><div class="msg-name">' + name + '</div>' +
    '<div class="msg-bubble">' + escHtml(text).replace(/\n/g,'<br>') + '</div></div>';
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function setThinking(show, text) {
  var el = document.getElementById('thinkingMsg');
  el.classList.toggle('show', show);
  if (text) document.getElementById('thinkingText').textContent = text;
}

function showErr(msg) { var e=document.getElementById('errMsg'); e.textContent=msg; e.classList.add('show'); }
function hideErr() { document.getElementById('errMsg').classList.remove('show'); }
function showBanner() { document.getElementById('upgradeBanner').classList.add('show'); }
function hideBanner() { document.getElementById('upgradeBanner').classList.remove('show'); }

function fillExample(el) {
  document.getElementById('chatInput').value = el.textContent;
  autoResize(document.getElementById('chatInput'));
  document.getElementById('chatInput').focus();
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

function onKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
}

function escHtml(s) {
  return String(s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; });
}

function copyRaw(btn, text) {
  navigator.clipboard.writeText(text).then(function(){
    var orig = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(function(){ btn.textContent = orig; }, 1600);
  });
}

function openModal(tab) { document.getElementById('authBackdrop').classList.add('open'); switchTab(tab||'signup'); }
function closeModal() { document.getElementById('authBackdrop').classList.remove('open'); }
function switchTab(tab) {
  document.getElementById('fmSignup').style.display = tab==='signup' ? 'block' : 'none';
  document.getElementById('fmLogin').style.display = tab==='login' ? 'block' : 'none';
  document.getElementById('tabA').classList.toggle('on', tab==='signup');
  document.getElementById('tabB').classList.toggle('on', tab==='login');
  document.getElementById('modalH').textContent = tab==='signup' ? 'Create your account' : 'Welcome back';
  document.getElementById('modalSub').textContent = tab==='signup' ? '5 free AI messages per day, no card needed.' : 'Log in to your TikGenius account';
}

async function doSignup() {
  var email=document.getElementById('sEmail').value.trim();
  var pass=document.getElementById('sPass').value;
  var region=document.getElementById('sRegion').value;
  var err=document.getElementById('sErr'); err.style.display='none';
  var r=await fetch('/api/signup',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass,region:region,ref_code:urlRef})});
  var d=await r.json();
  if(d.error){err.textContent=d.error;err.style.display='block';return;}
  closeModal(); user=null; await init();
  var pending = sessionStorage.getItem('pendingIdea');
  if (pending) { sessionStorage.removeItem('pendingIdea'); document.getElementById('chatInput').value = pending; setTimeout(function(){ handleSend(); }, 300); }
}

async function doLogin() {
  var email=document.getElementById('lEmail').value.trim();
  var pass=document.getElementById('lPass').value;
  var err=document.getElementById('lErr'); err.style.display='none';
  var r=await fetch('/api/login',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass})});
  var d=await r.json();
  if(d.error){err.textContent=d.error;err.style.display='block';return;}
  closeModal(); user=null; await init();
  var pending = sessionStorage.getItem('pendingIdea');
  if (pending) { sessionStorage.removeItem('pendingIdea'); document.getElementById('chatInput').value = pending; setTimeout(function(){ handleSend(); }, 300); }
}

async function doLogout() { await fetch('/api/logout',{method:'POST',credentials:'include'}); location.reload(); }

document.getElementById('authBackdrop').addEventListener('click',function(e){if(e.target===this)closeModal();});

var upgInProgress = false;
async function doUpgrade() {
  if (!user) { openModal('signup'); return; }
  if (upgInProgress) return; upgInProgress = true;
  try {
    var r = await fetch('/api/upgrade',{method:'POST',credentials:'same-origin'});
    var d = await r.json();
    if (r.status===401) { openModal('login'); return; }
    if (d.url) { window.location.assign(d.url); return; }
    showErr(d.error || 'Could not open payment.');
  } catch(e) { showErr('Network error.'); }
  finally { upgInProgress = false; }
}

async function saveRegion(v) {
  if (!user) return;
  await fetch('/api/set-region',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({region:v})});
}

async function openProfile() {
  document.getElementById('profilePanel').classList.add('open');
  document.getElementById('profileBackdrop').classList.add('open');
  if (!user) return;
  var isPro = user.plan === 'pro';
  document.getElementById('profAvatar').textContent = (user.email||'U')[0].toUpperCase();
  document.getElementById('profName').textContent = user.email.split('@')[0];
  document.getElementById('profEmail').textContent = user.email;
  var planEl = document.getElementById('profPlan');
  planEl.textContent = isPro ? 'Premium' : 'Free Plan';
  planEl.className = 'profile-plan' + (isPro ? ' pro' : '');
  try {
    var r = await fetch('/api/referral/stats', {credentials:'include'});
    var d = await r.json();
    document.getElementById('profBalance').textContent = 'N' + (d.balance_ngn||0).toLocaleString();
    document.getElementById('profTotalEarned').textContent = 'N' + (d.total_earned_ngn||0).toLocaleString();
    document.getElementById('profWithdrawn').textContent = 'N' + (d.total_withdrawn_ngn||0).toLocaleString();
    document.getElementById('profPaidRefs').textContent = (d.paid_referrals||0) + ' people';
    document.getElementById('profPendingRefs').textContent = (d.pending_referrals||0) + ' pending';
    document.getElementById('profRefLink').value = d.referral_link || '';
  } catch(e) {
    console.error('[TikGenius] openProfile error:', e);
    document.getElementById('profBalance').textContent = 'Error: ' + e.message;
  }
}

function closeProfile() {
  document.getElementById('profilePanel').classList.remove('open');
  document.getElementById('profileBackdrop').classList.remove('open');
}

function copyRefLink() {
  var link = document.getElementById('profRefLink').value;
  navigator.clipboard.writeText(link).then(function(){
    var btn = document.querySelector('.ref-copy-btn');
    btn.textContent = 'Copied!';
    setTimeout(function(){ btn.textContent = 'Copy'; }, 1600);
  });
}

if (location.search.includes('login=1')) { setTimeout(function(){ if(!user) openModal('login'); }, 500); }
init();</script>
</body>
</html>"""

REFER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius - Earn with Referrals</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#03050a;--card:#0b1928;--border:#14253a;--text:#f0f8ff;--muted:#607a90;--accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--green:#22c55e;--danger:#fb7185}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:.85rem 1.4rem;position:sticky;top:0;z-index:100;background:rgba(3,5,10,.9);backdrop-filter:blur(18px);border-bottom:1px solid rgba(0,255,200,.07)}
.logo{display:flex;align-items:center;gap:.5rem;text-decoration:none;color:var(--text);font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.15rem;letter-spacing:-.03em}
.logo em{color:var(--accent);font-style:normal}
.nav-right{display:flex;gap:.65rem;align-items:center}
.nav-link{text-decoration:none;color:var(--muted);font-size:.875rem;font-weight:600;padding:.4rem .7rem;border-radius:8px;transition:color .2s}
.nav-link:hover{color:var(--accent)}
.btn-nav{background:var(--accent);color:#030e0a;border:none;padding:.45rem 1.1rem;border-radius:8px;font-size:.875rem;font-weight:700;cursor:pointer;text-decoration:none;font-family:'Inter',sans-serif;transition:all .2s}
.wrap{max-width:780px;margin:0 auto;padding:2rem 1.25rem 5rem}
.page-hero{text-align:center;padding:3rem 0 2rem}
.page-hero h1{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:clamp(2rem,7vw,3.2rem);letter-spacing:-.045em;margin-bottom:.8rem}
.page-hero h1 em{color:var(--gold);font-style:normal}
.page-hero p{color:var(--muted);font-size:1rem;line-height:1.7;max-width:520px;margin:0 auto}
.stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:18px;padding:16px;text-align:center}
.stat-card .val{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.8rem;letter-spacing:-.04em;margin-bottom:4px}
.stat-card .lbl{font-size:.72rem;color:var(--muted);font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.stat-card.highlight{border-color:rgba(255,184,0,.35);background:linear-gradient(135deg,rgba(255,184,0,.08),rgba(0,255,200,.05))}
.stat-card.highlight .val{color:var(--gold)}
.link-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.link-card h3{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.5rem}
.link-card p{color:var(--muted);font-size:.82rem;line-height:1.5;margin-bottom:1rem}
.link-box{display:flex;gap:8px}
.link-input{flex:1;background:#050e18;border:1px solid var(--border);color:var(--accent);padding:11px 14px;border-radius:11px;font-size:.82rem;font-family:'Inter',sans-serif;outline:none;min-width:0}
.copy-link-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:11px;color:#030e0a;font-weight:800;padding:11px 18px;font-size:.85rem;font-family:'Inter',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
.copy-link-btn:hover{transform:translateY(-1px);box-shadow:0 5px 20px rgba(0,255,200,.3)}
.share-btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.share-btn{background:#07111c;border:1px solid var(--border);color:var(--text);padding:9px 14px;border-radius:10px;font-size:.8rem;font-weight:700;font-family:'Inter',sans-serif;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:.4rem;transition:all .2s}
.share-btn:hover{border-color:rgba(0,255,200,.3);color:var(--accent)}
.how-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.how-card h3{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1rem;margin-bottom:1rem}
.how-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.how-step{text-align:center;padding:12px 8px}
.how-step .num{font-family:'Space Grotesk',sans-serif;font-size:1.8rem;font-weight:800;color:rgba(255,184,0,.3);line-height:1;margin-bottom:8px}
.how-step b{display:block;font-size:.85rem;margin-bottom:4px}
.how-step p{color:var(--muted);font-size:.75rem;line-height:1.5}
.withdraw-card{background:var(--card);border:1px solid rgba(255,184,0,.25);border-radius:20px;padding:20px;margin-bottom:14px}
.withdraw-card h3{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.4rem;color:var(--gold)}
.withdraw-card .sub{color:var(--muted);font-size:.82rem;margin-bottom:1.2rem;line-height:1.5}
.fg{margin-bottom:12px}
.fg label{display:block;font-size:.75rem;font-weight:700;color:var(--muted);margin-bottom:.4rem;letter-spacing:.04em;text-transform:uppercase}
.fg input,.fg select{width:100%;background:#050e18;border:1px solid var(--border);color:var(--text);padding:12px 14px;border-radius:11px;font-size:.9rem;font-family:'Inter',sans-serif;outline:none;transition:border-color .2s}
.fg input:focus,.fg select:focus{border-color:var(--gold);box-shadow:0 0 0 3px rgba(255,184,0,.07)}
.verify-row{display:flex;gap:8px}
.verify-row input{flex:1}
.verify-btn{background:#0b1928;color:var(--muted);border:1px solid var(--border);border-radius:11px;padding:12px 14px;font-size:.82rem;font-weight:700;font-family:'Inter',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
.verify-btn:hover{border-color:rgba(0,255,200,.3);color:var(--accent)}
.account-name-display{font-size:.82rem;color:var(--green);font-weight:700;margin-top:5px;min-height:18px}
.withdraw-btn{width:100%;padding:14px;background:linear-gradient(135deg,var(--gold),#ff8c00);border:none;border-radius:12px;color:#030e0a;font-weight:800;font-size:.95rem;font-family:'Inter',sans-serif;cursor:pointer;transition:all .2s;margin-top:4px}
.withdraw-btn:hover{transform:translateY(-1px);box-shadow:0 6px 24px rgba(255,184,0,.35)}
.withdraw-btn:disabled{opacity:.5;transform:none;box-shadow:none}
.locked-msg{background:rgba(255,184,0,.06);border:1px solid rgba(255,184,0,.2);border-radius:12px;padding:14px;font-size:.85rem;color:var(--muted);line-height:1.6;text-align:center}
.locked-msg strong{color:var(--gold);display:block;margin-bottom:4px}
.history-card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:20px;margin-bottom:14px}
.history-card h3{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1rem;margin-bottom:1rem}
.ref-item{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;border-radius:11px;background:#050e18;margin-bottom:7px;font-size:.82rem}
.ref-item .email{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.ref-item .badge{padding:3px 8px;border-radius:6px;font-weight:700;font-size:.7rem;white-space:nowrap;margin-left:8px}
.badge.paid{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(34,197,94,.25)}
.badge.pending{background:rgba(255,184,0,.12);color:#fde68a;border:1px solid rgba(255,184,0,.25)}
.badge.success{background:rgba(34,197,94,.12);color:#86efac;border:1px solid rgba(34,197,94,.25)}
.badge.failed{background:rgba(251,113,133,.12);color:#fda4af;border:1px solid rgba(251,113,133,.25)}
.empty-state{color:var(--muted);font-size:.85rem;text-align:center;padding:20px;line-height:1.6}
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
  <a class="logo" href="/">Tik<em>Genius</em></a>
  <div class="nav-right">
    <a class="nav-link" href="/">AI Studio</a>
    <a class="btn-nav" href="/">Dashboard</a>
  </div>
</nav>

<div class="wrap">
  <div class="page-hero">
    <h1>Refer friends.<br><em>Earn real money.</em></h1>
    <p>Share your unique link. Every time someone upgrades to Premium through your link, N500 lands in your wallet automatically paid to your bank account.</p>
  </div>

  <div class="stats-row">
    <div class="stat-card highlight">
      <div class="val" id="balanceVal">N0</div>
      <div class="lbl">Wallet Balance</div>
    </div>
    <div class="stat-card">
      <div class="val" id="paidVal">0</div>
      <div class="lbl">Paid Referrals</div>
    </div>
    <div class="stat-card">
      <div class="val" id="totalEarnedVal">N0</div>
      <div class="lbl">Total Earned</div>
    </div>
  </div>

  <div class="link-card">
    <h3>Your referral link</h3>
    <p>Share this link anywhere. When someone signs up and goes Premium, you earn N500 instantly.</p>
    <div class="link-box">
      <input class="link-input" id="refLinkInput" readonly value="Loading...">
      <button class="copy-link-btn" onclick="copyRefLink()">Copy Link</button>
    </div>
    <div class="share-btns">
      <button class="share-btn" onclick="shareWhatsApp()">WhatsApp</button>
      <button class="share-btn" onclick="shareX()">Post on X</button>
      <button class="share-btn" onclick="shareTikTok()">TikTok Bio</button>
      <button class="share-btn" onclick="shareNative()">Share</button>
    </div>
  </div>

  <div class="how-card">
    <h3>How it works</h3>
    <div class="how-steps">
      <div class="how-step"><div class="num">01</div><b>Share your link</b><p>Post it on TikTok, WhatsApp, X anywhere your audience is</p></div>
      <div class="how-step"><div class="num">02</div><b>They upgrade</b><p>When they sign up and pay for Premium, it is automatically tracked</p></div>
      <div class="how-step"><div class="num">03</div><b>You get paid</b><p>N500 added to your wallet instantly. Withdraw to your bank anytime</p></div>
    </div>
  </div>

  <div class="withdraw-card">
    <h3>Withdraw to bank</h3>
    <div class="sub" id="withdrawSub">Minimum withdrawal: N2,000 (4 referrals). Paid instantly via Paystack.</div>
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
      <button class="withdraw-btn" id="withdrawBtn" onclick="doWithdraw()" disabled>Withdraw N0 to Bank</button>
    </div>
    <div class="locked-msg" id="lockedMsg" style="display:none">
      <strong>Keep referring to unlock withdrawal</strong>
      You need N2,000 in your wallet to withdraw. You currently have <span id="currentBal">N0</span>. Keep sharing your link!
    </div>
  </div>

  <div class="history-card">
    <h3>Recent referrals</h3>
    <div id="refList"><div class="empty-state">No referrals yet. Share your link to start earning!</div></div>
  </div>

  <div class="history-card">
    <h3>Withdrawal history</h3>
    <div id="wdList"><div class="empty-state">No withdrawals yet.</div></div>
  </div>
</div>

<script>
var stats={}, banks=[], verifiedName='', verifiedBankCode='', verifiedAccountNumber='';

async function loadStats(){
  try{
    var res=await fetch('/api/referral/stats', {credentials:'include'});
    if(res.status===401){window.location.href='/';return;}
    stats=await res.json();
    document.getElementById('balanceVal').textContent='N'+stats.balance_ngn.toLocaleString();
    document.getElementById('paidVal').textContent=stats.paid_referrals;
    document.getElementById('totalEarnedVal').textContent='N'+stats.total_earned_ngn.toLocaleString();
    document.getElementById('refLinkInput').value=stats.referral_link||'';
    updateWithdrawUI();
    renderReferrals(stats.recent||[]);
  }catch(e){console.error(e)}
}

async function loadBanks(){
  try{
    var res=await fetch('/api/referral/banks', {credentials:'include'});
    var data=await res.json();
    banks=data.banks||[];
    var sel=document.getElementById('bankSelect');
    sel.innerHTML='<option value="">Select your bank</option>';
    banks.forEach(function(b){sel.innerHTML+='<option value="'+b.code+'">'+b.name+'</option>';});
  }catch(e){console.error(e)}
}

async function loadWithdrawalHistory(){
  try{
    var res=await fetch('/api/referral/withdrawal-history', {credentials:'include'});
    var data=await res.json();
    var items=data.items||[];
    var el=document.getElementById('wdList');
    if(!items.length){el.innerHTML='<div class="empty-state">No withdrawals yet.</div>';return;}
    el.innerHTML=items.map(function(w){
      return '<div class="ref-item"><span class="email">N'+(w.amount_kobo/100).toLocaleString()+' to '+escHtml(w.account_name||'')+' ('+escHtml(w.account_number||'')+')</span><span class="badge '+w.status+'">'+w.status+'</span></div>';
    }).join('');
  }catch(e){}
}

function updateWithdrawUI(){
  var canWithdraw=stats.can_withdraw;
  var form=document.getElementById('withdrawForm');
  var locked=document.getElementById('lockedMsg');
  document.getElementById('currentBal').textContent='N'+(stats.balance_ngn||0).toLocaleString();
  if(canWithdraw){
    form.style.display='block';locked.style.display='none';
    document.getElementById('withdrawBtn').textContent='Withdraw N'+(stats.balance_ngn||0).toLocaleString()+' to Bank';
  } else {
    form.style.display='none';locked.style.display='block';
  }
}

function renderReferrals(refs){
  var el=document.getElementById('refList');
  if(!refs.length){el.innerHTML='<div class="empty-state">No referrals yet. Share your link to start earning!</div>';return;}
  el.innerHTML=refs.map(function(r){
    return '<div class="ref-item"><span class="email">'+escHtml(maskEmail(r.email||''))+'</span><span class="badge '+r.status+'">'+(r.status==='paid'?'Earned N'+(r.commission_kobo/100).toLocaleString():'Pending')+'</span></div>';
  }).join('');
}

function maskEmail(email){
  var parts=email.split('@');
  if(parts.length<2)return email;
  return parts[0].slice(0,2)+'***@'+parts[1];
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
    var res=await fetch('/api/referral/verify-account',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_number:accountNumber,bank_code:bankCode})});
    var data=await res.json();
    if(data.error){display.textContent='';showWErr(data.error);return;}
    verifiedName=data.account_name;
    verifiedAccountNumber=accountNumber;
    verifiedBankCode=bankCode;
    display.textContent='Verified: '+data.account_name;
    if(stats.can_withdraw){
      document.getElementById('withdrawBtn').disabled=false;
      document.getElementById('withdrawBtn').textContent='Withdraw N'+(stats.balance_ngn||0).toLocaleString()+' to '+data.account_name;
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
    var res=await fetch('/api/referral/withdraw',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({account_number:verifiedAccountNumber,bank_code:verifiedBankCode,account_name:verifiedName})});
    var data=await res.json();
    if(data.error){showWErr(data.error);return;}
    showWOk(data.message||'Withdrawal initiated successfully!');
    await loadStats();await loadWithdrawalHistory();
  }catch(e){showWErr('Network error. Please try again.');}
  finally{btn.disabled=false;btn.textContent='Withdraw to Bank';}
}

function copyRefLink(){
  var link=document.getElementById('refLinkInput').value;
  navigator.clipboard.writeText(link).then(function(){
    var btn=document.querySelector('.copy-link-btn');
    btn.textContent='Copied!';setTimeout(function(){btn.textContent='Copy Link';},2000);
  });
}

function shareWhatsApp(){
  var link=document.getElementById('refLinkInput').value;
  var msg=encodeURIComponent('I use TikGenius to generate viral TikTok captions, hooks and scripts in seconds. Try it free: '+link);
  window.open('https://wa.me/?text='+msg,'_blank');
}

function shareX(){
  var link=document.getElementById('refLinkInput').value;
  var msg=encodeURIComponent('I use TikGenius to generate viral TikTok content with AI. Try it free: '+link);
  window.open('https://twitter.com/intent/tweet?text='+msg,'_blank');
}

function shareTikTok(){
  var link=document.getElementById('refLinkInput').value;
  navigator.clipboard.writeText(link).then(function(){alert('Link copied! Add it to your TikTok bio.');});
}

function shareNative(){
  var link=document.getElementById('refLinkInput').value;
  if(navigator.share){navigator.share({title:'TikGenius',text:'Generate viral TikTok content with AI',url:link}).catch(function(){});}
  else{copyRefLink();}
}

function showWErr(msg){var e=document.getElementById('wErr');e.textContent=msg;e.classList.add('show');}
function hideWErr(){document.getElementById('wErr').classList.remove('show');}
function showWOk(msg){var e=document.getElementById('wOk');e.textContent=msg;e.classList.add('show');}
function hideWOk(){document.getElementById('wOk').classList.remove('show');}
function escHtml(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}

loadStats();loadBanks();loadWithdrawalHistory();
</script>
</body>
</html>"""

DOWNLOAD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>TikGenius - Download TikTok Videos No Watermark</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#03050a;--surface:#07111c;--card:#0b1928;--border:#14253a;--text:#f0f8ff;--muted:#607a90;--accent:#00ffc8;--accent2:#0af;--gold:#ffb800;--green:#22c55e;--danger:#fb7185}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
nav{display:flex;justify-content:space-between;align-items:center;padding:.85rem 1.4rem;position:sticky;top:0;z-index:100;background:rgba(3,5,10,.85);backdrop-filter:blur(18px);border-bottom:1px solid rgba(0,255,200,.07)}
.logo{display:flex;align-items:center;gap:.5rem;text-decoration:none;color:var(--text);font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.15rem;letter-spacing:-.03em}
.logo em{color:var(--accent);font-style:normal}
.nav-right{display:flex;align-items:center;gap:.65rem}
.nav-link{text-decoration:none;color:var(--muted);font-size:.875rem;font-weight:600;transition:color .2s;padding:.4rem .6rem;border-radius:7px}
.nav-link:hover,.nav-link.active{color:var(--accent)}
.btn-nav{background:var(--accent);color:#030e0a;border:none;padding:.45rem 1.1rem;border-radius:8px;font-size:.875rem;font-weight:700;cursor:pointer;text-decoration:none;font-family:'Inter',sans-serif;transition:all .2s}
.hero{text-align:center;padding:5rem 1.5rem 2.5rem;max-width:680px;margin:0 auto}
.hero h1{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:clamp(2rem,7vw,3.6rem);line-height:1.0;letter-spacing:-.045em;margin-bottom:1rem}
.hero h1 em{color:var(--accent);font-style:normal}
.hero p{color:var(--muted);font-size:1rem;line-height:1.7;max-width:500px;margin:0 auto}
.main-wrap{max-width:700px;margin:0 auto;padding:0 1.25rem 5rem}
.dl-card{background:rgba(7,17,28,.9);border:1px solid var(--border);border-radius:24px;padding:24px;box-shadow:0 32px 80px rgba(0,0,0,.35)}
.how-tip{background:#050e18;border:1px solid var(--border);border-radius:12px;padding:11px 14px;font-size:.8rem;color:var(--muted);margin-bottom:16px;line-height:1.5}
.how-tip strong{color:var(--text)}
.input-row{display:flex;gap:10px;margin-bottom:8px}
.url-input{flex:1;background:#050e18;color:var(--text);border:1px solid var(--border);border-radius:13px;padding:13px 16px;font-size:.95rem;font-family:'Inter',sans-serif;outline:none;transition:border-color .2s;min-width:0}
.url-input:focus{border-color:rgba(0,255,200,.4);box-shadow:0 0 0 3px rgba(0,255,200,.06)}
.url-input::placeholder{color:var(--muted)}
.fetch-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:13px;color:#030e0a;font-weight:800;padding:13px 20px;font-size:.9rem;font-family:'Inter',sans-serif;cursor:pointer;white-space:nowrap;transition:all .2s}
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
.ad-gate-title{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1rem;margin-bottom:.4rem}
.ad-gate-sub{color:var(--muted);font-size:.82rem;line-height:1.55;margin-bottom:14px}
.choices{display:grid;gap:10px}
.choice-btn{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:15px;border-radius:14px;font-family:'Inter',sans-serif;cursor:pointer;font-size:.9rem;font-weight:700;transition:all .2s;border:2px solid transparent}
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
.dl-btn{display:flex;justify-content:space-between;align-items:center;padding:13px 16px;border-radius:12px;text-decoration:none;font-size:.875rem;font-weight:700;font-family:'Inter',sans-serif;transition:all .2s;border:1px solid transparent}
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
.feat h3{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:.9rem}
.feat p{color:var(--muted);font-size:.8rem;line-height:1.55}
.also-try{margin-top:2rem;background:linear-gradient(135deg,rgba(0,255,200,.07),rgba(0,170,255,.05));border:1px solid rgba(0,255,200,.15);border-radius:18px;padding:1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;flex-wrap:wrap}
.also-try-text strong{display:block;font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:.25rem;color:var(--accent)}
.also-try-text span{color:var(--muted);font-size:.82rem}
.also-try-btn{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#030e0a;border:none;border-radius:10px;padding:10px 18px;font-size:.875rem;font-weight:700;font-family:'Inter',sans-serif;cursor:pointer;white-space:nowrap;text-decoration:none;transition:all .2s}
.also-try-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,255,200,.3)}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;align-items:center;justify-content:center;backdrop-filter:blur(6px)}
.modal-overlay.active{display:flex}
.modal{background:#0b1928;border:1px solid rgba(0,255,200,.2);border-radius:22px;padding:2rem;width:100%;max-width:400px;margin:1rem}
.modal h2{font-family:'Space Grotesk',sans-serif;font-weight:800;font-size:1.3rem;margin-bottom:.3rem}
.modal p{color:var(--muted);font-size:.85rem;margin-bottom:1.3rem;line-height:1.5}
.modal-tabs{display:flex;background:#050e18;border-radius:10px;padding:3px;gap:3px;margin-bottom:1.3rem}
.modal-tab{flex:1;padding:.5rem;border:none;border-radius:7px;background:transparent;color:var(--muted);font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;transition:all .2s}
.modal-tab.active{background:#14253a;color:var(--text)}
.fg{margin-bottom:.9rem}
.fg label{display:block;font-size:.75rem;font-weight:700;color:var(--muted);margin-bottom:.4rem;letter-spacing:.04em;text-transform:uppercase}
.fg input{width:100%;background:#050e18;border:1px solid var(--border);color:var(--text);padding:.7rem 1rem;border-radius:10px;font-size:.875rem;font-family:'Inter',sans-serif;outline:none;transition:border-color .2s}
.fg input:focus{border-color:var(--accent)}
.ferr{display:none;color:#fb7185;font-size:.8rem;margin-bottom:.8rem;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.2);padding:.55rem .85rem;border-radius:8px}
.modal-btn{width:100%;padding:.8rem;background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;border-radius:10px;color:#030e0a;font-size:.9rem;font-weight:700;cursor:pointer;font-family:'Inter',sans-serif;margin-bottom:.7rem;transition:all .2s}
.modal-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(0,255,200,.25)}
.modal-cancel{background:none;border:none;color:var(--muted);cursor:pointer;font-size:.8rem;font-family:'Inter',sans-serif;width:100%;padding:.4rem}
@media(max-width:600px){.hero{padding:3.5rem 1rem 2rem}.main-wrap{padding:0 1rem 4rem}.dl-card{padding:16px;border-radius:18px}.input-row{flex-direction:column}.fetch-btn{width:100%}.nav-link{display:none}.choices{grid-template-columns:1fr}}
</style>
</head>
<body>
<nav>
  <a class="logo" href="/">Tik<em>Genius</em></a>
  <div class="nav-right">
    <a class="nav-link" href="/">AI Studio</a>
    <a class="nav-link active" href="/download">Downloader</a>
    <a class="btn-nav" href="/">Open Studio</a>
  </div>
</nav>
<div class="hero">
  <h1>Download TikToks<br><em>No watermark.</em></h1>
  <p>Paste any TikTok link and save the video in HD, clean, no watermark. Free users get 3 downloads per day. Premium unlocks unlimited.</p>
</div>
<div class="main-wrap">
  <div class="dl-card" id="downloaderCard">
    <div class="how-tip"><strong>How to get the link:</strong> Open TikTok, tap Share, Copy Link, paste below.</div>
    <div class="input-row">
      <input type="url" class="url-input" id="urlInput" placeholder="https://www.tiktok.com/@user/video/..." autocomplete="off" autocorrect="off" spellcheck="false">
      <button class="fetch-btn" id="fetchBtn" onclick="fetchVideo()">Fetch Video</button>
    </div>
    <div class="input-hint">Works with tiktok.com, vm.tiktok.com and short links.</div>
    <div class="err-box" id="errorBox"></div>
    <div class="loader" id="loader"><div class="spin"></div><div>Fetching video info...</div></div>
    <div class="preview" id="previewSection">
      <div class="vid-card">
        <div class="vid-top">
          <img class="vid-cover" id="vidCover" src="" alt="cover">
          <div class="vid-info"><div class="vid-title" id="vidTitle"></div><div class="vid-author" id="vidAuthor"></div><div class="vid-meta" id="vidMeta"></div></div>
        </div>
        <div class="ad-gate" id="adGate">
          <div class="ad-gate-title">One quick step to unlock your download</div>
          <div class="ad-gate-sub">3 free TikTok downloads per day, or go Premium for instant, unlimited downloads.</div>
          <div class="choices">
            <button class="choice-btn choice-free" onclick="useFreeDownload()">Use Free Download<span class="choice-label">3 free per day</span></button>
            <button class="choice-btn choice-pro" onclick="upgradeToPro()">Go Premium<span class="choice-label">N2,000/month, Instant always</span></button>
          </div>
        </div>
        <div class="pro-skip" id="proSkip"><span style="font-size:1.2rem">OK</span><div><div class="pro-skip-text">Premium, instant download</div><div class="pro-skip-sub">Unlimited downloads included</div></div></div>
        <div class="dl-panel" id="dlPanel">
          <div class="dl-panel-title">Choose your format</div>
          <div class="dl-buttons">
            <a class="dl-btn dl-primary" id="dlNoWatermark" href="#" download onclick="confirmDownload(event,'nowm')"><span>No Watermark HD</span><span class="dl-btn-meta">Clean MP4</span></a>
            <a class="dl-btn dl-secondary" id="dlWatermark" href="#" download onclick="confirmDownload(event,'wm')"><span>Original with Watermark</span><span class="dl-btn-meta">MP4</span></a>
            <a class="dl-btn dl-audio" id="dlAudio" href="#" download onclick="confirmDownload(event,'audio')"><span>Audio Only</span><span class="dl-btn-meta">MP3</span></a>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="features">
    <div class="feat"><div class="feat-icon">X</div><h3>No Watermark</h3><p>Clean HD video, no TikTok logo burned in.</p></div>
    <div class="feat"><div class="feat-icon">!</div><h3>Instant for Premium</h3><p>Premium users skip every gate and download instantly.</p></div>
    <div class="feat"><div class="feat-icon">~</div><h3>Audio Extraction</h3><p>Save the background music as a standalone MP3.</p></div>
  </div>
  <div class="also-try">
    <div class="also-try-text"><strong>Also try the TikGenius AI Studio</strong><span>Chat with the AI about captions, hooks, scripts, and growth</span></div>
    <a class="also-try-btn" href="/">Open AI Studio</a>
  </div>
</div>
<div class="modal-overlay" id="authModal">
  <div class="modal">
    <h2 id="modalTitle">Create your account</h2>
    <p id="modalSub">Sign up to start downloading, 3 free downloads per day</p>
    <div class="modal-tabs">
      <button class="modal-tab active" id="tabSignup" onclick="switchAuthTab('signup')">Sign Up</button>
      <button class="modal-tab" id="tabLogin" onclick="switchAuthTab('login')">Log In</button>
    </div>
    <div id="fSignup">
      <div class="fg"><label>Email</label><input type="email" id="sEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="sPass" placeholder="Min 6 characters"></div>
      <div class="ferr" id="sErr"></div>
      <button class="modal-btn" onclick="doSignup()">Create Account and Continue</button>
    </div>
    <div id="fLogin" style="display:none">
      <div class="fg"><label>Email</label><input type="email" id="lEmail" placeholder="you@example.com"></div>
      <div class="fg"><label>Password</label><input type="password" id="lPass" placeholder="Your password"></div>
      <div class="ferr" id="lErr"></div>
      <button class="modal-btn" onclick="doLogin()">Log In and Continue</button>
    </div>
    <button class="modal-cancel" onclick="closeAuthModal()">Cancel</button>
  </div>
</div>
<script>
var videoData=null,userLoggedIn=false,userIsPro=false;
window.addEventListener('DOMContentLoaded',async function(){
  try{var res=await fetch('/api/me', {credentials:'include'});if(res.ok){var d=await res.json();userLoggedIn=true;userIsPro=(d.plan==='pro');}}catch(e){}
});
async function fetchVideo(){
  var url=document.getElementById('urlInput').value.trim();hideError();resetPreview();
  if(!url){showError('Please paste a TikTok link first.');return;}
  if(!userLoggedIn){openAuthModal('signup','download');return;}
  setLoading(true);
  try{
    var res=await fetch('/api/download/fetch',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})});
    var data=await res.json();setLoading(false);
    if(!res.ok||data.error){showError(data.error||'Could not fetch this video.');return;}
    videoData=data;renderPreview(data);
  }catch(e){setLoading(false);showError('Network error, please try again.');}
}
function renderPreview(d){
  document.getElementById('vidCover').src=d.cover||'';
  document.getElementById('vidTitle').textContent=d.title||'TikTok Video';
  document.getElementById('vidAuthor').textContent=d.author?'@'+d.author:'';
  var meta=document.getElementById('vidMeta');meta.innerHTML='';
  if(d.duration){meta.innerHTML+='<span class="meta-tag">'+Math.round(d.duration)+'s</span>';}
  meta.innerHTML+='<span class="meta-tag">No Watermark</span><span class="meta-tag">HD</span>';
  document.getElementById('previewSection').classList.add('show');
  if(d.is_pro){document.getElementById('proSkip').classList.add('show');revealDownloads(d);}
  else{document.getElementById('adGate').style.display='block';}
}
async function useFreeDownload(){
  hideError();if(!videoData){showError('Please fetch a TikTok video first.');return;}
  try{
    var res=await fetch('/api/download/confirm',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('urlInput').value.trim(),title:videoData?videoData.title:''})});
    var d=await res.json();
    if(!res.ok||d.error){showError(d.error||'Could not unlock download.');return;}
    document.getElementById('adGate').style.display='none';revealDownloads(videoData);
  }catch(e){showError('Network error, please try again.');}
}
async function upgradeToPro(){
  try{
    var res=await fetch('/api/upgrade',{method:'POST',credentials:'same-origin'});var d=await res.json();
    if(res.status===401){openAuthModal('login','upgrade');return;}
    if(d.url){window.location.assign(d.url);return;}
    showError(d.error||'Could not open payment.');
  }catch(e){showError('Network error.');}
}
function proxyDownloadUrl(fileUrl,filename){return '/api/download/file?url='+encodeURIComponent(fileUrl)+'&filename='+encodeURIComponent(filename);}
function revealDownloads(d){
  var panel=document.getElementById('dlPanel');panel.classList.add('show');
  var base=sanitizeFilename((d&&d.title)||'tiktok');
  var dlNW=document.getElementById('dlNoWatermark');
  if(d&&d.play_url){dlNW.href=proxyDownloadUrl(d.play_url,base+'_nowm.mp4');dlNW.setAttribute('download',base+'_nowm.mp4');}
  else{dlNW.style.display='none';}
  var dlWM=document.getElementById('dlWatermark');
  if(d&&d.wmplay_url){dlWM.href=proxyDownloadUrl(d.wmplay_url,base+'_wm.mp4');dlWM.setAttribute('download',base+'_wm.mp4');}
  else{dlWM.style.display='none';}
  var dlAU=document.getElementById('dlAudio');
  if(d&&d.music_url){dlAU.href=proxyDownloadUrl(d.music_url,base+'_audio.mp3');dlAU.setAttribute('download',base+'_audio.mp3');}
  else{dlAU.style.display='none';}
  panel.scrollIntoView({behavior:'smooth',block:'nearest'});
}
function confirmDownload(e,type){if(!userIsPro)return;try{fetch('/api/download/confirm',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:document.getElementById('urlInput').value.trim(),title:videoData?videoData.title:''})});}catch(err){}}
var _postAuthAction=null;
function openAuthModal(tab,action){_postAuthAction=action;document.getElementById('authModal').classList.add('active');switchAuthTab(tab||'signup');}
function closeAuthModal(){document.getElementById('authModal').classList.remove('active');}
function switchAuthTab(tab){
  document.getElementById('fSignup').style.display=tab==='signup'?'block':'none';
  document.getElementById('fLogin').style.display=tab==='login'?'block':'none';
  document.getElementById('tabSignup').classList.toggle('active',tab==='signup');
  document.getElementById('tabLogin').classList.toggle('active',tab==='login');
  document.getElementById('modalTitle').textContent=tab==='signup'?'Create your account':'Welcome back';
  document.getElementById('modalSub').textContent=tab==='signup'?'Sign up to start downloading, 3 free downloads per day':'Log in to your TikGenius account';
}
async function doSignup(){
  var email=document.getElementById('sEmail').value.trim(),pass=document.getElementById('sPass').value,err=document.getElementById('sErr');
  err.style.display='none';
  var res=await fetch('/api/signup',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass,region:'global'})});
  var data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return;}
  userLoggedIn=true;userIsPro=false;closeAuthModal();
  if(_postAuthAction==='upgrade'){upgradeToPro();}else{fetchVideo();}
}
async function doLogin(){
  var email=document.getElementById('lEmail').value.trim(),pass=document.getElementById('lPass').value,err=document.getElementById('lErr');
  err.style.display='none';
  var res=await fetch('/api/login',{credentials:'include',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email,password:pass})});
  var data=await res.json();
  if(data.error){err.textContent=data.error;err.style.display='block';return;}
  userLoggedIn=true;
  try{var me=await(await fetch('/api/me', {credentials:'include'})).json();userIsPro=me.plan==='pro';}catch(e){}
  closeAuthModal();
  if(_postAuthAction==='upgrade'){upgradeToPro();}else{fetchVideo();}
}
document.getElementById('authModal').addEventListener('click',function(e){if(e.target===this)closeAuthModal();});
function setLoading(show){
  document.getElementById('loader').classList.toggle('show',show);
  document.getElementById('fetchBtn').disabled=show;
  document.getElementById('fetchBtn').textContent=show?'Fetching...':'Fetch Video';
}
function resetPreview(){
  if(document.getElementById('adGate'))document.getElementById('adGate').style.display='';
  document.getElementById('previewSection').classList.remove('show');
  document.getElementById('proSkip').classList.remove('show');
  document.getElementById('dlPanel').classList.remove('show');
}
function showError(msg){var b=document.getElementById('errorBox');b.textContent=msg;b.classList.add('show');}
function hideError(){document.getElementById('errorBox').classList.remove('show');}
function sanitizeFilename(s){return s.replace(/[^a-z0-9_\-]/gi,'_').slice(0,60);}
document.getElementById('urlInput').addEventListener('keydown',function(e){if(e.key==='Enter')fetchVideo();});
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
