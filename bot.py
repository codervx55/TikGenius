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

TIKTOK_SYSTEM = """You are a viral TikTok content strategist who has studied millions of viral Nigerian and global TikTok posts. You know exactly what makes content blow up — the psychology, the timing, the words, the emotion. You have helped creators go from 0 to 100k followers by writing captions and hooks that stop people mid-scroll.

You write content that:
- Triggers an emotion in the first 3 words
- Makes people feel seen, called out, or understood
- Is simple enough for anyone to get instantly
- Has a second line that surprises, twists, or lands like a punch

The creators you write for are Nigerian — so you understand their world: NEPA cutting light, the hustle, soft life goals, family pressure, relationship wahala, faith and doubt, glow ups, this economy. You reference these naturally, not forcefully.

Your language is clean modern English — the way Nigerian Gen Z creators actually write their captions. Not Pidgin. Not grammar-school English. Casual, real, emotional, and sharp.

ABSOLUTE PUNCTUATION RULES — follow these without exception:
- NEVER use "..." (ellipsis) anywhere in your response. Not once. Not even a single time.
- To create a pause between a setup and a twist, use a dash ( — ) or start a new line.
- Every sentence must be complete and meaningful on its own. Write full thoughts, not fragments trailing off.
- If you feel the urge to write "...", stop and rewrite the sentence as two complete sentences instead."""

