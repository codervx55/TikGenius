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
# SYSTEM PROMPT
# ─────────────────────────────────────────────
SYSTEM_PROMPT = """You are TikGenius. You write Nigerian TikTok content that actually goes viral.

You think and write like real Nigerian TikTok creators — the ones with 100k–1M followers.
Your output sounds like a real person typed it on their phone, not an AI.

Your voice naturally blends Nigerian Pidgin and English the way Nigerians actually talk online:
- Sometimes full Pidgin: "this one pain me die", "abeg who collect my shine"
- Sometimes clean English: "I wasn't supposed to post this", "the signs were there"
- Sometimes mixed: "this thing pain me lowkey", "ngl this hit different abeg"

You understand Nigerian TikTok culture deeply:
- Sapa, soft life, main character era, toxic era
- Lagos life, hustle culture, Gen Z Nigerian humor
- Afrobeats references, Nollywood moments
- The way Nigerians express heartbreak, joy, frustration, unbothered energy

NEVER write:
- Motivational quotes or preaching
- Long grammar-perfect sentences
- "Here are your hooks:" or any AI preamble
- Forced Pidgin that sounds like Google Translate
- Content that sounds international or generic

ALWAYS write:
- Short. Punchy. Emotionally true.
- Like it belongs on a Nigerian TikTok screen right now
- Things that make people stop scrolling, laugh, feel seen, or send to their group chat
"""

