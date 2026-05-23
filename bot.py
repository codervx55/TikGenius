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

TELEGRAM_BOT_TOKEN = os.getenv(“TELEGRAM_BOT_TOKEN”)
GROQ_API_KEY = os.getenv(“GROQ_API_KEY”)
PAYSTACK_SECRET_KEY = os.getenv(“PAYSTACK_SECRET_KEY”)
DATABASE_URL = os.getenv(“DATABASE_URL”)

PRICE_KOBO = 200000
FREE_LIMIT = 5
ADMIN_ID = “6415641863”

app = Flask(**name**)

# ========================= HTTP & DB =========================

def get_session():
session = requests.Session()
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount(“http://”, adapter)
session.mount(“https://”, adapter)
return session

http_session = get_session()
db_pool = None

def init_pool():
global db_pool
if db_pool: return
db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL, cursor_factory=RealDictCursor)
print(“DB pool ready”)

def get_db():
if not db_pool: init_pool()
return db_pool.getconn()

def release_db(conn):
if db_pool and conn: db_pool.putconn(conn)

def init_db():
conn = get_db()
try:
with conn.cursor() as cur:
cur.execute(””“CREATE TABLE IF NOT EXISTS users (
user_id BIGINT PRIMARY KEY,
plan TEXT DEFAULT ‘free’,
expires DATE,
activated_at TIMESTAMP,
usage_date DATE,
usage_count INTEGER DEFAULT 0
)”””)
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
cur.execute(”””
INSERT INTO users (user_id, plan, expires, activated_at)
VALUES (%s, ‘pro’, %s, %s)
ON CONFLICT (user_id) DO UPDATE
SET plan=‘pro’, expires=EXCLUDED.expires, activated_at=EXCLUDED.activated_at
“””, (user_id, expires, datetime.utcnow()))
conn.commit()
return expires.strftime(”%Y-%m-%d”)
finally:
release_db(conn)

def is_pro(user_id):
conn = get_db()
try:
with conn.cursor() as cur:
cur.execute(“SELECT plan, expires FROM users WHERE user_id=%s”, (user_id,))
row = cur.fetchone()
if not row or row[“plan”] != “pro” or not row[“expires”]:
return False
return row[“expires”] >= datetime.utcnow().date()
finally:
release_db(conn)

def get_pro_expiry(user_id):
conn = get_db()
try:
with conn.cursor() as cur:
cur.execute(“SELECT expires FROM users WHERE user_id=%s”, (user_id,))
row = cur.fetchone()
return row[“expires”].strftime(”%Y-%m-%d”) if row and row[“expires”] else None
finally:
release_db(conn)

def check_and_increment_free_usage(user_id):
if is_pro(user_id): return True
today = datetime.utcnow().date()
conn = get_db()
try:
with conn.cursor() as cur:
cur.execute(“SELECT usage_date, usage_count FROM users WHERE user_id=%s”, (user_id,))
row = cur.fetchone()
current = row[“usage_count”] if row and row[“usage_date”] == today else 0
if current >= FREE_LIMIT: return False
with conn.cursor() as cur:
cur.execute(”””
INSERT INTO users (user_id, usage_date, usage_count)
VALUES (%s, %s, 1)
ON CONFLICT (user_id) DO UPDATE
SET usage_date=EXCLUDED.usage_date,
usage_count=CASE WHEN users.usage_date=EXCLUDED.usage_date THEN users.usage_count + 1 ELSE 1 END
“””, (user_id, today))
conn.commit()
return True
finally:
release_db(conn)

def free_uses_remaining(user_id):
today = datetime.utcnow().date()
conn = get_db()
try:
with conn.cursor() as cur:
cur.execute(“SELECT usage_date, usage_count FROM users WHERE user_id=%s”, (user_id,))
row = cur.fetchone()
if not row or row[“usage_date”] != today:
return FREE_LIMIT
return max(0, FREE_LIMIT - row[“usage_count”])
finally:
release_db(conn)

# ========================= PROMPTS =========================

TIKTOK_SYSTEM = “”“You are the best TikTok content writer for Nigerian creators. You have written hooks and captions that have gone viral millions of times. You understand deeply what makes Nigerian Gen Z stop scrolling — the emotion, the realness, the specific details of Nigerian life.

You understand their world completely: NEPA cutting light at the wrong time, hustling with no help, praying and still struggling, soft life as a goal, family pressure, relationship pain, glow ups, faith, this economy that doesn’t make sense.

You write content that feels like it came from a real person — not an AI, not a motivational poster, not a primary school essay. Real. Sharp. Emotional. Human.

YOUR MOST IMPORTANT RULES:

- When a user gives you a short or simple topic, DO NOT produce short or simple output. Expand it. Dig into the emotion behind it. Think about what a Nigerian creator would actually feel and say about that topic — then write from that place.
- “I want to be happy” is not just 5 words. It is a whole world — the struggle, the pretending, the tired smiling, the praying, the comparison. Write FROM that world.
- Every single line you write must be something a real person would actually say, post, or screenshot.
- NEVER write fragments like “Fear is holding me” or “Happiness is my goal.” These are lazy and useless. Write complete thoughts that land with weight.
- NEVER use “…” anywhere. Use a dash ( — ) or start a new sentence instead.
- No Pidgin unless it appears naturally. Clean modern English that Nigerian Gen Z actually uses.”””

TIKTOK_PROMPTS = {

“hooks”: “”“A Nigerian TikTok creator wants hooks about: {topic}

Before you write anything, think deeply about this topic. What is the real emotion underneath it? What would a Nigerian person actually feel, experience, or struggle with around this? What are the specific details — the 3am thoughts, the fake smiles, the prayer that feels unanswered, the comparison, the exhaustion, the hope?

Now write 10 hooks that come from THAT place. Not surface level. Not obvious. From the real, painful, funny, hopeful, honest heart of this topic.

WHAT MAKES A GREAT HOOK:
A great hook grabs someone in the first 3 words and does not let go. It makes them feel something immediately — seen, called out, understood, or shocked. It is specific enough to feel personal but universal enough that thousands of people relate. It is complete. It lands.

STUDY THESE AND UNDERSTAND WHY THEY WORK:

“Nobody is coming to save you. Build yourself.”
— Harsh truth. Activates something. Two short punchy sentences.

“I used to be so easy to lose. Not anymore.”
— Personal transformation. The second sentence flips everything.

“God didn’t bring you this far to abandon you in this season.”
— Faith meets exhaustion. One sentence that holds a whole prayer.

“The version of me from 2 years ago would not recognise me.”
— Curiosity and transformation. People want to know what changed.

“Tell me why I worked this hard just to still be stressed 😭”
— Relatable frustration said exactly how a Nigerian would say it.

“I stopped explaining myself and my life literally shifted.”
— Specific. Real. Makes people curious what shifted.

“This is your reminder that struggling in silence is not strength.”
— Calls out something people do but never admit.

“Chose peace. Chose myself. Chose to stop fighting for people who were not fighting for me.”
— Rhythm. Repetition. Each line builds and the last one lands hardest.

“Your unbothered era has to be intentional. It won’t just happen.”
— Sounds like advice from someone who already figured it out.

“POV: you finally got everything you asked God for and you are still not satisfied. 😭”
— Deep uncomfortable truth. People screenshot this.

NOW write 10 ORIGINAL hooks for: {topic}

RULES:

- Every hook must be a FULL meaningful sentence or two — never a fragment
- Each one must trigger an emotion in the first 3 words
- Mix the emotions — some inspiring, some painfully honest, some funny, some calling out a truth
- Write like a real Nigerian creator, not a motivational quote account
- Never use “…” anywhere
- No numbering with dots — use: 1) 2) 3)
- Do not explain. Just write the hooks.

