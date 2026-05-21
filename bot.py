import os
import json
import hmac
import hashlib
import random
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
from groq import Groq

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

PRICE_KOBO = 200000
USERS_FILE = "users.json"
FREE_LIMIT = 5

flask_app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────
# LANGUAGE STYLES
# Each style changes the system prompt + examples
# so the AI output genuinely shifts in voice.
# ─────────────────────────────────────────────

STYLE_NAMES = {
    "pidgin":  "Nigerian Pidgin 🇳🇬",
    "english": "Clean English 🌍",
    "mixed":   "Naija Mixed (default) ✨",
}

# Users pick one of these three
VALID_STYLES = set(STYLE_NAMES.keys())

# Per-style system prompts
SYSTEM_PROMPTS = {

    "pidgin": """You are TikGenius. You write Nigerian TikTok content that goes viral.

Your voice is 100% Nigerian Pidgin — the way people actually talk in Lagos, Abuja, PH.
You sound like a real person typing fast on their phone, not an AI.

Examples of your natural voice:
- "this one pain me die"
- "abeg who collect my shine?"
- "i no go lie, e dey sweet me"
- "they think say i forget? lol"
- "na so e be sha"
- "the audacity ehn 😭"
- "sapa don locate me again"

NEVER write:
- Motivational English quotes
- Long grammar-perfect sentences
- AI-sounding structure like "Here are your hooks:"
- Forced Pidgin that doesn't flow naturally
- Hashtags inside hooks or captions unless asked

ALWAYS write:
- Short. Punchy. Emotionally raw.
- Pidgin that feels typed, not translated
- Content a real Naija TikToker would post without editing
""",

    "english": """You are TikGenius. You write Nigerian TikTok content that goes viral.

Your voice is clean, modern English — the kind used by Nigerian creators who speak
to both local and international audiences. Think: polished but not stiff. Relatable
but not slangy. The energy of someone confident, unbothered, and very online.

Examples of your natural voice:
- "I wasn't supposed to share this"
- "Nobody talks about how hard this actually is"
- "The signs were there, I just wasn't looking"
- "I tried to play it cool and completely failed"
- "This hit differently than I expected"
- "Not me realizing this at 2am"
- "I gave it time and time gave me back clarity"

NEVER write:
- Nigerian Pidgin or local slang
- Motivational poster quotes
- Stiff corporate language
- "Here are your hooks:" or any preamble
- Hashtags inside hooks or captions unless asked

ALWAYS write:
- Clean, casual, human English
- Short sentences. Emotional truth. Real situations.
- Content that feels personal, not like a broadcast
""",

    "mixed": """You are TikGenius. You write Nigerian TikTok content that goes viral.

You think like a 22-year-old Lagos creator: fluent in English, Pidgin, and the
kind of Naija Gen Z Twitter/TikTok voice that mixes both naturally.
You never force slang. It just appears where it fits.

Examples of your natural voice:
- "this thing pain me lowkey"
- "i can't be the only one abeg"
- "Not me crying over this at midnight 😭"
- "they really thought i wasn't paying attention"
- "sapa don finish me this month"
- "I saw the signs btw"
- "na me cause am honestly"
- "the way I screamed"

NEVER write:
- Motivational quotes
- Long explanations or preamble like "Here are your hooks:"
- Robotic AI structure
- Hashtags inside hooks or captions unless asked

ALWAYS write:
- Short. Punchy. Mix of English and Pidgin that sounds effortless.
- Emotionally true. Occasionally unhinged.
- Like it was typed by a real person, not generated.
""",
}

# ─────────────────────────────────────────────
# PROMPTS — one set, style injected dynamically
# ─────────────────────────────────────────────

# Style-specific example banks injected into prompts
HOOK_EXAMPLES = {
    "pidgin": """- i no suppose post this 😭
- this one pain me die
- abeg who fit explain this
- e don happen again sha
- the audacity ehn
- i swear i dey act okay
- nobody tell me say na so e go be
- they think say i forget? lol
- wait make i gist you something
- sapa just locate me 💀""",

    "english": """- I wasn't supposed to share this
- nobody talks about this enough
- the signs were there the whole time
- I tried to stay unbothered and failed
- this hit differently than I expected
- not me realizing this at 2am 😭
- I gave them the benefit of the doubt and look
- can we talk about how real this is
- I kept this to myself for too long
- watch what happens next""",

    "mixed": """- i was not supposed to post this 😭
- this thing pain me lowkey
- nobody asked me this in 4 years 💀
- i can't be the only one abeg
- the way i SCREAMED
- they really thought i wasn't paying attention
- babe i found out something
- ngl this one hit different
- i tried to act unbothered and FAILED
- POV: you just remembered that thing 😭""",
}