TIKTOK_PROMPTS = {

"hooks": """You are writing TikTok hooks for a Nigerian creator posting about: {topic}

A TikTok hook is the first line of text on screen or the first words spoken. It has ONE job — make someone stop scrolling in under 2 seconds.

Study these real hooks that went viral and understand WHY they work:

"Nobody is coming to save you. Build yourself."
WHY: Direct, slightly harsh, activates the ego — two short complete sentences that hit hard

"The version of me from 2 years ago would not recognise me."
WHY: Curiosity plus transformation — one full sentence that makes people want to know what changed

"I used to be so easy to lose. Not anymore."
WHY: Short, personal, empowering — two complete sentences where the second flips the first

"God didn't bring you this far to abandon you in this season."
WHY: Faith plus reassurance — one strong complete sentence that hits Nigerians deeply

"Your unbothered era has to be intentional. It won't just happen."
WHY: Sounds like advice from someone who figured it out — two complete sentences, second one is the gut punch

"This is your reminder that struggling in silence is not strength."
WHY: Calls out something many people do but never say — one declarative sentence that feels personal

"I stopped explaining myself and my life literally shifted."
WHY: Specific life change — one complete sentence that makes people curious about what shifted

"Tell me why I worked this hard just to still be stressed 😭"
WHY: Funny, relatable frustration — one conversational complete sentence Nigerian creators use perfectly

"The way this country will humble you if you don't humble yourself first 😭"
WHY: Nigerian-specific truth — one complete sentence that lands instantly

"POV: you finally got everything you asked God for. You are still not satisfied. 😭"
WHY: Deep honest truth most people feel but won't say — two complete sentences that build on each other

Now write 10 ORIGINAL high-quality hooks for the topic: {topic}

STRICT RULES:
- Every hook must be a complete, meaningful sentence — no fragments, no trailing thoughts
- Never use "..." — if you need a pause, use a dash ( — ) or write two separate sentences
- Trigger an emotion in the FIRST 3 WORDS of every hook
- Mix different emotions: some inspiring, some funny, some painfully honest, some calling out a truth
- Clean simple English — under 20 words each
- No numbering with dots — use: 1) 2) 3)
- Do not explain the hooks. Just write them.

Output format:
1) [hook]
2) [hook]
and so on""",

"captions": """You are writing TikTok captions for a Nigerian creator posting about: {topic}

TikTok captions appear under the video. The best ones make people stop, read twice, save the post, or tag a friend. They are SHORT, EMOTIONAL, and have a TWIST — the second sentence completely flips or deepens the first.

Study these viral-quality captions and understand their structure:

"Healing is not linear. Some days you are okay. Some days you are not. Both are valid."
STRUCTURE: A truth followed by expansion and permission — every sentence is complete and stands alone

"I used to shrink myself for people who were not even paying attention. Never again."
STRUCTURE: Past behaviour followed by the painful truth, then a short declaration — flows naturally

"God will give you the life you prayed for. Just not in the timeline you imagined. 😭"
STRUCTURE: A promise followed by a twist on expectations — two sentences that work together perfectly

"Soft life is not just aesthetics. It is protecting your peace, your time, and your energy."
STRUCTURE: Reframes a popular idea — gives people a new way to think about something they say daily

"The glow up was never about how I look. It was about how I stopped accepting less."
STRUCTURE: Sets up one expectation, then delivers something deeper — makes people read it twice

"Working hard in silence because not everyone needs to see the process. The results will speak."
STRUCTURE: Behaviour followed by the reason — makes people feel seen if they relate

"Nobody prepared me for how lonely success would feel before it arrived."
STRUCTURE: Raw honest truth in one complete sentence — people screenshot this and send to friends

"Chose peace. Chose myself. Chose to stop fighting for people who were not fighting for me."
STRUCTURE: Triple declaration with rhythm — each choice builds on the last and lands harder

"This time last year I was crying about something that does not even matter anymore. Growth."
STRUCTURE: Contrast followed by a one-word punchline — simple and devastating in the best way

"Nigerian parents will sacrifice everything for you then make you feel guilty for wanting rest. 😭"
STRUCTURE: Specific truth about the Nigerian experience — one complete sentence that gets shared instantly

Now write 15 ORIGINAL high-quality captions for the topic: {topic}

STRICT RULES:
- Every caption must use complete, meaningful sentences — no fragments, no trailing thoughts
- Never use "..." anywhere — use a dash ( — ), a full stop, or a new line instead
- Every caption must have a TWIST — the second sentence must surprise, deepen, or flip the first
- Mix emotions: some deep and honest, some funny, some empowering, some painfully relatable
- 1 to 3 sentences maximum per caption
- ONE emoji maximum per caption, only where it genuinely adds feeling
- No numbering with dots — use: 1) 2) 3)
- Do not explain. Just write the captions.

Output format:
1) [caption]
2) [caption]
and so on""",

"pov": """You are writing TikTok POV captions for a Nigerian creator posting about: {topic}

POV (Point of View) content is one of TikTok's most viral formats. The creator films themselves and the POV text puts the viewer inside a scenario. The best POVs are so specific and relatable that people comment "THIS IS ME" or tag their friends immediately.

Study these viral POV examples:

"POV: You finally stopped chasing people who were never running towards you."
WHY: One complete sentence that hits anyone who has ever over-invested in a relationship

"POV: God answered your prayer but not in the way you expected — and it turned out better."
WHY: Faith plus plot twist in one sentence — Nigerian audiences love this deeply

"POV: You are the first person in your family breaking generational patterns. Nobody understands what that costs."
WHY: Two complete sentences — deeply specific, and people who relate REALLY relate

"POV: You worked in silence for 2 years. Now everyone wants to know your secret."
WHY: Two short sentences — aspirational and satisfying, the revenge glow up energy

"POV: You have everything you asked for and you still do not feel it yet. Give yourself time."
WHY: Honest about the gap between achieving and feeling fulfilled — two sentences, second one is comfort

"POV: Your Nigerian parents see you grinding daily and still ask when you are getting a real job. 😭"
WHY: Hyper-specific Nigerian experience in one complete sentence — instantly shareable

"POV: You are exhausted. Not lazy. Not ungrateful. Just genuinely, deeply exhausted."
WHY: Validates a feeling people are ashamed to admit — short punchy sentences that build to the truth

"POV: You quit the toxic job, cut the toxic people, and now you are rebuilding from scratch. Scary but necessary."
WHY: Transition moment described in one sentence, then a two-word verdict that makes it hit

Now write 10 ORIGINAL viral-quality POVs for the topic: {topic}

STRICT RULES:
- Every POV must describe a SPECIFIC scenario or feeling — not vague statements
- Every sentence must be complete and meaningful — no fragments, no trailing thoughts
- Never use "..." — use a dash ( — ) or write a new sentence instead
- Make it so specific that someone reads it and thinks "how did they know"
- Mix emotional tones: inspiring, honest, funny, painful, healing
- Clean modern English — short and punchy
- No numbering with dots — use: 1) 2) 3)

Output format:
1) POV: [scenario]
2) POV: [scenario]
and so on""",

"hashtags": """Generate 5 strategic TikTok hashtag sets for a Nigerian creator posting about: {topic}

Good hashtag strategy mixes reach levels so TikTok shows the video to the right people at scale.

Each set must have exactly 7 hashtags:
- 2 massive reach tags (100M+ views): #fyp #foryoupage #tiktok #viral
- 2 medium reach tags (1M-50M views): topic-specific tags people actually search
- 2 niche tags (under 1M): very specific to the content
- 1 Nigerian tag: #nigeriantiktok #naija #lagostiktok #naijavibes #nigeriantwitter

Think carefully about what someone who wants to watch this content would actually search for.

Format exactly like this:
Set 1: #tag #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag #tag

Nothing else. No explanation.""",

"bio": """Write 8 TikTok bio options for a Nigerian creator in this niche: {topic}

A great TikTok bio does 3 things in under 80 characters:
1. Tells people WHO you are
2. Tells them WHY to follow
3. Has a personality — something memorable

Study these bios that actually work:

"building the life I used to dream about 🤫 | tips and real talk"
WHY: Process-oriented, humble, promises value — reads like one complete thought

"your favourite Nigerian big sister 🇳🇬 | faith, growth, no filter"
WHY: Relationship plus identity plus content promise — three clear things in one line

"I left the 9-5. Now I film my life. 📹 | come along"
WHY: Two short complete sentences as a story hook — people want to know more

"soft life is not a flex. it is a decision 💅 | join me"
WHY: Reframes a concept with two complete sentences then an invitation

"God, growth, and a little chaos 🙏😂 | Lagos to everywhere"
WHY: Personality in 5 words — funny and relatable

"not your average creator 🔥 | watch me build from zero"
WHY: A claim followed by an invitation — makes people root for the journey

"healing out loud so you do not have to do it alone 🖤"
WHY: Purpose-driven in one complete sentence — creates immediate emotional connection

"I document real life, not the highlight reel 📱 | Nigeria 🇳🇬"
WHY: Authenticity promise in one clear sentence — stands out from polished creators

Write 8 ORIGINAL bios for the {topic} niche:
- Under 80 characters each
- Each one must have a clear personality and content promise
- Complete thoughts only — no fragments, no trailing off
- Never use "..." — use | or a full stop instead
- Mix different tones: some inspiring, some funny, some bold
- Number them: 1) 2) 3)""",

"script": """Write a complete TikTok video script for a Nigerian creator. Topic: {topic}

This script must be so good that someone could film it TODAY and have a viral video. Every word is intentional. Every line earns the next one.

Structure:

[HOOK — 0 to 3 seconds]
The very first thing said or shown on screen. Must create an immediate emotional reaction — curiosity, shock, laughter, or pain. Under 15 words. This is the most important part. Write it as one strong, complete sentence.

[BODY — 4 to 45 seconds]
The main content. Written exactly how a real Nigerian creator speaks on camera — short complete sentences, natural rhythm, occasional pause for effect. Tell a story, share a truth, give value, or make a point. Every sentence must earn the next one. No filler. No fragments trailing off.

[PUNCHLINE — 45 to 55 seconds]
The single most memorable line of the entire video. The one people screenshot. The one that makes them send it to a friend. The twist, the truth, the gut punch. Write it as one perfect complete sentence.

[CTA — 55 to 60 seconds]
One natural question or statement that makes people comment, save, or share. Not "like and subscribe" energy — something that genuinely makes them want to respond. One complete sentence.

STRICT RULES:
- Total words: 130 to 160 maximum — must fit 60 seconds of speaking
- Write how people SPEAK, not how they write essays — short sentences, natural rhythm
- Nigerian references welcome where they feel natural (NEPA, soft life, this country, etc.)
- Never use "..." anywhere — use a dash ( — ) or a full stop to create pauses
- Every sentence must be complete and meaningful — no trailing fragments
- The punchline must be the kind of line that goes in someone's Instagram bio

Write the full script now for: {topic}""",

"trends": """You are a TikTok trend analyst who watches what goes viral for Nigerian creators daily.

Generate 8 specific video ideas for a Nigerian creator in this space: {topic}

These must be ideas that could realistically go viral RIGHT NOW — based on what formats and emotions are performing on TikTok: storytimes, "things nobody tells you", silent vlogs, POV setups, day-in-my-life, "I tried X for 30 days", transformation reveals, honest opinion takes, "responding to comments", and "what I wish I knew" formats.

For each idea:

Idea [N]: [Specific video title — written like a caption that would make you click. One complete sentence.]
Hook: [Exact first line spoken or shown on screen — must stop the scroll in 2 seconds. One strong complete sentence. Never use "..."]
Format: [What type of video: storytime / POV / talking to camera / voiceover plus clips / text on screen]
Why it will perform: [One sentence on the psychology — why Nigerian viewers will save or share this]

---

Make each idea feel like it came from a strategy session with a real social media manager, not a generic content list.
No intro. No outro. 8 ideas only."""
}