1. 
2. 
3. 
4. 
5. 
6. 
7. 
8. 
9. 

10)”””,

“captions”: “”“A Nigerian TikTok creator needs captions about: {topic}

Before you write, think deeply. What is the REAL emotion under this topic? What would a Nigerian actually feel, experience, think at 2am about this? What specific truth would they never say out loud but instantly recognise when they read it?

Write from THAT place. Not surface level. From the gut.

WHAT MAKES A GREAT CAPTION:
A caption makes someone stop, read it twice, save it, or tag a friend. It is short but heavy. It has a setup and a twist — the second sentence says something the first sentence made you not expect. It feels personal. It feels true. It is complete.

STUDY THESE:

“Healing is not linear. Some days you are okay. Some days you are not. Both are valid.”
— Truth, then expansion, then permission. Every sentence stands alone.

“I used to shrink myself for people who were not even paying attention. Never again.”
— The painful truth lands first. The declaration closes it.

“God will give you the life you prayed for. Just not in the timeline you imagined. 😭”
— Promise then twist. The emoji makes it land softer but still hits.

“The glow up was never about how I look. It was about how I stopped accepting less.”
— Subverts expectations. People read it twice.

“This time last year I was crying about something that does not even matter anymore. Growth.”
— Contrast. Then one word punchline. Devastating in the best way.