CAPTION_EXAMPLES = {
    "pidgin": """- e pain me die lowkey
- na me cause am sha
- i no go lie ehn
- them really do me like that 😭
- sapa don locate me again
- i dey manage myself
- love no easy like this before
- i see the signs sha
- e be like say na joke
- soft life when? 🕊️
- my own don do 💀
- i carry myself comot
- them really try am
- nobody normal here and e dey okay
- this your bestie era 💅""",

    "english": """- this one stayed with me
- I should have seen it coming
- quietly collecting myself
- it took time, but I'm good now
- not everything needs an explanation
- I missed a version of myself
- love wasn't supposed to feel like this
- I noticed. I just said nothing.
- learning to let things be
- soft life is the only life 🕊️
- I carried myself and left
- they really did that 😭
- nobody here is fully okay and that's fine
- your healing era is valid 💅
- I chose peace and I'd do it again""",

    "mixed": """- this one pain me lowkey
- i can explain sha 🤦‍♀️
- i miss my old self
- no because WHY
- na me cause am honestly
- mentally i'm tired
- love no hard like this before
- i saw the signs btw
- i dey act okay
- soft life pls 🕊️
- my toxic trait is thinking i'm fine
- carried myself and left
- they really did that 😭
- nobody is normal here and that's okay
- this your bestie era 💅""",
}

POV_EXAMPLES = {
    "pidgin": """- POV: you finally stop reply person wey dey stress you
- POV: sapa locate you the same day you say soft life
- POV: you dey pretend say you no care but you check their story 3 times
- POV: you enter Lagos traffic with full tank and full confidence
- POV: you be the friend wey remember everything
- POV: you get the bag and suddenly cousins everywhere
- POV: you hear your name for conversation you no dey
- POV: you finally chop the good food wey you save
- POV: you give am second chance and third thing happen
- POV: you realize say na you be the main character""",

    "english": """- POV: you finally stop responding to someone who was draining you
- POV: you say "soft life" and immediately hit a financial wall
- POV: you pretend not to care but check their story three times
- POV: you walk into a situation with full confidence and leave humbled
- POV: you're the friend who remembers everything nobody else does
- POV: you finally get the money and long-lost people appear
- POV: you overhear your name in a conversation you weren't in
- POV: you finally eat the good food you've been saving for no reason
- POV: you gave a second chance and a third problem showed up
- POV: you realize the main character was you the whole time""",

    "mixed": """- POV: you finally stop replying someone that was stressing you
- POV: your sapa hits the moment you say "soft life"
- POV: you pretend you don't care but check their story 3x
- POV: you enter Lagos traffic with full confidence and a full tank
- POV: you're the friend that remembers everything
- POV: you realize the main character was you the whole time
- POV: you get the bag and suddenly everyone is a cousin
- POV: you hear your name in a conversation you weren't in
- POV: you finally eat the good food you've been saving
- POV: you gave them a second chance and THIRD thing happened""",
}

BIO_EXAMPLES = {
    "pidgin": """- lagos pikin. content wey go burst. no dulling
- i post when spirit carry me
- your fave sef dey follow me
- soft life in progress 🕊️
- i dey unlearn things slowly
- professional overthinker | amateur human
- e dey give main character and i no dey sorry
- just a girl wey love chaos and jollof 🌸""",

    "english": """- just a girl navigating Lagos one day at a time 🌸
- I post when the moment is right
- your favorite creator's favorite creator
- soft life in progress 🕊️
- slowly unlearning everything I was taught
- professional overthinker | amateur human
- main character energy, no apologies
- content, chaos, and good food""",

    "mixed": """- just a girl who loves chaos and carbs 🌸
- lagos bred. content made. no apologies
- i post when the spirit moves me
- your fave's fave tbh
- soft life in progress 🕊️
- unlearning everything slowly
- professional overthinker | amateur human
- it's giving main character and i'm not sorry""",
}


