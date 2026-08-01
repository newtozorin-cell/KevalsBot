# Sensex Supertrend Bot — Web App

Long-running Upstox bot for the **SENSEX Supertrend Cross Options Strategy**, with
a live dashboard for your friend to monitor 24/7.

This is a direct port of the OpenAlgo `sensex_strategy.py` to the Upstox v2 API,
wrapped in a small Flask web app for login + live tracking.

---

## Strategy (unchanged from OpenAlgo version)

| Param | Value |
|------|------|
| Underlying | SENSEX spot/futures (BSE) |
| Timeframe  | 5-min candles |
| Indicator  | Dual Supertrend — Fast (5 / 1.3) + Slow (20 / 4.0) |
| Signal     | Fast ST crosses Slow ST (BUY up-cross, SELL down-cross) |
| Instrument | Nearest-strike SENSEX option (CE for BUY, PE for SELL) |
| Exchange   | BSE_FO (BFO segment on Upstox) |
| Product    | NRML (`I` on Upstox) |
| Lot size   | 20 |
| SL         | Slow Supertrend value at entry |
| TP1 / TP2  | 1.5 R (breakeven trail) / 2.5 R (full exit) |
| Filters    | Trading calendar (weekday + hour-bucket) + grade (A+ / A only) |
| Hours      | No new entry after 14:30 · force exit 15:15 IST |

---

## Project layout

```
webapp/
├── app.py              # Flask app, OAuth, dashboard routes
├── bot.py              # Upstox port of the strategy (+ paper mode)
├── templates/
│   └── index.html      # Live dashboard UI (Bootstrap 5)
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

The bot starts in **paper mode** by default — no orders are sent to Upstox.

To use real Upstox data + place live orders, set `PAPER_TRADE=false` and
complete the OAuth login (button on the homepage).

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
upgrade to the **Starter** plan ($7/mo) or split the bot into a separate
Render **Background Worker** (future enhancement).

---

## Paper trading for a week

Leave `PAPER_TRADE=true`. The bot:

- Fetches real Upstox 5-min candles (if logged in) **or** uses a flat synthetic
  series (if not).
- Simulates fills using live LTP from the market-quote endpoint.
- Tracks virtual positions, P&L, and exit reasons in `sensex_bot.db`.
- Shows everything on the dashboard with a yellow **PAPER** badge.

When your friend is happy, set `PAPER_TRADE=false` and re-deploy — the same
bot will start placing real orders. No code changes needed.

> **Note for the synthetic-mode paper test:** until OAuth is connected, the bot
> uses a flat price series and will not produce real cross signals. To test
> the full pipeline without real money, log in once with paper mode on — the
> bot will pull real candles and simulate fills based on the live LTP feed.

---

## Dashboard

Auto-refreshes every 5 seconds. Shows:

- Current mode (PAPER / LIVE)
- Closed trades, wins, win rate, total P&L
- Last signal time
- Current open position (with T1-hit status and current SL)
- Last 80 signals (with grade, confidence, color, action)
- Last 80 trades (with entry/exit, P&L, reason, grade)

---

## OAuth flow

```
Browser                  Flask app              Upstox
   |  GET /login             |                    |
   |  302 → /v2/login/...    |                    |
   |------------------------>|                    |
   |       auth dialog       |                    |
   |<----------------------->|<------------------>|
   |  GET /callback?code=... |                    |
   |------------------------>| POST /v2/login/... |
   |                         |------------------->|
   |                         |<-- access_token ---|
   |  302 /                  |                    |
   |<------------------------|                    |
```

Tokens are stored in the SQLite DB (keyed under `upstox_access_token`).
Upstox tokens last ~24 h, so re-login once a day is required until refresh
tokens are wired in (todo).

---

## Files summary

| File | Purpose |
|------|---------|
| `bot.py` | Full strategy + Upstox REST client + SQLite persistence |
| `app.py` | Flask app, OAuth, dashboard JSON endpoints |
| `templates/index.html` | Dark-themed dashboard with live polling |
| `render.yaml` | One-click Render.com Blueprint deploy |
| `requirements.txt` | Python dependencies (Flask, pandas, requests, …) |
| `.env.example` | Environment variable template |
| `Procfile` | Heroku-style fallback for other PaaS hosts |
| `runtime.txt` | Python version pin |

---

## Known caveats

- Upstox access tokens expire after ~24 h. Re-login daily.
- The first deploy creates an empty SQLite DB; Render's ephemeral disk means
  **trade history resets on every redeploy** unless you attach a persistent
  disk or switch to Postgres (Render Key-Value Store or external).
- The `BSE_FO` instruments file is fetched on first live-mode start. If
  Upstox's CDN is slow, the bot will fall back to a synthetic data path and
  log a warning.
- This is the same strategy as your OpenAlgo script — backtest the OpenAlgo
  version first if you want to validate edge before going live here.

---

## License

Personal use. No warranty. Trading involves risk; use paper mode first.