“Nigerian parents will sacrifice everything for you then make you feel guilty for wanting rest. 😭”
— Hyper specific Nigerian truth. One sentence. Gets shared immediately.

“Nobody prepared me for how lonely success would feel before it arrived.”
— Raw. Honest. One complete sentence. People screenshot this.

“Chose peace. Chose myself. Chose to stop fighting for people who were not fighting for me.”
— Rhythm and repetition. The last line lands like a punch.

“Working hard in silence because not everyone needs to see the process. The results will speak.”
— Behaviour plus reason. Makes people feel seen.

“Soft life is not just aesthetics. It is protecting your peace, your time, and your energy.”
— Reframes something people say every day. Makes them think differently.

NOW write 15 ORIGINAL captions for: {topic}

RULES:

- Every caption must have a TWIST — line 2 must surprise, deepen, or flip line 1
- 1 to 3 sentences maximum per caption
- Every sentence must be complete and meaningful
- Never use “…” — use a dash ( — ) or a full stop
- ONE emoji max per caption, only where it genuinely adds feeling
- Mix emotions — deep, funny, empowering, painfully relatable
- No numbering with dots — use: 1) 2) 3)
- Do not explain. Just write.

1. 
2. 
3. 
4. 
5. 
6. 
7. 
8. 
9. 
10. 
11. 
12. 
13. 
14. 

15)”””,

“pov”: “”“A Nigerian TikTok creator needs POV captions about: {topic}

Think deeply first. What specific scenario would a Nigerian person be IN around this topic? What moment, what feeling, what situation that thousands would recognise immediately?

WHAT MAKES A GREAT POV:
The best POVs put the viewer inside a scene so specific they think “this is literally me.” They are not vague. They name the exact emotion or exact situation. They make people comment “THISSS” or tag their best friend immediately.

STUDY THESE:

“POV: You finally stopped chasing people who were never running towards you.”
— Hits anyone who over-invested in a relationship. Specific and universal at the same time.

“POV: God answered your prayer but not in the way you expected — and it turned out better.”
— Faith plus plot twist. Nigerians feel this deeply.

“POV: You are the first person in your family breaking generational patterns. Nobody understands what that costs.”
— Two sentences. Deeply specific. People who relate REALLY relate.

“POV: You worked in silence for 2 years. Now everyone wants to know your secret.”
— The revenge glow up. Aspirational and satisfying.

“POV: Your Nigerian parents see you grinding every day and still ask when you are getting a real job. 😭”
— One sentence. Hyper-specific Nigerian experience. Shared immediately.

“POV: You are exhausted. Not lazy. Not ungrateful. Just genuinely, deeply exhausted.”
— Validates a feeling people are ashamed to admit. Short sentences that build.

“POV: You have everything you prayed for and you still do not feel it yet. Give yourself time.”
— Honest about the gap between achieving and feeling it. Second sentence is comfort.