def build_prompts(style: str) -> dict:
    """Return the full prompt dict with style-specific examples injected."""
    h = HOOK_EXAMPLES[style]
    c = CAPTION_EXAMPLES[style]
    p = POV_EXAMPLES[style]
    b = BIO_EXAMPLES[style]

    return {
        "hooks": f"""Topic: {{topic}}

Write 10 viral TikTok opening hooks. These are the first words that appear on the video screen.

Study these real examples first:
{h}

Rules:
- under 12 words each
- no punctuation at sentence end unless it's an emoji
- mix emotions: funny, painful, dramatic, chaotic, relatable
- sound like breaking news from someone's life
- match the voice style of the examples exactly

Output: just the 10 hooks, numbered 1-10. Nothing else.""",

        "captions": f"""Topic: {{topic}}

Write 15 TikTok captions. These go below the video.

Study these real captions:
{c}

Rules:
- short, 1-8 words mostly
- no full storytelling
- mix: aesthetic, soft, unbothered, chaotic, funny
- match the voice style of the examples exactly

Output: just the 15 captions, numbered 1-15. Nothing else.""",

        "hashtags": """Topic: {topic}

Create 5 TikTok hashtag sets.

Rules:
- 6 hashtags per set
- Set 1: broad reach (#fyp type)
- Set 2: niche/topic specific
- Set 3: Nigerian audience focus
- Set 4: emotional/mood tags
- Set 5: mixed best-of strategy
- No explanation

Output format:
Set 1: #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag""",

        "bio": f"""Creator niche: {{topic}}

Write 8 short TikTok bios.

Study these for the right voice:
{b}

Rules:
- under 80 characters each
- sound like a real person wrote it
- match the voice style of the examples exactly
- no cheesy job descriptions

Output: just 8 bios, numbered 1-8. Nothing else.""",

        "pov": f"""Topic: {{topic}}

Write 10 POV ideas for TikTok videos.

Study these real examples:
{p}

Rules:
- one sentence each
- relatable, specific, Nigerian references where natural
- funny, painful, dramatic, or quietly real
- match the voice style of the examples exactly

Output: just the 10 POVs, numbered 1-10. Nothing else.""",

        "script": """Topic/idea: {topic}

Write a short TikTok video script. Under 60 seconds when read aloud.

Format:
[HOOK] — the first 3 seconds (text on screen OR spoken line)
[BODY] — main content in short punchy lines
[ENDING] — mic-drop line or call to action

Style:
- casual, like talking to a close friend on camera
- short sentences, natural pauses
- funny or emotionally real
- match the voice and language style you've been using

Output: the script only. No extra explanation.""",

        "trends": """Current TikTok niche: {topic}

Suggest 8 video concepts this creator should film RIGHT NOW.

For each idea:
- Concept: one line describing the video
- Hook: exact text to put on screen
- Why it works: one sentence

Style:
- specific and actionable, not generic
- grounded in what's actually working on Nigerian TikTok
- match the voice and language style you've been using

Output: 8 numbered ideas in that format. Nothing else.""",
    }


# ─────────────────────────────────────────────
# USER DATA
# ─────────────────────────────────────────────
def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def get_user_style(user_id: int) -> str:
    users = load_users()
    return users.get(str(user_id), {}).get("style", "mixed")


def set_user_style(user_id: int, style: str):
    users = load_users()
    uid = str(user_id)
    users.setdefault(uid, {})["style"] = style
    save_users(users)


