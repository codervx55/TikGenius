import os
import json
import hmac
import hashlib
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
from groq import Groq

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

PRICE_KOBO = 200000
USERS_FILE = "users.json"

flask_app = Flask(__name__)
groq_client = Groq(api_key=GROQ_API_KEY)


def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def activate_pro(user_id):
    users = load_users()
    expires = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
    users[str(user_id)] = {"plan": "pro", "expires": expires}
    save_users(users)
    return expires


def is_pro(user_id):
    users = load_users()
    user = users.get(str(user_id))

    if not user or user.get("plan") != "pro":
        return False

    expires = datetime.strptime(user["expires"], "%Y-%m-%d")
    return expires >= datetime.utcnow()


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})


def ask_ai(prompt):
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        print(e)
        return "⚠️ TikGenius AI is busy right now.\nPlease try again shortly."


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
        print(e)
        return None


@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200


@flask_app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.json

    message = data.get("message", {})
    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")
    user_id = user.get("id")
    username = user.get("username", "")
    text = message.get("text", "")

    if not chat_id or not text:
        return jsonify({"ok": True})

    parts = text.split(" ", 1)
    command = parts[0].lower()
    topic = parts[1] if len(parts) > 1 else ""

    if command == "/start":
        send_message(chat_id, """🔥 Welcome to TikGenius

Your AI assistant for TikTok growth 🚀

Commands:
/ideas fashion
/hooks fitness
/captions skincare
/scripts business
/hashtags food
/bio content creator
/pov relationship
/upgrade
/plan""")

    elif command == "/plan":
        users = load_users()
        current_user = users.get(str(user_id))

        if is_pro(user_id):
            send_message(chat_id, f"✅ TikGenius Pro Active\n\nExpires:\n{current_user['expires']}")
        else:
            send_message(chat_id, "🆓 Free Plan\n\nUpgrade with:\n/upgrade")

    elif command == "/upgrade":
        link = create_payment_link(user_id, username)

        if link:
            send_message(chat_id, f"""🚀 TikGenius Pro

Price: ₦2,000/month

Pay here:
{link}

✅ Automatic activation after payment.""")
        else:
            send_message(chat_id, "⚠️ Payment link failed.")

    elif command in [
        "/ideas",
        "/hooks",
        "/captions",
        "/scripts",
        "/hashtags",
        "/bio",
        "/pov"
    ]:
        if not topic:
            send_message(chat_id, f"Example:\n{command} fashion")
            return jsonify({"ok": True})

        mode = command.replace("/", "")

        prompt = f"""
You are TikGenius, an AI assistant for TikTok creators.

Create TikTok-ready {mode} for:
{topic}

Use:
- Nigerian TikTok creator style
- catchy hooks
- short captions
- viral tone
- POV storytelling
- Gen Z style
- practical content ideas

If mode is pov, include:
1. POV text
2. Video scene
3. Hook
4. Caption
5. Hashtags
6. CTA

Avoid watermark removal discussion.
"""

        result = ask_ai(prompt)
        send_message(chat_id, result[:4000])

    else:
        send_message(chat_id, "Unknown command.\nUse /start")

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

    if signature != expected:
        return jsonify({"error": "invalid signature"}), 400

    event = request.json

    if event.get("event") == "charge.success":
        data = event["data"]
        amount = data.get("amount")
        metadata = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")

        if amount == PRICE_KOBO and telegram_id:
            activate_pro(telegram_id)

    return jsonify({"status": "ok"}), 200