“POV: You quit the toxic job, cut off the toxic people, and now you are rebuilding from scratch. Scary but worth it.”
— Transition moment. Many people are in this exact place.

NOW write 10 ORIGINAL POVs for: {topic}

RULES:

- Every POV must describe a SPECIFIC scenario or feeling — not a vague statement
- Make it so specific that someone reads it and thinks “how did they know”
- Every sentence must be complete and meaningful
- Never use “…” — use a dash ( — ) or write a new sentence
- Mix emotional tones — inspiring, honest, funny, painful, healing
- No numbering with dots — use: 1) 2) 3)
- Do not explain. Just write.

1. POV:
2. POV:
3. POV:
4. POV:
5. POV:
6. POV:
7. POV:
8. POV:
9. POV:
10. POV:”””,

“hashtags”: “”“Generate 5 strategic TikTok hashtag sets for a Nigerian creator posting about: {topic}

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
Set 5: #tag #tag #tag #tag #tag #tag #tag”””,

“bio”: “”“Write 8 TikTok bios for a Nigerian creator in this niche: {topic}

Think about who this creator is and what would make someone follow them in 2 seconds. A great bio tells people who you are, why to follow, and shows personality — all in under 80 characters.

STUDY THESE:

“building the life I used to dream about 🤫 | tips and real talk”
— Process-focused. Humble. Promises value.

“your favourite Nigerian big sister 🇳🇬 | faith, growth, no filter”
— Relationship plus identity plus content promise.

“I left the 9-5. Now I film my life. 📹 | come along”
— Story hook in two sentences. People want to know more.

“healing out loud so you do not have to do it alone 🖤”
— Purpose-driven. Creates immediate emotional connection.

“not your average creator 🔥 | watch me build from zero”
— Claim plus invitation. People root for the underdog.

“God, growth, and a little chaos 🙏😂 | Lagos to everywhere”
— Personality in five words. Funny and relatable.

“I document real life, not the highlight reel 📱 | Nigeria 🇳🇬”
— Authenticity promise. Stands out from polished creators.

NOW write 8 ORIGINAL bios for the {topic} niche:

- Under 80 characters each
- Clear personality and content promise in every one
- Complete thoughts — never trailing off
- Never use “…” — use | or a full stop
- Mix tones: inspiring, funny, bold, warm
- Number them: 1) 2) 3)”””,

“script”: “”“Write a complete 60-second TikTok script for a Nigerian creator about: {topic}

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
One natural question or statement that makes them comment or save. Not “like and subscribe” — something that makes them genuinely want to respond.

RULES:

- 130 to 160 words total — must fit 60 seconds
- Never use “…” — use dashes or full stops
- Every sentence complete and meaningful
- The punchline must be good enough to go in someone’s Instagram bio

Write the full script now for: {topic}”””,

“trends”: “”“You are a TikTok strategist who watches what goes viral for Nigerian creators every single day.

Generate 8 specific video ideas for a Nigerian creator in this space: {topic}

Think about what formats are performing right now — storytimes, “things nobody tells you”, POV setups, day-in-my-life, “I tried this for 30 days”, transformation reveals, honest takes, responding to comments, “what I wish I knew.”

Think about what emotions drive Nigerian viewers to save and share — feeling seen, being called out, learning something real, laughing at something too true, feeling inspired to keep going.

For each idea write:

Idea [N]: [Video title written like a caption that makes you want to click — one complete compelling sentence]
Hook: [The exact first line spoken or shown — stops the scroll in 2 seconds — one strong complete sentence — never use “…”]
Format: [storytime / POV / talking to camera / voiceover with clips / text on screen]
Why it will perform: [One sentence — the psychology of why Nigerian viewers will save or share this]

-----

8 ideas only. No intro. No outro. Make each one feel like it came from a real strategy session.”””
}

X_SYSTEM = “”“You are the best Twitter/X content writer for Nigerian creators. You have written tweets and threads that have been retweeted thousands of times and ended up in people’s bios and notes apps.