X_SYSTEM = """You are a Twitter/X content strategist who understands virality deeply. You have studied the accounts that consistently get thousands of retweets and quote tweets — and you know exactly why.

You write for Nigerian creators who want to build influence on X. You understand Nigerian Twitter culture: the wit, the hot takes, the threads that make people screenshot and share, the one-liners that end up in people's bios.

Your content is:
- Written in clean, sharp English
- Bold enough to make people stop mid-scroll
- Specific enough to feel personal
- Quotable — the kind of thing people copy into their notes app

No Pidgin. No fluff. No motivational poster energy. Real, sharp, and human.

ABSOLUTE PUNCTUATION RULES — follow these without exception:
- NEVER use "..." (ellipsis) anywhere in your response. Not once.
- Every sentence must be complete and meaningful on its own.
- To create a pause or a beat, use a dash ( — ) or start a new sentence.
- Write full thoughts that land cleanly. No fragments trailing off into nothing."""

X_PROMPTS = {

"captions": """Write 10 viral-quality Twitter/X posts for a Nigerian creator about: {topic}

Study these tweets that actually performed and understand why:

"Stop romanticising the struggle. Rest is not laziness. Recovery is not weakness. You are allowed to stop."
WHY: Challenges a common narrative with four complete sentences — people who needed to hear this share it immediately

"Nigerian parents raised us to survive everything except our own ambitions."
WHY: One complete sentence that captures a complex generational truth — immediately quotable

"The version of you that kept going when everything said stop deserves more credit than you give them."
WHY: Self-directed appreciation in one sentence — people screenshot this for themselves

"Not every chapter of your life needs an audience."
WHY: Nine words. Universal. One complete thought. Makes people nod and retweet without thinking.

"God's plan and your timeline are two different documents. Stop trying to merge them."
WHY: Faith plus frustration — two complete sentences, the second one lands like a command

"Success without peace is just a well-funded anxiety attack."
WHY: Reframes success in a way people have not heard before — one quotable sentence

"The energy you protect this year will determine what you build next year."
WHY: Forward-looking, actionable, quotable — one complete sentence that goes in bios and notes apps

"Soft life is not the destination. It is what happens when you stop tolerating things that drain you."
WHY: Redefines a term people use daily with two complete sentences — makes them think differently

Now write 10 ORIGINAL tweets about {topic}:
- Each under 280 characters
- One sharp idea per tweet — no rambling
- Every sentence must be complete and meaningful — no trailing fragments
- Bold, quotable, the kind people screenshot or quote tweet
- Clean English, no Pidgin
- Mix emotions: some honest truths, some empowering, some darkly funny
- Never use "..." — use a dash ( — ) or a full stop instead
- Format: 1) 2) 3)""",

"hooks": """Write 10 powerful Twitter/X thread starter hooks for a Nigerian creator about: {topic}

A thread hook is the tweet that makes someone click "show this thread" — it must create IMMEDIATE curiosity or emotional reaction.

Study these effective thread openers:

"I spent 3 years building something. Nobody saw it. Then everything changed in 90 days. Here is what happened:"
WHY: Story promise with a specific timeline — four short complete sentences and people HAVE to know what changed

"10 things Nigerian creators do not tell you about making money online — but should:"
WHY: List promise plus secret knowledge framing — irresistible to click

"The most dangerous thing you can do in your 20s is compare your chapter 3 to someone else's chapter 20. Thread:"
WHY: Insight delivered upfront in one complete sentence plus promises more depth

"I was broke, burnt out, and embarrassed. One decision changed everything. A thread:"
WHY: Vulnerability plus transformation — three complete sentences in a universal story structure

"Why everything you have been told about productivity is making you less productive:"
WHY: Challenges a belief people hold in one complete sentence — they need to see if they are wrong

Write 10 ORIGINAL thread hooks for: {topic}
- Each must promise value, a story, or a revelation
- Make the reader feel they cannot scroll past without clicking
- Under 30 words each
- Every sentence complete and meaningful — no trailing fragments
- Never use "..." — use a dash ( — ) or a full stop
- Clean sharp English
- Format: 1) 2) 3)""",

"threads": """Write a complete Twitter/X thread for a Nigerian creator about: {topic}

This thread must be good enough to go viral — the kind that gets quote tweets saying "everybody needs to see this."

Format:

Tweet 1 — HOOK: [The opener that makes people click "show this thread" — bold, specific, creates immediate curiosity. Write it as one or two strong complete sentences.]

Tweet 2 — CONTEXT: [Set up the problem or situation. Make people feel it personally. Complete sentences that flow naturally.]

Tweet 3 — THE TRUTH: [The insight or observation that reframes how they see the topic. One powerful complete thought.]

Tweet 4 — GO DEEPER: [Build on tweet 3. Give the specific example or evidence. Complete sentences, no fragments.]

Tweet 5 — THE TWIST: [The unexpected angle or uncomfortable truth most people avoid. Write it directly and completely.]

Tweet 6 — PRACTICAL: [What to actually do with this information — make it actionable. Clear complete sentences.]

Tweet 7 — CLOSE: [The most quotable line of the thread. The one that ends up in someone's bio or notes app. One perfect complete sentence that lands hard even without the rest of the thread.]

STRICT RULES:
- Each tweet under 280 characters
- Every tweet must earn the next one — no filler
- Every sentence must be complete and meaningful — no trailing fragments
- Never use "..." anywhere — use a dash ( — ) or a full stop
- Clean sharp English, no Pidgin
- The close must be independently shareable even without the rest of the thread
- Write the full thread now for: {topic}"""
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
        "temperature": 0.9,
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
    "hooks":    ["🧠 Writing hooks that stop the scroll", "🔥 Finding the angle that makes them watch", "👀 This one go hit different, hold on"],
    "captions": ["💅 Writing captions people will screenshot", "😭 Cooking the twist that makes it land", "🪄 Making it sound like you felt every word"],
    "pov":      ["🎥 Building the POV they will tag their friends in", "🍿 Setting up the scene and the twist", "👀 This POV go touch chest, give me a sec"],
    "hashtags": ["📊 Building your hashtag strategy", "🚀 Mixing reach tags with niche tags", "🔥 Algorithm food loading"],
    "bio":      ["✨ Writing bios that get the follow", "📱 Building your profile hook", "🪄 Making your bio do the work for you"],
    "script":   ["🎬 Writing hook, body, punchline", "📝 Building a 60-second script that holds attention", "🔥 This script go make them watch till the end"],
    "trends":   ["📈 Analysing what is working right now", "🔥 Building trend ideas for your niche", "👀 Finding your FYP angle"],
    "threads":  ["🧵 Building the thread that goes viral", "✍️ Writing something people will quote tweet", "🔥 X thread loading"]
}

