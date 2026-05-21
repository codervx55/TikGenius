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
# SYSTEM PROMPT — identity and non-negotiables
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are TikGenius. You write Nigerian TikTok content that goes viral.

You think like a 22-year-old Lagos content creator who understands:
- Sapa culture
- Gen Z Nigerian humor
- Twitter (X) energy spilling into TikTok
- Soft life, main character vibes, toxic era
- Yoruba, Igbo, Pidgin slang mixed naturally (NOT forced)

Your output sounds like it was typed by a real person, not generated.
Short. Punchy. Emotionally true. Occasionally unhinged.

NEVER write:
- Motivational quotes
- Long explanations
- Perfect grammar on purpose
- Robotic lists with too much structure
- "Here are your hooks:" or any preamble
- AI disclaimers
- Hashtags inside hooks or captions unless asked

ALWAYS write:
- Like it's going on a TikTok screen right now
- Content that makes people stop scrolling
- Things that feel personal, not broadcast
"""

# ─────────────────────────────────────────────
# PROMPTS — tightly scoped, example-heavy
# ─────────────────────────────────────────────
PROMPTS = {
    "hooks": """Topic: {topic}

Write 10 viral TikTok opening hooks. These are the first words that appear on the video screen.

Study these real examples first:
- i was not supposed to post this 😭
- this thing pain me lowkey
- nobody asked me this in 4 years 💀
- i can't be the only one abeg
- the way i SCREAMED
- they really thought i wasn't paying attention
- babe i found out something
- ngl this one hit different
- i tried to act unbothered and FAILED
- POV: you just remembered that thing 😭

Rules:
- under 12 words each
- no punctuation at sentence end unless it's an emoji
- lowercase feels more human
- mix emotions: funny, painful, dramatic, chaotic, relatable
- sound like breaking news from someone's life

Output: just the 10 hooks, numbered 1-10. Nothing else.""",

    "captions": """Topic: {topic}

Write 15 TikTok captions. These go below the video.

Study these real Nigerian TikTok captions:
- this one pain me lowkey
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
- this your bestie era 💅

Rules:
- short. 1-8 words mostly
- no full storytelling
- mix: aesthetic, soft, unbothered, chaotic, funny
- lowercase preferred
- feel like something a real person typed in 5 seconds

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
- No explanation, no commentary

Output format:
Set 1: #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag""",

    "bio": """Topic/niche: {topic}

Write 8 short TikTok bios for this creator.

Study these real TikTok bio styles:
- just a girl who loves chaos and carbs 🌸
- lagos bred. content made. no apologies
- i post when the spirit moves me
- your fave's fave tbh
- soft life in progress 🕊️
- unlearning everything slowly
- professional overthinker | amateur human
- it's giving main character and i'm not sorry

Rules:
- under 80 characters
- sound like a real person wrote it
- mix: funny, aesthetic, unbothered, Nigerian-coded
- no cheesy job descriptions

Output: just 8 bios, numbered 1-8. Nothing else.""",

    "pov": """Topic: {topic}

Write 10 POV ideas for TikTok videos.

Study these real examples:
- POV: you finally stop replying someone that was stressing you
- POV: your sapa hits the moment you say "soft life"
- POV: you pretend you don't care but check their story 3x
- POV: you enter Lagos traffic with full confidence and a full tank
- POV: you're the friend that remembers everything
- POV: you realize the main character was you the whole time
- POV: you get the bag and suddenly everyone is a cousin
- POV: you hear your name in a conversation you weren't in
- POV: you finally eat the good food you've been saving
- POV: you gave them a second chance and THIRD thing happened

Rules:
- one sentence each
- no comma-heavy run-ons
- relatable, specific, Nigerian where it fits naturally
- funny, painful, dramatic, or quietly real

Output: just the 10 POVs, numbered 1-10. Nothing else.""",

    "script": """Topic/idea: {topic}

Write a short TikTok video script. Under 60 seconds when read aloud.

Format:
[HOOK] — the first 3 seconds (text on screen OR what they say)
[BODY] — the main content, broken into short punchy lines
[ENDING] — call to action or mic-drop line

Style:
- sounds like how a Nigerian creator actually speaks
- casual, like talking to a close friend on camera
- short sentences, natural pauses
- if using Pidgin, use it naturally not forced
- funny or emotionally real

Output: the script only. No extra explanation.""",

    "trends": """Current TikTok niche: {topic}

Suggest 8 trending video concepts this creator should film RIGHT NOW.

For each idea give:
- the concept in one line
- what text goes on screen (hook)
- why it would do well (one sentence max)

Style:
- specific and actionable
- sounds like advice from someone on Naija TikTok daily
- no generic "share your story" advice

Output: 8 numbered ideas in that format. Nothing else."""
}

# ─────────────────────────────────────────────
# COMMAND MENU
# ─────────────────────────────────────────────
COMMANDS_HELP = """🔥 *TikGenius Commands*

*Content:*
/hooks \[topic\] — viral opening lines
/captions \[topic\] — short captions
/hashtags \[topic\] — 5 hashtag sets
/pov \[topic\] — POV video ideas
/bio \[niche\] — TikTok bio options
/script \[idea\] — full video script ✨
/trends \[niche\] — what to film now ✨

*Account:*
/plan — check your plan
/upgrade — go Pro

*Examples:*
/hooks soft life lagos
/script my morning routine as a slay queen
/trends relationship content

New: /script and /trends for Pro users 🚀"""


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


