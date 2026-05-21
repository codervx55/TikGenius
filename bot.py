import os
import json
import hmac
import hashlib
import threading
import asyncio
from datetime import datetime, timedelta

import requests
from flask import Flask, request, jsonify
from google import genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

PRICE_KOBO = 200000
USERS_FILE = "users.json"

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
flask_app = Flask(__name__)


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
    users[str(user_id)] = {
        "plan": "pro",
        "expires": expires
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """🔥 Welcome to TikGenius

Your AI assistant for TikTok growth 🚀

Commands:
/ideas fashion
/hooks fitness
/captions skincare
/scripts business
/hashtags food
/bio content creator
/upgrade
/plan"""
    )


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users = load_users()
    user = users.get(str(user_id))

    if is_pro(user_id):
        await update.message.reply_text(
            f"✅ You are on TikGenius Pro\nExpires: {user['expires']}"
        )
    else:
        await update.message.reply_text(
            "🆓 You are on Free Plan\nUpgrade with /upgrade"
        )


async def upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    reference = f"TG-{user.id}-{int(datetime.utcnow().timestamp())}"

    payload = {
        "email": f"{user.id}@tikgenius.bot",
        "amount": PRICE_KOBO,
        "reference": reference,
        "metadata": {
            "telegram_id": user.id,
            "username": user.username or "",
            "plan": "pro"
        }
    }

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    res = requests.post(
        "https://api.paystack.co/transaction/initialize",
        json=payload,
        headers=headers,
        timeout=20
    ).json()

    if res.get("status"):
        link = res["data"]["authorization_url"]
        await update.message.reply_text(
            f"""🚀 Upgrade to TikGenius Pro

Price: ₦2,000/month

Pay here:
{link}

Your Pro access activates automatically after payment."""
        )
    else:
        await update.message.reply_text("Payment link failed. Please try again.")


def ask_ai(prompt):
    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt
    )
    return response.text


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    topic = " ".join(context.args)

    if not topic:
        await update.message.reply_text(f"Example: /{mode} fashion")
        return

    prompt = f"""
You are TikGenius, an AI assistant for TikTok creators.

Create {mode} for this niche/topic: {topic}

Use Nigerian TikTok creator style.
Make it practical, catchy, simple, and viral.
Do not mention watermark removal.
"""

    result = ask_ai(prompt)
    await update.message.reply_text(result[:4000])


async def hooks(update, context):
    await ai_command(update, context, "hooks")


async def ideas(update, context):
    await ai_command(update, context, "ideas")


async def captions(update, context):
    await ai_command(update, context, "captions")


async def scripts(update, context):
    await ai_command(update, context, "scripts")


async def hashtags(update, context):
    await ai_command(update, context, "hashtags")


async def bio(update, context):
    await ai_command(update, context, "bio")


@flask_app.route("/", methods=["GET"])
def home():
    return "TikGenius is running ✅", 200


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


def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("hooks", hooks))
    app.add_handler(CommandHandler("ideas", ideas))
    app.add_handler(CommandHandler("captions", captions))
    app.add_handler(CommandHandler("scripts", scripts))
    app.add_handler(CommandHandler("hashtags", hashtags))
    app.add_handler(CommandHandler("bio", bio))

    app.run_polling()


threading.Thread(target=run_bot, daemon=True).start()