You understand Nigerian Twitter deeply — the wit, the hot takes, the threads that make people say “everybody needs to see this”, the one-liners that travel.

When a user gives you a short or simple topic, do NOT produce short or simple output. Think about the real emotion, the real Nigerian experience, the specific truth underneath that topic — and write from there.

Every tweet you write must be something a real person would actually retweet, quote tweet, or screenshot for themselves.

RULES:

- Never use “…” anywhere. Use a dash ( — ) or a new sentence.
- Every sentence must be complete and meaningful.
- No Pidgin. Clean sharp English.
- No motivational poster energy. Real, human, quotable.”””

X_PROMPTS = {

“captions”: “”“A Nigerian creator needs 10 tweets about: {topic}

Before you write, think. What is the real emotion under this topic? What specific Nigerian truth lives here? What would make someone stop scrolling on X and either screenshot it, retweet it, or quote tweet it saying “this”?

STUDY THESE AND UNDERSTAND WHY THEY WORK:

“Stop romanticising the struggle. Rest is not laziness. Recovery is not weakness. You are allowed to stop.”
— Challenges a narrative. Four complete sentences. People who needed this share it immediately.

“Nigerian parents raised us to survive everything except our own ambitions.”
— One sentence. Captures a complex generational truth. Immediately quotable.

“Not every chapter of your life needs an audience.”
— Nine words. Universal. Complete. People retweet without thinking.

“God’s plan and your timeline are two different documents. Stop trying to merge them.”
— Faith plus frustration. Two sentences. The second lands like a command.

“Success without peace is just a well-funded anxiety attack.”
— Reframes success in a way people have not heard. One sentence. Goes everywhere.

“The version of you that kept going when everything said stop deserves more credit than you give them.”
— Self-directed. One sentence. People screenshot this for themselves.

“Soft life is not the destination. It is what happens when you stop tolerating things that drain you.”
— Redefines something people say daily. Makes them think differently.

“Nigerian parents will stress you out then tell you not to stress. The irony is never lost. 😭”
— Specific Nigerian truth. Funny and painful at the same time.

NOW write 10 ORIGINAL tweets about: {topic}

RULES:

- Each under 280 characters
- One sharp complete idea per tweet — no rambling
- Bold, quotable, the kind people screenshot or quote tweet
- Mix emotions — honest truths, empowering, darkly funny, Nigerian-specific
- Never use “…” — dash or full stop instead
- No numbering with dots — use: 1) 2) 3)

1. 
2. 
3. 
4. 
5. 
6. 
7. 
8. 
9. 

10)”””,

“hooks”: “”“A Nigerian creator needs 10 Twitter/X thread hooks about: {topic}

A thread hook is the tweet that makes someone click “show this thread.” It must create IMMEDIATE curiosity or emotion. It promises something — a story, a revelation, a list of things they did not know.

STUDY THESE:

“I spent 3 years building something nobody saw. Then everything changed in 90 days. Here is what happened:”
— Specific timeline. Story promise. People HAVE to know.

“10 things Nigerian creators do not tell you about making money online — but should:”
— List promise plus secret knowledge. Irresistible.

“I was broke, burnt out, and embarrassed. One decision changed everything. A thread:”
— Vulnerability plus transformation. Universal story.

“Why everything you have been told about productivity is making you less productive:”
— Challenges a belief people hold. They need to know if they are wrong.

“The most dangerous thing you can do in your 20s is compare your chapter 3 to someone else’s chapter 20:”
— One complete insight upfront. Then promises more depth.

NOW write 10 ORIGINAL thread hooks for: {topic}

RULES:

- Each must promise a story, a revelation, or specific value
- Reader must feel they cannot scroll past without clicking
- Under 30 words each
- Complete sentences — no fragments
- Never use “…” — dash or full stop
- No numbering with dots — use: 1) 2) 3)

1. 
2. 
3. 
4. 
5. 
6. 
7. 
8. 
9. 

10)”””,