# ─────────────────────────────────────────────
# PROMPTS
# ─────────────────────────────────────────────
PROMPTS = {

    "hooks": """Topic: {topic}

Write 10 viral TikTok opening hooks for this topic.
These are the first words that flash on the video screen in the first 2 seconds.

Learn from these real Nigerian TikTok hooks that went viral:
- i was not supposed to post this 😭
- this one pain me die
- abeg who can relate to this
- the way i SCREAMED when i found out
- they really thought i wasn't watching 💀
- ngl this hit different
- babe i have to tell you something
- i tried to act unbothered and FAILED
- nobody is talking about this and i'm mad
- wait till you see what happened next
- POV: you finally stopped explaining yourself
- i gave them the benefit of the doubt and look

Rules:
- under 12 words each
- no full stop at the end unless it's an emoji
- lowercase feels more human and real
- mix the emotions: funny, painful, dramatic, unhinged, relatable
- some in Pidgin, some in English, some mixed — like a real Nigerian would write

Output ONLY the 10 hooks numbered 1–10. No intro. No explanation.""",

    "captions": """Topic: {topic}

Write 15 short Nigerian TikTok captions for this topic.
These go under the video. People read them in 2 seconds.

Learn from these real captions that worked:
- this one pain me lowkey
- i can explain sha 🤦‍♀️
- i miss my old self
- no because WHY would you do that
- na me cause am honestly
- mentally i'm somewhere else
- love no supposed to hard like this
- i saw the signs btw
- i dey act okay
- soft life pls 🕊️
- my toxic trait is thinking i'm fine
- carried myself out and left
- they really did that 😭
- nobody here is fully okay and that's fine
- this your healing era 💅
- the audacity ehn
- sapa don locate me again
- i no go lie this one stress me
- quietly collecting myself
- not everything needs a response

Rules:
- 1 to 8 words mostly, occasionally up to 12
- no storytelling, no long explanations
- mix Pidgin, English, and mixed naturally
- aesthetic, soft, unbothered, chaotic, funny — vary the mood
- feel like something typed fast by a real person

Output ONLY the 15 captions numbered 1–15. No intro. No explanation.""",

    "hashtags": """Topic: {topic}

Create 5 ready-to-copy TikTok hashtag sets for this topic.

Rules:
- exactly 6 hashtags per set
- Set 1: maximum reach (#fyp, #viral type)
- Set 2: specific to the topic/niche
- Set 3: Nigerian TikTok audience (#naijatiktok, #lagoslife type)
- Set 4: mood and emotion tags
- Set 5: best combined strategy for this topic

No explanation. Just the sets.

Output format exactly like this:
Set 1: #tag #tag #tag #tag #tag #tag
Set 2: #tag #tag #tag #tag #tag #tag
Set 3: #tag #tag #tag #tag #tag #tag
Set 4: #tag #tag #tag #tag #tag #tag
Set 5: #tag #tag #tag #tag #tag #tag""",

    "bio": """Creator niche/vibe: {topic}

Write 8 short TikTok bios for this creator. People read bios in 3 seconds to decide if they'll follow.

Learn from these real TikTok bios that work:
- just a girl who loves chaos and jollof 🌸
- lagos bred. content made. no apologies.
- i post when the spirit moves me
- your fave's fave tbh
- soft life in progress 🕊️
- unlearning everything slowly
- professional overthinker | amateur human
- it's giving main character and i'm not sorry
- sapa survivor. content creator. still here.
- na God dey do am for me honestly

Rules:
- under 80 characters each
- mix Pidgin, English, and mixed styles
- funny, aesthetic, unbothered, or Nigerian-coded
- no cheesy job titles like "content creator | lifestyle blogger"
- sound like a real person, not a CV

Output ONLY the 8 bios numbered 1–8. No intro. No explanation.""",

    "pov": """Topic: {topic}

Write 10 POV video ideas for Nigerian TikTok.
POV videos are first-person scenarios. The text goes on screen and people relate hard.

Learn from these real POVs that went viral in Nigeria:
- POV: you finally stop replying someone that was stressing you
- POV: sapa locates you the same day you say soft life
- POV: you pretend you don't care but check their story 3 times
- POV: you enter Lagos traffic with full confidence and a full tank
- POV: you be the friend wey remember everything
- POV: you get the bag and suddenly cousins appear everywhere
- POV: you hear your name in a conversation you weren't part of
- POV: you finally chop the good food you've been saving for no reason
- POV: you gave them a second chance and a third problem showed up
- POV: you realize say na you be the main character the whole time

Rules:
- one sentence each
- specific and relatable, not vague
- mix Pidgin, English, and mixed — like real Nigerian TikTok
- funny, painful, dramatic, or quietly real

Output ONLY the 10 POVs numbered 1–10. No intro. No explanation.""",

    "script": """Video idea: {topic}

Write a full TikTok video script for a Nigerian creator. Should be under 60 seconds when spoken.

Format:
[HOOK] — the first 3 seconds. Text on screen OR the first thing they say out loud.
[BODY] — the main content. Short punchy lines, one idea per line, like how people actually talk.
[ENDING] — the last line. A mic-drop, a question to drive comments, or a call to action.

Style:
- Talk like a Nigerian creator speaking to their audience, not reading a script
- Mix Pidgin and English naturally
- Short sentences. Real pauses. Actual emotion.
- Funny, relatable, or emotionally real — not motivational

Output ONLY the script in that format. No explanation.""",

    "trends": """TikTok niche: {topic}

Give 8 specific video ideas this Nigerian creator should film RIGHT NOW to get views.

For each idea write:
Idea: [what the video is about in one line]
Hook: [exact text to show on screen in first 2 seconds]
Why it works: [one sentence on why Nigerians will watch and share this]

Make the ideas specific to what's actually working on Nigerian TikTok —
real situations, relatable moments, current culture.
No generic advice like "share your story" or "be authentic".

Output ONLY the 8 ideas in that format. No intro.""",
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
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
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
def ask_ai(mode: str, topic: str) -> str:
    prompt = PROMPTS[mode].format(topic=topic)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
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
        return "⚠️ TikGenius dey busy right now. Try again small time."


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
# LOADING MESSAGES
# ─────────────────────────────────────────────
LOADING = [
    "🧠 hold on make i cook this properly...",
    "🔥 this one go burst, just wait",
    "⚡ checking wetin dey land on Naija FYP rn...",
    "👀 finding content your followers won't skip...",
    "🎬 relax... something mad is loading",
    "📈 oya make we touch FYP small...",
    "🇳🇬 thinking like a Lagos creator with 500k followers...",
    "💅 your content era is loading...",
    "🕊️ soft life content incoming...",
    "😭 this one go make them comment their whole life story...",
    "🔥 cooking something your FYP has never seen...",
    "⚡ almost ready... this one go hit different",
]

FREE_COMMANDS = {"/hooks", "/captions", "/hashtags", "/pov", "/bio"}
PRO_COMMANDS  = {"/script", "/trends"}
ALL_CONTENT   = FREE_COMMANDS | PRO_COMMANDS

EXAMPLES = {
    "hooks":    "/hooks soft life lagos",
    "captions": "/captions my glow up era",
    "hashtags": "/hashtags Nigerian food",
    "pov":      "/pov you finally left a toxic situation",
    "bio":      "/bio lifestyle and fashion creator",
    "script":   "/script how I saved ₦500k in 6 months",
    "trends":   "/trends relationship content",
}


# ─────────────────────────────────────────────
# WEBHOOK — TELEGRAM
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

    # ── /start ──
    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

Your AI TikTok content plug. Built for Nigerian creators.

Commands:
/hooks [topic]     → viral opening lines
/captions [topic]  → short captions
/hashtags [topic]  → 5 hashtag sets
/pov [topic]       → POV video ideas
/bio [niche]       → bio options
/script [idea]     → full video script ⭐
/trends [niche]    → what to film now ⭐

⭐ = Pro only

Free: {FREE_LIMIT} uses/day
Pro: ₦2,000/month — unlimited everything

/upgrade to go Pro""")

    # ── /plan ──
    elif command == "/plan":
        users = load_users()
        if is_pro(user_id):
            exp = users[str(user_id)]["expires"]
            send_message(chat_id, f"""✅ TikGenius Pro — Active

Expires: {exp}
Usage: Unlimited

Keep posting. Your page go blow 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""📊 Free Plan

Uses left today: {remaining}/{FREE_LIMIT}
Resets at midnight UTC

Pro unlocks:
✅ Unlimited uses
✅ /script — full video scripts
✅ /trends — what to film now

/upgrade → ₦2,000/month""")

    # ── /upgrade ──
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro — ₦2,000/month

What you unlock:
✅ Unlimited hooks, captions, hashtags, POVs, bios
✅ /script — AI writes your full video script
✅ /trends — trending ideas for your niche

Pay here 👇
{link}

Activation is automatic after payment ⚡""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try /upgrade again.")

    # ── Content commands ──
    elif command in ALL_CONTENT:
        mode = command.replace("/", "")

        # Pro gate
        if command in PRO_COMMANDS and not is_pro(user_id):
            link = create_payment_link(user_id, username)
            msg  = f"🔒 {command} is a Pro feature.\n\n"
            msg += f"Upgrade to unlock:\n{link}" if link else "Use /upgrade to go Pro."
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # No topic
        if not topic:
            send_message(chat_id, f"Add a topic 👇\n\nExample:\n{EXAMPLES.get(mode, command + ' [topic]')}")
            return jsonify({"ok": True})

        # Free limit
        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            msg  = "⏳ Free uses don finish for today.\n\nUpgrade for unlimited:\n"
            msg += link if link else "/upgrade"
            send_message(chat_id, msg)
            return jsonify({"ok": True})

        # Generate
        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING))
        send_typing(chat_id)

        result = ask_ai(mode, topic)
        send_message(chat_id, result[:4000])

        # Nudge near limit
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining == 0:
                link = create_payment_link(user_id, username)
                msg  = "⚡ That was your last free use today.\n\nGo unlimited:\n"
                msg += link if link else "/upgrade"
                send_message(chat_id, msg)
            elif remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today. /upgrade to go unlimited.")

    else:
        send_message(chat_id, "Unknown command.\n\nTry /hooks, /captions, /pov, /bio, /hashtags\nOr type /start")

    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# WEBHOOK — PAYSTACK
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
Unlimited access activated ✅

Unlocked:
/script [idea]  — full video script
/trends [niche] — what to film this week

Go try it 🔥
/script your first idea""",
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
