# Sensex Bot — Web App

A long-running Upstox options bot with a live web dashboard. Trade signals,
open positions, and P&L are visible in the browser; the bot itself runs in the
background 24/7 once deployed.

---

## Project layout

```
webapp/
├── app.py              # Flask app, OAuth, dashboard routes
├── bot.py              # Trading bot + Upstox REST client + SQLite persistence
├── templates/
│   └── index.html      # Live dashboard UI
├── requirements.txt
├── render.yaml         # Render.com deploy config
├── Procfile            # Backup for Heroku-style hosts
├── runtime.txt         # Python version
└── .env.example        # Copy to .env and fill in
```

---

## Local development

```bash
cd webapp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Upstox app keys + redirect URI

python app.py
# open http://localhost:5000
```

By default the bot runs in **paper mode** — no real orders are sent to Upstox.
The dashboard is fully functional either way.

---

## Deploying to Render.com

1. Push this folder to a fresh GitHub repo.
2. On Render → **New** → **Blueprint** → point at the repo.
3. Render reads `render.yaml` and creates the web service.
4. After it deploys, set the env vars in the Render dashboard:
   - `UPSTOX_CLIENT_ID`
   - `UPSTOX_CLIENT_SECRET`
   - `UPSTOX_REDIRECT_URI` (use the deployed URL: `https://<your-service>.onrender.com/callback`)
5. Go to https://upstox.com/developer/apps and **add the same redirect URI**
   to your app's allowed list.
6. Open the app, click **Login with Upstox**, and you're live.

The free plan will spin down after 15 min of no HTTP traffic. For 24/7 trading,
upgrade to the **Starter** plan ($7/mo).

---

## Paper trading

Leave `PAPER_TRADE=true`. The bot:

- Fetches real Upstox 5-min candles when logged in.
- Simulates fills using live LTP from the market-quote endpoint.
- Tracks virtual positions, P&L, and exit reasons in `sensex_bot.db`.
- Shows everything on the dashboard with a yellow **PAPER** badge.

When ready for live trading, set `PAPER_TRADE=false` in the Render env vars
and re-deploy. The same bot will start placing real orders — no code changes.

---

## Dashboard

Auto-refreshes every 5 seconds. Shows:

- Current mode (PAPER / LIVE)
- Closed trades, wins, win rate, total P&L
- Last signal
- Current open position
- Recent signals table
- Recent trades table

---

## OAuth flow

```
Browser         Flask app          Upstox
   |  GET /login    |                 |
   |  302 → Upstox  |                 |
   |--------------->|                 |
   |   auth dialog  |                 |
   |<-------------->|<--------------->|
   |  GET /callback |                 |
   |--------------->| POST token URL  |
   |                |---------------->|
   |                |<-- access_token |
   |  302 /         |                 |
   |<---------------|                 |
```

Tokens are stored in the SQLite DB. Upstox tokens last ~24 h, so re-login
once a day is required.

---

## Known caveats

- Upstox access tokens expire after ~24 h. Re-login daily.
- Trade history lives on Render's ephemeral disk and resets on every redeploy.
  For long-term history, switch to an external Postgres database.
- Personal use, no warranty. Trading involves risk.

---

## License

Personal use. No warranty. Trading involves risk.
