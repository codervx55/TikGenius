import os
import json
import hmac
import hashlib
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
from groq import Groq

# ── Config ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY       = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

PRICE_KOBO  = 200_000          # ₦2,000
USERS_FILE  = "users.json"
FREE_LIMIT  = 5                # free uses per day before paywall

flask_app   = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

# ── System prompt (governs every AI response) ────────────────────────────────
SYSTEM_PROMPT = """You are TikGenius — a world-class TikTok growth strategist and content coach.
You specialize in the Nigerian creator ecosystem but your strategies work globally.

Your responses are always:
• Practical and immediately usable — no filler, no fluff
• Written in punchy, Gen-Z-native language
• Formatted with clear emojis, numbered lists, and section headers
• Tailored to the EXACT niche the user gives you
• Optimized for the For You Page (FYP) algorithm

You understand:
- TikTok SEO, hooks, trending audio strategy
- Nigerian slang and culture (Naija vibes where relevant)
- POV storytelling, day-in-my-life formats, stitch/duet tactics
- Monetization: TikTok Shop, brand deals, live gifts, link-in-bio funnels
- Short-form psychology: pattern interrupts, curiosity gaps, emotional triggers

Never give generic advice. Every output should feel like it came from a creator who has gone viral multiple times."""

# ── Prompt templates per command ─────────────────────────────────────────────
PROMPTS = {
    "ideas": """Generate 7 highly specific, viral TikTok video ideas for the niche: **{topic}**

Format each idea like this:
🎬 **Idea [N]: [Catchy Title]**
📌 Concept: [2-sentence description of the video]
🎣 Hook (first 3 sec): [Exact words to say or show on screen]
🎵 Audio vibe: [Type of sound/trend to use]
💡 Why it'll blow up: [1 specific reason tied to TikTok algorithm or human psychology]

Make all 7 ideas different formats: tutorial, storytime, POV, reaction, list, challenge, "secrets of".
End with:
⚡ **Pro Tip:** [One advanced FYP growth tip specific to this niche]""",

    "hooks": """Write 10 scroll-stopping TikTok hooks for the niche: **{topic}**

Rules:
- Each hook must work as on-screen text OR as spoken words
- Use pattern interrupts, controversy, curiosity gaps, or bold claims
- Max 12 words per hook

Format:
🔥 Hook [N]: "[The hook]"
🧠 Psychology: [Why this stops the scroll — 1 line]

Include these hook types:
1. Shocking stat
2. Controversial opinion
3. "Nobody talks about this…"
4. Direct challenge
5. Relatable struggle
6. "POV:"
7. Countdown/list
8. Before/after
9. Mistake warning
10. Secret reveal

End with:
✅ **Best hook for right now:** Hook [N] — [reason why it fits current TikTok trends]""",

    "captions": """Write 6 TikTok captions for the niche: **{topic}**

Each caption should:
- Be 1-3 lines max (TikTok cuts off long captions)
- End with a question OR call-to-action to boost comments
- Include 5 relevant hashtags (mix of niche + trending)

Format:
📝 **Caption [N]:**
[The caption text]
[Hashtags]
🎯 Goal: [What this caption optimizes for — saves, comments, shares, or follows]

Caption styles to cover: Storytelling, Question, Controversial take, Relatable, Motivational, Humorous""",

    "scripts": """Write a complete, ready-to-film TikTok script for the niche: **{topic}**

Structure:
🎬 **VIDEO SCRIPT: [Title]**
⏱️ Estimated length: [15s / 30s / 60s]
📐 Format: [Talking head / Voiceover / POV / Tutorial]

---
[HOOK — 0-3 sec]
[Exact words + action]

[BODY — 3-45 sec]
[Beat-by-beat breakdown with timestamps]
[What to show on screen vs what to say]

[CTA — last 3-5 sec]
[Exact words for call to action]
---

🎵 Sound suggestion: [Specific vibe or trending audio type]
📸 B-roll ideas: [3 quick visual suggestions]
💬 Caption: [Ready-to-copy caption + hashtags]
⚡ Virality score: [X/10] — [Short reason]""",

    "hashtags": """Generate the perfect hashtag strategy for a TikTok creator in the **{topic}** niche.

Provide 3 tiers:

🏆 **Tier 1 — Mega (100M+ views)** [use 1-2 max]
List 4 hashtags with estimated post counts

📈 **Tier 2 — Mid (1M–50M views)** [use 3-4]
List 6 hashtags with estimated post counts

🎯 **Tier 3 — Niche (under 1M)** [use 2-3]  
List 5 hashtags with estimated post counts

💡 **Optimal combo for FYP reach:**
[Give the exact 8-10 hashtag combo to copy-paste]

🔍 **3 Underrated hashtags in this niche right now:**
[Hashtags most creators overlook but are growing fast]

⚠️ **Never use these:** [2-3 shadowbanned or oversaturated hashtags to avoid]""",

    "bio": """Write 5 high-converting TikTok bio options for a creator in: **{topic}**

Each bio must be under 80 characters (TikTok limit) and include:
- Clear value proposition (what the viewer gets)
- Personality or hook
- CTA (follow, link, DM)

Format:
✍️ **Bio [N]:**
[The bio text]
📊 Tone: [Professional / Funny / Bold / Mysterious / Relatable]
🎯 Best for: [Type of creator this works for]

After the 5 bios, add:
📌 **Profile optimization checklist:**
- Profile photo tip for {topic} niche
- Username tip
- Pinned video strategy
- Link-in-bio recommendation""",

    "pov": """Create 3 complete POV TikTok concepts for the niche: **{topic}**

For each POV, provide:

🎭 **POV [N]: [POV Title]**

📱 On-screen text: "[Exact POV text — starts with 'POV:']"

🎬 Scene setup: [What the creator is doing / environment / camera angle]

🗣️ Script:
- Opening (0-3s): [What to say/do]
- Middle (3-25s): [The story beats]
- Twist/Peak (25-35s): [Emotional high point]
- Ending (35-45s): [Resolution + CTA]

🎵 Audio: [Specific song vibe or trending sound type]

💬 Caption: [Ready caption]
🏷️ Hashtags: [8 relevant hashtags]

💡 Comment bait question: [A question in the comments to spark debate/replies]"""
}