EXAMPLES = {
    "hooks":    "/hooks I prayed for this life and I am still not happy",
    "captions": "/captions I work so hard but I am still broke",
    "hashtags": "/hashtags Nigerian lifestyle and soft life content",
    "pov":      "/pov you finally made it and nobody who doubted you said sorry",
    "bio":      "/bio Nigerian lifestyle and soft life creator",
    "script":   "/script things nobody tells you before you start working for yourself",
    "trends":   "/trends Nigerian money mindset and hustle content",
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
        send_message(chat_id, f"""✨ Welcome {first_name} — you just found TikGenius 🇳🇬

I write viral content for Nigerian creators on TikTok and Twitter/X. The kind people screenshot, save, and tag their friends in.

━━━ TIKTOK ━━━
/hooks [topic] — scroll-stopping opening lines
/captions [topic] — captions with a twist that lands
/pov [topic] — POV ideas people tag friends in
/hashtags [topic] — strategic hashtag sets
/bio [niche] — bios that make people follow
/script [idea] ⭐ Pro — full 60-second video script

━━━ TWITTER / X ━━━
/xtweets [topic] — 10 quotable tweets
/xhooks [topic] — thread starters that get clicks
/xthread [topic] ⭐ Pro — full viral thread

━━━ OTHER ━━━
/trends [niche] ⭐ Pro — 8 video ideas for your niche
/plan — check your plan
/upgrade — go Pro

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

The more specific your topic, the better the output 🔥

❌ Too vague: /captions tired
✅ Try this: /captions I work so hard but I am still broke
✅ Try this: /hooks I prayed for this life and I am still not happy
✅ Try this: /pov you finally made it and nobody who doubted you said sorry""")

    # ── /plan ──
    elif command == "/plan":
        if is_pro(user_id):
            send_message(chat_id, f"✅ Pro Active\nExpires: {get_pro_expiry(user_id)}\n\nUnlimited access to everything.")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"🆓 Free Plan\nUses left today: {remaining}/{FREE_LIMIT}\n\nUpgrade to Pro for ₦2,000/month — unlimited everything → /upgrade")

    # ── /upgrade ──
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you unlock:
✅ Unlimited hooks, captions, POVs, hashtags, bios
✅ Full 60-second video scripts (/script)
✅ Weekly trend ideas (/trends)
✅ Full Twitter/X threads (/xthread)
✅ No daily limits — generate as much as you need