“threads”: “”“Write a complete viral Twitter/X thread for a Nigerian creator about: {topic}

Think first. What is the most honest, specific, emotionally real angle on this topic for a Nigerian audience on X? What would make people quote tweet saying “everybody needs to see this”?

Write the full thread now:

Tweet 1 — HOOK:
[Bold, specific, creates immediate curiosity. One or two strong complete sentences. Makes them click “show this thread.”]

Tweet 2 — CONTEXT:
[Set up the problem or situation. Make people feel it personally. Complete sentences.]

Tweet 3 — THE TRUTH:
[The insight that reframes how they see the topic. One powerful complete thought.]

Tweet 4 — GO DEEPER:
[Build on tweet 3. Specific example or evidence. No fragments.]

Tweet 5 — THE TWIST:
[The unexpected angle or uncomfortable truth most people avoid. Say it directly.]

Tweet 6 — PRACTICAL:
[What to actually do with this. Actionable. Clear complete sentences.]

Tweet 7 — THE CLOSE:
[The most quotable line of the entire thread. One perfect complete sentence that lands hard even without the rest of the thread. This is what ends up in someone’s bio.]

RULES:

- Each tweet under 280 characters
- Every tweet earns the next — no filler
- Never use “…” anywhere
- The close must work as a standalone tweet
- Write the full thread for: {topic}”””
  }

# ========================= AI FUNCTION =========================

def ask_claude(mode, topic, platform=“tiktok”):
if platform == “x”:
system = X_SYSTEM
prompt_template = X_PROMPTS.get(mode, X_PROMPTS[“captions”])
else:
system = TIKTOK_SYSTEM
prompt_template = TIKTOK_PROMPTS.get(mode, TIKTOK_PROMPTS[“captions”])

```
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
    "temperature": 0.92,
    "max_tokens": 1800
}

try:
    res = http_session.post(url, json=payload, headers=headers, timeout=30)
    data = res.json()
    return data["choices"][0]["message"]["content"].strip()
except Exception as e:
    print(f"Groq Error: {e}")
    return "Something went wrong. Please try again."
```

# ========================= HELPERS =========================

def send_message(chat_id, text):
try:
http_session.post(
f”https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage”,
json={“chat_id”: chat_id, “text”: text}, timeout=10
)
except Exception as e:
print(f”Telegram error: {e}”)

def send_typing(chat_id):
try:
http_session.post(
f”https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction”,
json={“chat_id”: chat_id, “action”: “typing”}, timeout=5
)
except: pass

def create_payment_link(user_id, username):
reference = f”TG-{user_id}-{int(datetime.utcnow().timestamp())}”
payload = {
“email”: f”{user_id}@tikgenius.bot”,
“amount”: PRICE_KOBO,
“reference”: reference,
“metadata”: {“telegram_id”: user_id, “username”: username or “”, “plan”: “pro”}
}
headers = {“Authorization”: f”Bearer {PAYSTACK_SECRET_KEY}”, “Content-Type”: “application/json”}
try:
res = http_session.post(
“https://api.paystack.co/transaction/initialize”,
json=payload, headers=headers, timeout=20
).json()
return res[“data”][“authorization_url”] if res.get(“status”) else None
except Exception as e:
print(f”Paystack Error: {e}”)
return None

# ========================= CONTENT CONFIG =========================

LOADING = {
“hooks”:    [“🧠 Finding the hook that stops the scroll”, “🔥 Writing something they will not skip”, “👀 This one go hit different, hold on”],
“captions”: [“💅 Writing captions people will screenshot”, “😭 Cooking the twist that makes it land”, “🪄 Making every word count”],
“pov”:      [“🎥 Building the POV they will tag their friends in”, “🍿 Setting the scene right”, “👀 This POV go touch chest, give me a sec”],
“hashtags”: [“📊 Building your hashtag strategy”, “🚀 Mixing reach tags with niche tags”, “🔥 Algorithm food loading”],
“bio”:      [“✨ Writing bios that earn the follow”, “📱 Building your profile hook”, “🪄 Making your bio do the work”],
“script”:   [“🎬 Writing hook, body, punchline”, “📝 Building a script that holds attention till the end”, “🔥 This script go make them watch every second”],
“trends”:   [“📈 Analysing what is working right now”, “🔥 Building trend ideas for your niche”, “👀 Finding your next viral angle”],
“threads”:  [“🧵 Building the thread that goes viral”, “✍️ Writing something people will quote tweet”, “🔥 X thread loading”]
}