# ── Persistence ───────────────────────────────────────────────────────────────
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

# ── Free usage tracking ───────────────────────────────────────────────────────
def check_and_increment_free_usage(user_id):
    """Returns True if user is allowed to proceed (within free limit)."""
    if is_pro(user_id):
        return True

    users = load_users()
    uid = str(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")

    user = users.setdefault(uid, {})
    usage = user.get("usage", {})

    # Reset daily counter
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
    uid = str(user_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    usage = users.get(uid, {}).get("usage", {})
    if usage.get("date") != today:
        return FREE_LIMIT
    return max(0, FREE_LIMIT - usage.get("count", 0))

# ── Telegram helpers ──────────────────────────────────────────────────────────
def send_message(chat_id, text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram send error: {e}")

def send_typing(chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass

# ── AI ────────────────────────────────────────────────────────────────────────
def ask_ai(mode, topic):
    prompt_template = PROMPTS.get(mode, "")
    user_prompt = prompt_template.format(topic=topic)

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt}
            ],
            temperature=0.85,
            max_tokens=1800,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq error: {e}")
        return "⚠️ TikGenius AI is momentarily busy. Please try again in a few seconds."

# ── Payment ───────────────────────────────────────────────────────────────────
def create_payment_link(user_id, username):
    reference = f"TG-{user_id}-{int(datetime.utcnow().timestamp())}"
    payload = {
        "email": f"{user_id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "callback_url": "https://t.me/TikGeniusBot",
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
            json=payload, headers=headers, timeout=20
        ).json()
        if res.get("status"):
            return res["data"]["authorization_url"]
        return None
    except Exception as e:
        print(f"Paystack error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────────────────────
@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200

@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    message = data.get("message", {})
    chat    = message.get("chat", {})
    user    = message.get("from", {})

    chat_id  = chat.get("id")
    user_id  = user.get("id")
    username = user.get("username", "")
    first    = user.get("first_name", "Creator")
    text     = message.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts   = text.split(" ", 1)
    command = parts[0].lower().split("@")[0]  # strip @BotName suffix
    topic   = parts[1].strip() if len(parts) > 1 else ""

    # ── /start ────────────────────────────────────────────────────────────────
    if command == "/start":
        send_message(chat_id, f"""🎯 *Welcome to TikGenius, {first}!*

Your personal TikTok growth strategist — powered by AI.

━━━━━━━━━━━━━━━━━━━━
🛠 *What I can do for you:*

/ideas `[niche]` → 7 viral video concepts
/hooks `[niche]` → 10 scroll-stopping openers
/captions `[niche]` → 6 ready-to-post captions
/scripts `[niche]` → Full ready-to-film script
/hashtags `[niche]` → Tiered hashtag strategy
/bio `[niche]` → 5 high-converting bio options
/pov `[niche]` → 3 POV video concepts

━━━━━━━━━━━━━━━━━━━━
💡 *Example:*
`/ideas skincare` → instant viral ideas for skincare

/plan — check your plan
/upgrade — go Pro (₦2,000/month)

Free plan: *{FREE_LIMIT} uses/day* • Pro: *Unlimited* 🚀""")

    # ── /plan ─────────────────────────────────────────────────────────────────
    elif command == "/plan":
        users = load_users()
        if is_pro(user_id):
            exp = users[str(user_id)]["expires"]
            send_message(chat_id, f"""✅ *TikGenius Pro — Active*

📅 Expires: `{exp}`
⚡ Usage: Unlimited
🎯 All commands unlocked

You're on the best plan. Keep creating! 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""📊 *Your Plan: Free*

🔢 Uses remaining today: *{remaining}/{FREE_LIMIT}*

Upgrade to Pro for:
• ♾️ Unlimited daily uses
• ✨ Priority AI responses
• 🔓 All commands unlocked

→ /upgrade to get Pro for ₦2,000/month""")

    # ── /upgrade ──────────────────────────────────────────────────────────────
    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 *Upgrade to TikGenius Pro*

*What you get:*
• ♾️ Unlimited AI requests — no daily cap
• 🎯 All 7 content tools unlocked
• ⚡ Full-length scripts, ideas & more
• 🔥 New features as they drop

💳 *Price: ₦2,000 / month*

👇 Pay securely via Paystack:
{link}

✅ Your account activates *automatically* within seconds of payment.
Need help? Message @YourSupportHandle""")
        else:
            send_message(chat_id, "⚠️ Could not generate payment link. Please try again in a moment.")

    # ── AI commands ───────────────────────────────────────────────────────────
    elif command in ["/ideas", "/hooks", "/captions", "/scripts", "/hashtags", "/bio", "/pov"]:
        mode = command.replace("/", "")

        if not topic:
            examples = {
                "ideas":    "fashion, fitness, food, finance",
                "hooks":    "skincare, crypto, relationships",
                "captions": "business, travel, comedy",
                "scripts":  "motivation, cooking, tech",
                "hashtags": "beauty, gaming, lifestyle",
                "bio":      "content creator, chef, fitness coach",
                "pov":      "student life, 9-to-5 escape, side hustle"
            }
            send_message(chat_id, f"""ℹ️ *Usage:* `{command} [your niche]`

*Examples:*
`{command} {examples[mode]}`

Tell me your niche and I'll generate fire content! 🔥""")
            return jsonify({"ok": True})

        # Check free limit
        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            pay_line = f"\n\n💳 [Upgrade now — ₦2,000/month]({link})" if link else "\n\n→ /upgrade to continue"
            send_message(chat_id, f"""⏳ *Daily free limit reached ({FREE_LIMIT} uses)*

You've used all your free requests for today.

🚀 *Go Pro to get unlimited access:*
• No daily cap — use as much as you want
• All 7 tools unlocked
• ₦2,000/month{pay_line}""")
            return jsonify({"ok": True})

        # Show typing indicator then generate
        send_typing(chat_id)
        send_message(chat_id, f"🔄 Generating your {mode} for *{topic}*...\n\n_This takes 5-10 seconds ⏳_")

        result = ask_ai(mode, topic)

        # Split if too long (Telegram max = 4096 chars)
        if len(result) <= 4000:
            send_message(chat_id, result)
        else:
            chunks = [result[i:i+3900] for i in range(0, len(result), 3900)]
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    send_message(chat_id, chunk)
                else:
                    send_message(chat_id, f"_(continued...)_\n\n{chunk}")

        # Upsell footer for free users
        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 _{remaining} free use(s) left today. Use /upgrade to go unlimited._")

    # ── Unknown command ───────────────────────────────────────────────────────
    else:
        send_message(chat_id, "❓ Unknown command.\n\nType /start to see everything I can do.")

    return jsonify({"ok": True})


@flask_app.route("/paystack-webhook", methods=["POST"])
def paystack_webhook():
    signature = request.headers.get("x-paystack-signature", "")
    body      = request.get_data()

    expected = hmac.new(
        PAYSTACK_SECRET_KEY.encode(),
        body,
        hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return jsonify({"error": "invalid signature"}), 400

    event = request.json or {}

    if event.get("event") == "charge.success":
        data       = event["data"]
        amount     = data.get("amount")
        metadata   = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            expires = activate_pro(telegram_id)
            # Notify the user automatically
            send_message(
                telegram_id,
                f"""🎉 *Payment confirmed! Welcome to TikGenius Pro!*

✅ Your account is now activated.
📅 Valid until: `{expires}`
♾️ You now have unlimited access to all tools.

Start creating viral content now 🚀
Try: `/ideas your niche`"""
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