def activate_pro(user_id):
    users = load_users()
    expires = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    users[str(user_id)] = {
        "plan": "pro",
        "expires": expires,
        "activated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    }
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
def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.92,       # high creativity but not random garbage
            max_tokens=800,
            top_p=0.95,             # nucleus sampling for more natural output
            frequency_penalty=0.4,  # reduces repetition across items
            presence_penalty=0.3    # encourages topic variety
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
            "plan": "pro"
        }
    }
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=20
        ).json()
        if res.get("status"):
            return res["data"]["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None


# ─────────────────────────────────────────────
# LOADING MESSAGES
# ─────────────────────────────────────────────
LOADING = [
    "🧠 hold on make i cook this properly...",
    "🔥 this one go burst, just wait",
    "⚡ checking what's landing on Naija FYP rn...",
    "👀 finding something your followers won't skip...",
    "🎬 relax... something mad is loading",
    "📈 oya make we touch FYP small...",
    "🇳🇬 thinking like a Lagos creator with 500k followers...",
    "💅 cooking your content era right now...",
    "🕊️ soft life content incoming...",
    "😭 this one go make them comment their whole life story...",
]

# Commands available to free users
FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
# Commands only for Pro users
PRO_COMMANDS = {"/script", "/trends"}
ALL_CONTENT_COMMANDS = FREE_COMMANDS | PRO_COMMANDS


# ─────────────────────────────────────────────
# WEBHOOK — TELEGRAM
# ─────────────────────────────────────────────
@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200


@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    message = data.get("message", {})
    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")
    user_id = user.get("id")
    username = user.get("username", "")
    first_name = user.get("first_name", "Creator")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]
    topic = parts[1].strip() if len(parts) > 1 else ""

    # ── /start ──
    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

Your AI TikTok content plug. Built for Nigerian creators.

What I can make for you:
/hooks fashion → viral opening lines
/captions skincare → short captions
/hashtags football → hashtag sets
/pov relationship → POV ideas
/bio content creator → bio options
/script morning routine → full video script ⭐
/trends lifestyle → what to film now ⭐

⭐ = Pro only

Free plan: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

Type /help to see all commands
Type /upgrade to go Pro""")

    # ── /help ──
    elif command == "/help":
        send_message(chat_id, COMMANDS_HELP, parse_mode="Markdown")

    # ── /plan ──
    elif command == "/plan":
        users = load_users()
        if is_pro(user_id):
            exp = users[str(user_id)]["expires"]
            send_message(chat_id, f"""✅ TikGenius Pro — Active

Expires: {exp}
Usage: Unlimited
Commands: All unlocked

Your page go blow. Keep posting. 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""📊 Free Plan

Uses left today: {remaining}/{FREE_LIMIT}
Resets: midnight UTC

Pro unlocks:
✅ Unlimited uses
✅ /script — full video scripts
✅ /trends — what to film now
✅ Priority AI quality

Type /upgrade to go Pro for ₦2,000/month""")

    # ── /upgrade ──
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you unlock:
✅ Unlimited hooks, captions, hashtags, POVs, bios
✅ /script — AI writes your full video script
✅ /trends — trending video ideas for your niche
✅ Faster, better quality AI output

Pay here 👇
{link}

Activation is automatic after payment ⚡""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try /upgrade again in a moment.")

    # ── Content commands ──
    elif command in ALL_CONTENT_COMMANDS:
        mode = command.replace("/", "")

        # Pro-only gate
        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            msg = f"🔒 {command} is a Pro feature.\n\n"
            if link:
                msg += f"Upgrade to unlock it:\n{link}"
            else:
                msg += "Use /upgrade to go Pro."
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Topic required
        if not topic:
            examples = {
                "hooks": "/hooks soft life lagos",
                "captions": "/captions my glow up era",
                "hashtags": "/hashtags Nigerian food",
                "pov": "/pov you finally left a bad situation",
                "bio": "/bio lifestyle and fashion creator",
                "script": "/script how I saved ₦500k in 6 months",
                "trends": "/trends relationship content"
            }
            send_message(chat_id, f"Add a topic 👇\n\nExample:\n{examples.get(mode, command + ' [topic]')}")
            return jsonify({"ok": True})

        # Free limit check
        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            msg = "⏳ You don finish your free uses for today.\n\nUpgrade for unlimited access:\n"
            msg += link if link else "/upgrade"
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Generate content
        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING))
        send_typing(chat_id)

        result = ask_ai(mode, topic)
        send_message(chat_id, result[:4000])

        # Nudge free users near limit
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining == 0:
                link = create_payment_link(user_id, username)
                msg = "⚡ That was your last free use today.\n\nGo unlimited:\n"
                msg += link if link else "/upgrade"
                send_message(chat_id, msg)
            elif remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today. /upgrade to go unlimited.")

    else:
        send_message(chat_id, "Unknown command.\n\nType /help to see everything I can do.")

    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# WEBHOOK — PAYSTACK
# ─────────────────────────────────────────────
@flask_app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body = request.get_data()

    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512
    ).hexdigest()

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
            send_message(
                telegram_id,
                f"""🎉 Payment confirmed! Welcome to Pro 🚀

Valid until: {expires}
Status: Unlimited access activated

New commands unlocked:
/script [idea] — AI writes your full video
/trends [niche] — what to post this week

Go try it:
/script your first idea 🔥"""
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    flask_app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000))
    )