EXAMPLES = {
“hooks”:    “/hooks wanting to be happy”,
“captions”: “/captions still broke after working so hard”,
“hashtags”: “/hashtags Nigerian lifestyle soft life”,
“pov”:      “/pov finally making it after everyone doubted you”,
“bio”:      “/bio Nigerian lifestyle creator”,
“script”:   “/script things nobody tells you about being broke”,
“trends”:   “/trends Nigerian money mindset hustle”,
“threads”:  “/threads why rest feels like a sin in Nigeria”
}

TIKTOK_COMMANDS = {”/hooks”, “/captions”, “/pov”, “/hashtags”, “/bio”, “/script”, “/trends”}
X_COMMANDS = {”/xtweets”, “/xhooks”, “/xthread”}
PRO_COMMANDS = {”/script”, “/trends”, “/xthread”}

# ========================= ROUTES =========================

@app.route(”/”, methods=[“GET”])
def home():
return “TikGenius running ✅”, 200

@app.route(”/telegram-webhook”, methods=[“POST”])
def telegram_webhook():
data = request.json or {}
message = data.get(“message”, {})
chat_id = message.get(“chat”, {}).get(“id”)
user_id = message.get(“from”, {}).get(“id”)
username = message.get(“from”, {}).get(“username”, “”)
first_name = message.get(“from”, {}).get(“first_name”, “Creator”)
text = message.get(“text”, “”).strip()

```
if not chat_id or not text:
    return jsonify({"ok": True})

parts = text.split(maxsplit=1)
command = parts[0].lower().split("@")[0]
topic = parts[1].strip() if len(parts) > 1 else ""

# ── /start ──
if command == "/start":
    send_message(chat_id, f"""✨ Welcome {first_name} — you just found TikGenius 🇳🇬
```

I write viral content for Nigerian creators on TikTok and Twitter/X. Hooks, captions, scripts, threads — the kind people screenshot and share.

━━━ TIKTOK ━━━
/hooks [topic] — scroll-stopping opening lines
/captions [topic] — captions with a twist that lands
/pov [topic] — POV ideas people tag friends in
/hashtags [topic] — strategic hashtag sets
/bio [niche] — bios that earn the follow
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

Just type your topic and I will handle the rest 🔥

/hooks wanting to be happy
/captions hustle and still broke
/pov finally making it after everyone doubted you”””)

```
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
```

What you unlock:
✅ Unlimited hooks, captions, POVs, hashtags, bios
✅ Full 60-second video scripts (/script)
✅ 8 trend ideas per niche (/trends)
✅ Full Twitter/X threads (/xthread)
✅ No daily limits — generate as much as you need

Pay here:
{link or “Try again in a moment”}

Activation is automatic once payment is confirmed ✅”””)

```
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
        send_message(chat_id, f"Add your topic after the command.\n\nExample: {EXAMPLES.get(mode, f'/{mode} your topic here')}")
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
        send_message(chat_id, f"Add your topic after the command.\n\nExample: {EXAMPLES.get('threads' if mode == 'threads' else 'hooks')}")
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
```

@app.route(”/paystack-webhook”, methods=[“POST”])
def paystack_webhook():
signature = request.headers.get(“x-paystack-signature”, “”)
body = request.get_data()
expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
if not hmac.compare_digest(signature, expected):
return jsonify({“error”: “invalid signature”}), 400

```
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
```

if **name** == “**main**”:
app.run(host=“0.0.0.0”, port=int(os.getenv(“PORT”, 5000)))