def activate_pro(user_id):
    users = load_users()
    uid = str(user_id)
    expires = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    existing = users.get(uid, {})
    existing.update({
        "plan": "pro",
        "expires": expires,
        "activated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    })
    users[uid] = existing
    save_users(users)
    return expires


def is_pro(user_id):
    users = load_users()
    user = users.get(str(user_id))
    if not user or user.get("plan") != "pro":
        return False
    expires = datetime.strptime(user["expires"], "%Y-%m-%d")
    return expires >= datetime.utcnow()


def check_and_increment_free_usage(user_id):
    if is_pro(user_id):
        return True
    users = load_users()
    uid = str(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    user = users.setdefault(uid, {})
    usage = user.get("usage", {})
    if usage.get("date") != today:
        usage = {"date": today, "count": 0}
    if usage["count"] >= FREE_LIMIT:
        return False
    usage["count"] += 1
    user["usage"] = usage
    users[uid] = user
    save_users(users)
    return True


def free_uses_remaining(user_id):
    users = load_users()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    usage = users.get(str(user_id), {}).get("usage", {})
    if usage.get("date") != today:
        return FREE_LIMIT
    return max(0, FREE_LIMIT - usage.get("count", 0))


# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────
def send_message(chat_id, text, parse_mode=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def send_typing(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────
# AI CALL
# ─────────────────────────────────────────────
def ask_ai(mode: str, topic: str, style: str) -> str:
    prompts = build_prompts(style)
    prompt = prompts[mode].format(topic=topic)
    system = SYSTEM_PROMPTS[style]

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.92,
            max_tokens=800,
            top_p=0.95,
            frequency_penalty=0.4,
            presence_penalty=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq error: {e}")
        return "⚠️ TikGenius AI dey busy right now. Try again small time."


# ─────────────────────────────────────────────
# PAYMENT
# ─────────────────────────────────────────────
def create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {
            "telegram_id": user_id,
            "username": username or "",
            "plan": "pro",
        },
    }
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }
    try:
        res = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=20,
        ).json()
        if res.get("status"):
            return res["data"]["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None


# ─────────────────────────────────────────────
# LOADING MESSAGES — per style
# ─────────────────────────────────────────────
LOADING = {
    "pidgin": [
        "🧠 hold on make i cook this one well well...",
        "🔥 this one go burst, just wait small",
        "⚡ i dey check wetin dey land on Naija FYP...",
        "👀 e dey load... your followers no go fit skip am",
        "🎬 relax... something mad dey come",
        "📈 oya make we touch FYP small...",
        "🇳🇬 i dey think like Lagos creator wey get 500k...",
        "😭 this one go make dem comment their whole story...",
    ],
    "english": [
        "🧠 Working on something that will actually stop scrolling...",
        "🔥 Hold on, making this one count...",
        "⚡ Checking what's landing on the FYP right now...",
        "👀 Finding content your audience won't skip...",
        "🎬 Almost ready, this one's going to hit...",
        "📈 Crafting your path to the FYP...",
        "🇳🇬 Thinking like a creator with 500k followers...",
        "😭 This one will have them typing paragraphs in the comments...",
    ],
    "mixed": [
        "🧠 hold on make i cook this properly...",
        "🔥 this one go burst, just wait",
        "⚡ checking what's landing on Naija FYP rn...",
        "👀 finding something your followers won't skip...",
        "🎬 relax... something mad is loading",
        "📈 oya make we touch FYP small...",
        "🇳🇬 thinking like a Lagos creator with 500k followers...",
        "💅 cooking your content era right now...",
        "😭 this one go make them comment their whole life story...",
    ],
}

# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS  = {"/script", "/trends"}
ALL_CONTENT   = FREE_COMMANDS | PRO_COMMANDS


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200


@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data    = request.json or {}
    message = data.get("message", {})
    chat    = message.get("chat", {})
    user    = message.get("from", {})

    chat_id    = chat.get("id")
    user_id    = user.get("id")
    username   = user.get("username", "")
    first_name = user.get("first_name", "Creator")
    text       = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts   = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]
    topic   = parts[1].strip() if len(parts) > 1 else ""

    # ── /start ──────────────────────────────
    if command == "/start":
        send_message(chat_id, f"""🔥 Welcome to TikGenius, {first_name}! 🇳🇬

Your AI TikTok content plug — built for Nigerian creators.

First, pick your content language style:

/setlanguage pidgin   → full Nigerian Pidgin
/setlanguage english  → clean English
/setlanguage mixed    → Naija mix (default)

You can switch anytime with /language

━━━━━━━━━━━━━━━
Commands:
/hooks [topic]     → viral opening lines
/captions [topic]  → short captions
/hashtags [topic]  → 5 hashtag sets
/pov [topic]       → POV video ideas
/bio [niche]       → bio options
/script [idea]     → full video script ⭐
/trends [niche]    → what to film now ⭐

⭐ = Pro only | Free: {FREE_LIMIT} uses/day
/upgrade → Pro for ₦2,000/month""")

    # ── /language / /setlanguage ─────────────
    elif command in ("/language", "/setlanguage"):
        if topic.lower() in VALID_STYLES:
            style = topic.lower()
            set_user_style(user_id, style)
            name = STYLE_NAMES[style]
            send_message(chat_id, f"✅ Language style set to: {name}\n\nAll your content will now come in this style. Switch anytime with /language")
        else:
            current = get_user_style(user_id)
            current_name = STYLE_NAMES[current]
            send_message(chat_id, f"""🌐 Choose your content language style:

/setlanguage pidgin   — Nigerian Pidgin 🇳🇬
/setlanguage english  — Clean English 🌍
/setlanguage mixed    — Naija Mix ✨ (default)

Current style: {current_name}

This affects how ALL your hooks, captions, POVs, scripts, and bios are written.""")

    # ── /plan ────────────────────────────────
    elif command == "/plan":
        users = load_users()
        style = get_user_style(user_id)
        style_name = STYLE_NAMES[style]
        if is_pro(user_id):
            exp = users[str(user_id)]["expires"]
            send_message(chat_id, f"""✅ TikGenius Pro — Active

Expires: {exp}
Usage: Unlimited
Language: {style_name}

Keep posting. Your page go blow. 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""📊 Free Plan

Uses left today: {remaining}/{FREE_LIMIT}
Language style: {style_name}

Pro unlocks:
✅ Unlimited uses
✅ /script — full video scripts
✅ /trends — what to film now

/upgrade → ₦2,000/month
/language → change content style""")

    # ── /upgrade ─────────────────────────────
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you unlock:
✅ Unlimited hooks, captions, hashtags, POVs, bios
✅ /script — AI writes your full video script
✅ /trends — trending video ideas for your niche
✅ All 3 language styles

Pay here 👇
{link}

Activation is automatic after payment ⚡""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try /upgrade again in a moment.")

    # ── /help ────────────────────────────────
    elif command == "/help":
        style = get_user_style(user_id)
        style_name = STYLE_NAMES[style]
        send_message(chat_id, f"""🔥 TikGenius Commands

Content:
/hooks [topic]
/captions [topic]
/hashtags [topic]
/pov [topic]
/bio [niche]
/script [idea]  ⭐ Pro
/trends [niche] ⭐ Pro

Settings:
/language       → change content style
/plan           → check your plan
/upgrade        → go Pro

Current style: {style_name}
Change with: /language

Examples:
/hooks soft life lagos
/captions my glow up
/script how I saved ₦500k""")

    # ── Content commands ──────────────────────
    elif command in ALL_CONTENT:
        mode  = command.replace("/", "")
        style = get_user_style(user_id)

        # Pro-only gate
        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            msg  = f"🔒 {command} is a Pro feature.\n\n"
            msg += f"Upgrade to unlock it:\n{link}" if link else "Use /upgrade to go Pro."
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Topic required
        if not topic:
            examples = {
                "hooks":    f"/hooks soft life lagos",
                "captions": f"/captions my glow up era",
                "hashtags": f"/hashtags Nigerian food",
                "pov":      f"/pov you finally left a bad situation",
                "bio":      f"/bio lifestyle and fashion creator",
                "script":   f"/script how I saved ₦500k in 6 months",
                "trends":   f"/trends relationship content",
            }
            send_message(chat_id, f"Add a topic 👇\n\nExample:\n{examples.get(mode, command + ' [topic]')}")
            return jsonify({"ok": True})

        # Free limit check
        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            msg  = "⏳ You've used all your free tries for today.\n\nUpgrade for unlimited access:\n"
            msg += link if link else "/upgrade"
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Generate content
        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING[style]))
        send_typing(chat_id)

        result = ask_ai(mode, topic, style)
        send_message(chat_id, result[:4000])

        # Show style tip once (first use only) or nudge near limit
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining == 0:
                link = create_payment_link(user_id, username)
                msg  = "⚡ That was your last free use today.\n\nGo unlimited:\n"
                msg += link if link else "/upgrade"
                send_message(chat_id, msg)
            elif remaining <= 2:
                send_message(
                    chat_id,
                    f"💡 {remaining} free use(s) left today.\n/upgrade to go unlimited.\n/language to switch content style."
                )

    else:
        send_message(chat_id, "Unknown command.\n\nType /help to see everything TikGenius can do.")

    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# PAYSTACK WEBHOOK
# ─────────────────────────────────────────────
@flask_app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body      = request.get_data()
    expected  = hmac.new(
        PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid signature"}), 400

    event = request.json or {}

    if event.get("event") == "charge.success":
        data        = event["data"]
        amount      = data.get("amount")
        metadata    = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            expires = activate_pro(telegram_id)
            send_message(
                telegram_id,
                f"""🎉 Payment confirmed! Welcome to Pro 🚀

Valid until: {expires}
Status: Unlimited access activated

Newly unlocked:
/script [idea]  — AI writes your full video
/trends [niche] — what to post this week

Tip: set your content language with /language

Go try it:
/hooks your niche 🔥""",
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