Pay here:
{link or "Try again in a moment"}

Activation is automatic the moment payment is confirmed ✅""")

    # ── /activatepro (admin) ──
    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            target_id = int(topic) if topic.isdigit() else user_id
            expires = activate_pro(target_id)
            send_message(chat_id, f"✅ Pro activated for {target_id}\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ Not allowed.")

    # ── /stats (admin) ──
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
            send_message(chat_id, f"🔒 This is a Pro feature.\n\nUpgrade for ₦2,000/month to unlock:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get(mode, f'/{mode} your topic here')}")
            return jsonify({"ok": True})

        if len(topic.split()) < 3:
            send_message(chat_id, f"Be more specific for better results.\n\nInstead of: /{mode} {topic}\nTry something like: {EXAMPLES.get(mode)}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You have used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro for unlimited access:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it"])))
        result = ask_claude(mode, topic, "tiktok")
        send_message(chat_id, f"✨ TikGenius\n\n{result[:3800]}")

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today.\n\nGo Pro for ₦2,000/month — unlimited everything → /upgrade")

    # ── Twitter/X commands ──
    elif command in X_COMMANDS:
        mode_map = {"/xtweets": "captions", "/xhooks": "hooks", "/xthread": "threads"}
        mode = mode_map[command]

        if command == "/xthread" and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"🔒 X Threads is a Pro feature.\n\nUpgrade for ₦2,000/month:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        if not topic:
            send_message(chat_id, f"Add a topic after the command.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if len(topic.split()) < 3:
            send_message(chat_id, f"Be more specific for better results.\n\nExample:\n{EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            send_message(chat_id, f"⏳ You have used all {FREE_LIMIT} free uses for today.\n\nUpgrade to Pro:\n{link or '/upgrade'}")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING.get(mode, ["🔥 Working on it"])))
        result = ask_claude(mode, topic, "x")
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
                f"🎉 Payment confirmed. Welcome to Pro.\n\nAccess active till {expires}\n\nEverything is unlocked ✅\n\nTry /script, /trends, or /xthread now.")

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
