import os, json, hmac, hashlib, random
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
ADMIN_ID = "7375528876"

flask_app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)

SYSTEM_PROMPT = """
You are TikGenius, a Nigerian TikTok content plug.

Create TikTok text that feels like real Nigerian creators:
soft-life, soft-pain, funny, emotional, dramatic, aesthetic, unserious.

Rules:
- short
- human
- copy-ready
- no explanation
- no long sentences
- no motivational quotes
- no AI tone
- no old Facebook captions
- no forced pidgin
- assume audience is Nigerian
"""

PROMPTS = {
    "hooks": """Create 10 Nigerian TikTok opening texts for: {topic}

Style:
- feels like text on a viral TikTok video
- casual random thought
- emotional, funny, dramatic, or relatable
- make people want to keep watching
- lowercase is okay

Examples:
- omo this thing pain me
- i really thought today would be different
- why does this always happen when i’m happy
- i can’t even explain this feeling
- make nobody stress me abeg
- i just knew something was off

Output only numbered hooks.""",

    "captions": """Create 15 ultra-short Nigerian TikTok captions for: {topic}

Style:
- soft pain
- toxic sometimes
- aesthetic
- unserious
- sounds like TikTok girls/lifestyle creators
- no story, no explanation

Examples:
- this one pain me lowkey
- i can explain sha
- i miss my old self
- na me cause am
- mentally i’m tired
- i saw the signs btw

Output only numbered captions.""",

    "pov": """Create 10 short Nigerian TikTok POV ideas for: {topic}

Style:
- one sentence each
- realistic
- dramatic
- funny or emotional
- sounds like real TikTok video text

Examples:
- POV: you finally stop replying someone that was stressing you
- POV: your money finishes immediately after you say soft life
- POV: you pretend you don't care but you check their story

Output only numbered POVs.""",

    "hashtags": """Create 5 TikTok hashtag sets for: {topic}

Rules:
- 6 hashtags per set
- mix broad, niche, and Nigerian tags
- no explanation

Output:
Set 1:
Set 2:
Set 3:
Set 4:
Set 5:""",

    "bio": """Create 8 short TikTok bios for: {topic}

Rules:
- under 80 characters
- cool
- clean
- human
- creator-friendly
- Nigerian where useful

Output only numbered bios."""
}

LOADING_MESSAGES = {
    "hooks": [
        "🧠 Omo relax... make we think like TikTok girls small",
        "🔥 Searching wetin fit blow for Naija FYP...",
        "👀 This hook suppose touch people's chest...",
        "🎬 Cooking something dangerously relatable..."
    ],
    "captions": [
        "💅 Generating soft-life caption...",
        "😭 Adding small emotional damage...",
        "🪄 Making it look effortlessly viral...",
        "🥹 This one dey sweet already..."
    ],
    "pov": [
        "🎥 Oya imagine this scene first...",
        "🍿 Wait first... this POV fit mad",
        "👀 This one get drama small...",
        "😭 Somebody definitely go relate to this..."
    ],
    "hashtags": [
        "📊 Finding hashtags wey TikTok go like...",
        "🚀 Oya make algorithm favor you...",
        "🔥 Mixing viral tags together..."
    ],
    "bio": [
        "✨ Soft-life bio loading...",
        "📱 Creating bio wey dey attractive...",
        "🪄 This bio fit collect followers..."
    ]
}

EXTRA_REPLIES = [
    "😭 nah this one too real",
    "🔥 this content dey smell viral",
    "🫠 omo this one hard small",
    "💀 people go relate die",
    "📈 this one fit touch FYP"
]


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
    return datetime.strptime(user["expires"], "%Y-%m-%d") >= datetime.utcnow()


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


def ask_ai(mode, topic):
    prompt = PROMPTS[mode].format(topic=topic)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.98,
            max_tokens=650
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq error: {e}")
        return "⚠️ TikGenius AI dey busy right now. Try again small."


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

    if command == "/start":
        send_message(chat_id, f"""🔥 Oya {first_name}, welcome to TikGenius 🇳🇬

Your AI TikTok content plug.

Commands:
/hooks fashion
/captions relationship
/hashtags football
/pov soft life
/bio content creator

Free plan: {FREE_LIMIT} uses/day
Pro: unlimited access

/plan - check plan
/upgrade - go Pro for ₦2,000/month""")

    elif command == "/activatepro":
        if str(user_id) == ADMIN_ID:
            expires = activate_pro(user_id)
            send_message(chat_id, f"✅ Pro activated manually.\n\nExpires: {expires}")
        else:
            send_message(chat_id, "❌ You are not allowed to use this command.")

    elif command == "/plan":
        users = load_users()
        if is_pro(user_id):
            exp = users[str(user_id)]["expires"]
            send_message(chat_id, f"""✅ TikGenius Pro Active

Expires: {exp}
Usage: Unlimited

No dulling. Your page go blow 🔥""")
        else:
            remaining = free_uses_remaining(user_id)
            send_message(chat_id, f"""🆓 Free Plan

Uses left today: {remaining}/{FREE_LIMIT}

Upgrade:
/upgrade""")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)
        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro

Price: ₦2,000/month

You get:
✅ Unlimited hooks
✅ Captions
✅ Hashtags
✅ POV ideas
✅ Bios

Pay here:
{link}

Activation is automatic after payment.""")
        else:
            send_message(chat_id, "⚠️ Payment link failed. Try again.")

    elif command in ["/hooks", "/captions", "/hashtags", "/bio", "/pov"]:
        mode = command.replace("/", "")

        if not topic:
            send_message(chat_id, f"Example:\n{command} relationship")
            return jsonify({"ok": True})

        if not check_and_increment_free_usage(user_id):
            link = create_payment_link(user_id, username)
            if link:
                send_message(chat_id, f"⏳ Free limit don finish.\n\nGo Pro:\n{link}")
            else:
                send_message(chat_id, "⏳ Free limit don finish.\nUse /upgrade to continue.")
            return jsonify({"ok": True})

        send_typing(chat_id)
        send_message(chat_id, random.choice(LOADING_MESSAGES.get(mode, ["🔥 Cooking something viral..."])))

        result = ask_ai(mode, topic)
        send_message(chat_id, f"✨ TikGenius Results\n\n{result[:3800]}")

        if random.random() < 0.45:
            send_message(chat_id, random.choice(EXTRA_REPLIES))

        if not is_pro(user_id):
            remaining = free_uses_remaining(user_id)
            if remaining <= 2:
                send_message(chat_id, f"💡 {remaining} free use(s) left today. Use /upgrade to go unlimited.")

    else:
        send_message(chat_id, "Unknown command. Use /start")

    return jsonify({"ok": True})


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
                f"""🎉 Payment confirmed!

Welcome to TikGenius Pro 🚀

Valid until: {expires}
Unlimited access don open.

Try: /hooks your niche"""
            )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
