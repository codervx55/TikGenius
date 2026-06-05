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

PRICE_KOBO = 200000
FREE_LIMIT = 5
DOWNLOADER_FREE_LIMIT = 3

app = Flask(__name__)
app.secret_key = SECRET_KEY
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
app.permanent_session_lifetime = timedelta(days=SESSION_DAYS)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_DAYS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ... [All your existing helper functions, DB setup, AI functions, payment, referral, etc. remain 100% the same] ...

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


# Keep all your other @app.route functions exactly as they are
# (signup, login, chat, generate, me, referral/stats, etc.)

# ========================= HTML PAGES =========================

# FULL STUDIO_HTML with the important fix
STUDIO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>TikGenius — AI Content Studio</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* Your original full CSS - unchanged */
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--sidebar:#111118;--card:#16161e;--border:#1e1e2e;
  --text:#f4f4f8;--muted:#6b6b80;--accent:#00ffcc;--accent2:#00aaff;
  --gold:#ffb800;--danger:#ff4d6d;--green:#00c896;
  --radius:14px;--font:'Inter',system-ui,sans-serif;--font-h:'Space Grotesk',system-ui,sans-serif;
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--font);-webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
/* ... (all your original styles) ... */
</style>
</head>
<body>
<!-- Your full original HTML structure remains here (nav, sidebar, chat, input bar, profile panel, auth modal) -->

<script>
// === CRITICAL FIX ===
var user = null;
var idea = '';
var questions = [];
var stage = 'idle';
var urlRef = new URLSearchParams(location.search).get('ref') || '';

async function init() {
  try {
    var r = await fetch('/api/me');
    if (r.ok) { 
      user = await r.json(); 
      applyUser(); 
      loadHistory(); 
    } else { 
      showGuest(); 
    }
  } catch(e) { showGuest(); }
}

function handleSend() {
  if (!user) { 
    openModal('signup'); 
    return; 
  }
  var text = (document.getElementById('chatInput').value || '').trim();
  if (!text) return;
  if (stage === 'idle') startIdea(text);
  else if (stage === 'done') { resetChat(); }
}

// Rest of your original script (addMsg, startIdea, showQuestions, etc.) remains the same
// ... [paste the rest of your original <script> content here] ...

init();
</script>
</body>
</html>"""

# Keep REFER_HTML, DASHBOARD_HTML, DOWNLOAD_HTML as they were
REFER_HTML = """...[your full REFER_HTML code]..."""
# (same for others)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
