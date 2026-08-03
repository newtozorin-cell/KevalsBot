"""
Sensex Bot — Flask web app
==========================

Routes
------
GET  /                       Login page or live dashboard
GET  /login                  Redirect to Upstox OAuth dialog
GET  /callback               OAuth callback — exchanges code for token
GET  /logout                 Clear token + stop bot
GET  /api/status             Bot status + PnL summary
GET  /api/signals            Recent signals (last 50)
GET  /api/trades             Recent trades (last 50)
GET  /api/position           Current open position, if any
POST /api/paper/toggle       Toggle paper mode (owner only)
GET  /healthz                Liveness probe for Render

The bot runs as a background thread started after the user logs in.
"""

import os
import secrets
import sqlite3
import threading
import logging
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, abort,
)
from dotenv import load_dotenv

from bot import Database, SensexBot

load_dotenv()

# =========================================================
#  LOGGING
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
)
log = logging.getLogger("sensex_app")

# =========================================================
#  CONFIG
# =========================================================
APP_SECRET      = os.getenv("FLASK_SECRET", secrets.token_hex(16))
UPSTOX_CLIENT_ID     = os.getenv("UPSTOX_CLIENT_ID", "")
UPSTOX_CLIENT_SECRET = os.getenv("UPSTOX_CLIENT_SECRET", "")
UPSTOX_REDIRECT_URI  = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:5000/callback")
PAPER_TRADE     = os.getenv("PAPER_TRADE", "true").lower() in ("1", "true", "yes")
DB_PATH         = os.getenv("DB_PATH", "sensex_bot.db")

UPSTOX_AUTH_URL  = "https://api.upstox.com/v2/login/authorization/dialog"
UPSTOX_TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"

# =========================================================
#  APP
# =========================================================
app = Flask(__name__)
app.secret_key = APP_SECRET
app.permanent_session_lifetime = timedelta(days=7)

db = Database(DB_PATH)
bot: SensexBot | None = None
bot_lock = threading.Lock()

# Token storage in DB (so it survives restarts)
def save_token(token_data: dict):
    db.set_state("upstox_access_token", token_data.get("access_token", ""))
    db.set_state("upstox_token_expiry", token_data.get("expires_at", ""))
    db.set_state("upstox_user_id", token_data.get("user_id", ""))


def load_token() -> str | None:
    return db.get_state("upstox_access_token")


def clear_token():
    for k in ("upstox_access_token", "upstox_token_expiry", "upstox_user_id"):
        db.set_state(k, "")


def ensure_bot(paper: bool | None = None) -> SensexBot:
    """Create the singleton bot if needed and start it."""
    global bot
    with bot_lock:
        if bot is None:
            token = None if PAPER_TRADE else load_token()
            paper_mode = PAPER_TRADE if paper is None else paper
            bot = SensexBot(db=db, access_token=token, paper=paper_mode)
            bot.start()
        elif paper is not None and paper != bot.paper:
            # Mode change requires a restart
            bot.stop()
            token = None if paper else load_token()
            bot = SensexBot(db=db, access_token=token, paper=paper)
            bot.start()
        return bot


# =========================================================
#  ROUTES — pages
# =========================================================
@app.route("/")
def index():
    token = load_token()
    logged_in = bool(token) or PAPER_TRADE
    return render_template(
        "index.html",
        logged_in=logged_in,
        paper=PAPER_TRADE,
        redirect_uri=UPSTOX_REDIRECT_URI,
    )


@app.route("/login")
def login():
    if not UPSTOX_CLIENT_ID:
        return "UPSTOX_CLIENT_ID not set in environment.", 500

    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    params = {
        "client_id": UPSTOX_CLIENT_ID,
        "redirect_uri": UPSTOX_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    }
    return redirect(f"{UPSTOX_AUTH_URL}?{urlencode(params)}")


@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != session.get("oauth_state"):
        return "OAuth state mismatch — please retry login.", 400

    try:
        r = requests.post(UPSTOX_TOKEN_URL, data={
            "code": code,
            "client_id": UPSTOX_CLIENT_ID,
            "client_secret": UPSTOX_CLIENT_SECRET,
            "redirect_uri": UPSTOX_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=15)
        r.raise_for_status()
        token_data = r.json()
    except Exception as e:
        log.error(f"token exchange failed: {e}")
        return f"Token exchange failed: {e}", 500

    access_token = token_data.get("access_token")
    if not access_token:
        return f"Upstox did not return an access token: {token_data}", 500

    # Upstox returns no expires_in; tokens last 1 day by default.
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)
    save_token({
        "access_token": access_token,
        "expires_at": expiry.isoformat(),
        "user_id": token_data.get("user_id", ""),
    })

    session.pop("oauth_state", None)
    log.info("Upstox login successful — starting bot")
    ensure_bot(paper=False)
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    clear_token()
    global bot
    with bot_lock:
        if bot:
            bot.stop()
            bot = None
    session.clear()
    return redirect(url_for("index"))


# =========================================================
#  ROUTES — API (JSON, used by the dashboard JS)
# =========================================================
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})


@app.route("/api/status")
def api_status():
    s = db.stats()
    s["paper"] = PAPER_TRADE
    s["logged_in"] = bool(load_token()) or PAPER_TRADE
    s["bot_running"] = bool(bot and bot.running)
    s["last_signal_ts"] = None
    last = db.recent_signals(1)
    if last:
        s["last_signal_ts"] = last[0]["ts"]
    return jsonify(s)


@app.route("/api/signals")
def api_signals():
    return jsonify(db.recent_signals(80))


@app.route("/api/trades")
def api_trades():
    return jsonify(db.recent_trades(80))


@app.route("/api/position")
def api_position():
    row = db.open_trade_row()
    if not row:
        return jsonify({"open": False})
    pos = dict(row)
    pos["open"] = True
    pos["t1_hit"] = False
    if bot and bot.position:
        pos["t1_hit"] = bot.position.t1_hit
        pos["current_sl"] = bot.position.sl
    return jsonify(pos)


@app.route("/api/paper/toggle", methods=["POST"])
def api_paper_toggle():
    global PAPER_TRADE
    PAPER_TRADE = not PAPER_TRADE
    log.info(f"Paper mode toggled to {PAPER_TRADE}")
    ensure_bot(paper=PAPER_TRADE)
    return jsonify({"paper": PAPER_TRADE})


# =========================================================
#  ERROR HANDLERS
# =========================================================
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404


# =========================================================
#  STARTUP — start the bot when the app module loads
# =========================================================
# Procfile uses gunicorn with 1 worker, so this runs exactly once
# when the worker boots. PAPER_TRADE=true starts the bot in
# simulation mode without needing a real Upstox token.
if PAPER_TRADE or load_token():
    try:
        ensure_bot()
    except Exception as e:
        log.error(f"Failed to start bot at module load: {e}")


# =========================================================
#  MAIN
# =========================================================
if __name__ == "__main__":
    # If running locally and a token is already saved, start the bot immediately
    if PAPER_TRADE or load_token():
        ensure_bot()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
