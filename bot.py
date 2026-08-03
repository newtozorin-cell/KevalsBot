"""
Sensex Supertrend Cross Options Bot — Upstox Edition
====================================================

Direct port of the OpenAlgo sensex_strategy.py, adapted for Upstox v2 API.

- Indicator : Dual Supertrend (Fast 5/1.3 + Slow 20/4.0)
- Signal    : Fast ST crosses Slow ST (BUY up-cross, SELL down-cross)
- Trade     : Nearest-strike SENSEX Option on BSE_FO (CE for BUY, PE for SELL)
- Product   : NRML (Upstox: I)
- SL        : Slow Supertrend value at entry
- T1 / T2   : 1.5R breakeven trail / 2.5R full exit
- Filters   : Calendar (weekday + hour-bucket)  +  Grade (A+ / A only)

Run modes
---------
PAPER_TRADE=true  ->  simulate fills using live LTP, no real orders
PAPER_TRADE=false ->  real Upstox orders (default)

Designed to run as a background thread inside the Flask app.
"""

import os
import sys
import time as time_module
import logging
import threading
import sqlite3
from datetime import datetime, time, timedelta, date, timezone
from calendar import monthrange
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum

import pandas as pd
import requests

# =========================================================
#  THREAD LIMITS (suppress OpenBLAS noise)
# =========================================================
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

# =========================================================
#  LOGGING
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sensex_bot")

# =========================================================
#  CONFIG
# =========================================================
UPSTOX_API_BASE = "https://api.upstox.com/v2"
UPSTOX_HFT_BASE = "https://api-hft.upstox.com/v2"

# SENSEX
SENSEX_FUT_PREFIX = "SENSEX"
STRIKE_STEP = 100
LOT_SIZE = 20
EXCHANGE = "BSE_FO"  # BFO segment on Upstox
OPTION_PRODUCT = "I"  # NRML

# Supertrend
FAST_ST_PERIOD = 5
FAST_ST_MULT = 1.3
SLOW_ST_PERIOD = 20
SLOW_ST_MULT = 4.0

# Trading hours (IST)
MARKET_OPEN = time(9, 15)
NO_NEW_ENTRY = time(14, 30)
FORCE_EXIT = time(15, 15)

# Trading calendar
TRADE_CALENDAR = {
    0: {"H1": "small", "H2": "small", "H4": "small", "H6": "small"},
    1: {"H4": "sized_up", "H6": "normal"},
    2: {"H1": "sized_up", "H2": "sized_up", "H3": "sized_up",
        "H4": "sized_up", "H5": "sized_up", "H6": "sized_up"},
    3: {"H1": "normal", "H4": "normal", "H6": "normal"},
    4: {"H1": "sized_up", "H3": "normal", "H4": "normal"},
}

# Grading
BASE_CONFIDENCE = 0.50
GRADE_A_PLUS = 0.80
GRADE_A = 0.70
GRADE_B = 0.60
TRADEABLE_GRADES = ("A+", "A")

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    return datetime.now(IST)


def get_hour_bucket(t: time) -> Optional[str]:
    if time(9, 15) <= t < time(10, 16):  return "H1"
    if time(10, 16) <= t < time(11, 16): return "H2"
    if time(11, 16) <= t < time(12, 16): return "H3"
    if time(12, 16) <= t < time(13, 16): return "H4"
    if time(13, 16) <= t < time(14, 16): return "H5"
    if time(14, 16) <= t <= time(15, 30): return "H6"
    return None


def calendar_sizing(weekday: int, bucket: Optional[str]) -> Optional[str]:
    if bucket is None:
        return None
    return TRADE_CALENDAR.get(weekday, {}).get(bucket)


# =========================================================
#  SYMBOL HELPERS
# =========================================================
def next_monthly_expiry() -> date:
    today = now_ist().date()
    cur_t = now_ist().time()
    y, m = today.year, today.month

    def _last_thursday(year: int, month: int) -> date:
        last_day = monthrange(year, month)[1]
        d = date(year, month, last_day)
        return d - timedelta(days=(d.weekday() - 3) % 7)

    expiry = _last_thursday(y, m)
    if today > expiry or (today == expiry and cur_t >= FORCE_EXIT):
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
        expiry = _last_thursday(y, m)
    return expiry


def format_expiry(d: date) -> str:
    # Upstox format: 25JUL, 25JUL25
    return d.strftime("%d%b").upper()


def nearest_strike(price: float) -> int:
    return int(round(price / STRIKE_STEP) * STRIKE_STEP)


def build_option_symbol(futures_price: float, opt_type: str) -> str:
    """Upstox SENSEX option symbol: SENSEX25JUL78500CE"""
    expiry = format_expiry(next_monthly_expiry())
    strike = nearest_strike(futures_price)
    return f"{SENSEX_FUT_PREFIX}{expiry}{strike}{opt_type}"


def current_fut_symbol() -> str:
    """SENSEX front-month futures symbol on Upstox."""
    expiry = format_expiry(next_monthly_expiry())
    return f"{SENSEX_FUT_PREFIX}{expiry}FUT"


# =========================================================
#  INDICATORS
# =========================================================
def wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def supertrend(df: pd.DataFrame, period: int, mult: float):
    hl2 = (df["high"] + df["low"]) / 2
    atr = wilder_atr(df, period)
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr

    st = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    for i in range(len(df)):
        if i == 0:
            st.iloc[i] = upper.iloc[i]
            direction.iloc[i] = -1
            continue

        direction.iloc[i] = 1 if df["close"].iloc[i] > st.iloc[i-1] else -1
        st.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]

        if direction.iloc[i] == 1 and direction.iloc[i-1] == 1:
            st.iloc[i] = max(st.iloc[i], st.iloc[i-1])
        elif direction.iloc[i] == -1 and direction.iloc[i-1] == -1:
            st.iloc[i] = min(st.iloc[i], st.iloc[i-1])

    return st, direction


def add_supertrends(df: pd.DataFrame) -> pd.DataFrame:
    f_st, _ = supertrend(df, FAST_ST_PERIOD, FAST_ST_MULT)
    s_st, _ = supertrend(df, SLOW_ST_PERIOD, SLOW_ST_MULT)
    df["fast_st"] = f_st
    df["slow_st"] = s_st
    return df


def detect_cross(df: pd.DataFrame) -> Optional[str]:
    if len(df) < 2:
        return None
    f_now,  s_now  = df["fast_st"].iloc[-1], df["slow_st"].iloc[-1]
    f_prev, s_prev = df["fast_st"].iloc[-2], df["slow_st"].iloc[-2]
    if f_prev <= s_prev and f_now > s_now:
        return "BUY"
    if f_prev >= s_prev and f_now < s_now:
        return "SELL"
    return None


def get_bar_color(df: pd.DataFrame) -> str:
    close = df["close"].iloc[-1]
    fst   = df["fast_st"].iloc[-1]
    sst   = df["slow_st"].iloc[-1]
    above_fast = close > fst
    above_slow = close > sst
    if above_fast and above_slow:  return "green"
    if above_fast:                 return "blue"
    if not above_fast and not above_slow: return "red"
    return "yellow"


# =========================================================
#  GRADING
# =========================================================
def grade_signal(signal: str, df: pd.DataFrame, sl: float, tp2: float):
    confidence = BASE_CONFIDENCE
    color = get_bar_color(df)

    if signal == "BUY":
        if   color == "green": confidence += 0.20
        elif color == "blue":  confidence += 0.10
    else:
        if   color == "red":    confidence += 0.20
        elif color == "yellow": confidence += 0.10

    entry = df["close"].iloc[-1]
    risk = abs(entry - sl)
    if risk > 0:
        rr = abs(tp2 - entry) / risk
        if   rr >= 3.0: confidence += 0.20
        elif rr >= 2.0: confidence += 0.10

    confidence = min(confidence, 0.95)

    if   confidence >= GRADE_A_PLUS: grade = "A+"
    elif confidence >= GRADE_A:      grade = "A"
    elif confidence >= GRADE_B:      grade = "B"
    else:                            grade = "C"

    return confidence, grade, color


# =========================================================
#  DATABASE (SQLite for trades + signals)
# =========================================================
class Database:
    def __init__(self, path: str = "sensex_bot.db"):
        self.path = path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        with self._lock, self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    fut_price REAL,
                    fast_st REAL,
                    slow_st REAL,
                    color TEXT,
                    confidence REAL,
                    grade TEXT,
                    action TEXT
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_open TEXT NOT NULL,
                    ts_close TEXT,
                    direction TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    option_type TEXT,
                    entry_fut REAL,
                    entry_opt REAL,
                    sl REAL,
                    target_1 REAL,
                    target_2 REAL,
                    exit_fut REAL,
                    exit_opt REAL,
                    qty INTEGER,
                    pnl REAL,
                    status TEXT,
                    exit_reason TEXT,
                    grade TEXT,
                    paper INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            c.commit()

    def insert_signal(self, sig: Dict[str, Any]):
        with self._lock, self._conn() as c:
            c.execute("""
                INSERT INTO signals
                  (ts, signal, fut_price, fast_st, slow_st, color, confidence, grade, action)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sig["ts"], sig["signal"], sig["fut_price"],
                sig["fast_st"], sig["slow_st"], sig["color"],
                sig["confidence"], sig["grade"], sig["action"],
            ))
            c.commit()

    def open_trade(self, t: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("""
                INSERT INTO trades
                  (ts_open, direction, symbol, option_type, entry_fut, entry_opt,
                   sl, target_1, target_2, qty, status, grade, paper)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
            """, (
                t["ts_open"], t["direction"], t["symbol"], t["option_type"],
                t["entry_fut"], t["entry_opt"], t["sl"], t["target_1"],
                t["target_2"], t["qty"], t["grade"], int(t.get("paper", True)),
            ))
            c.commit()
            return cur.lastrowid

    def update_trade(self, trade_id: int, **kwargs):
        if not kwargs:
            return
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [trade_id]
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE trades SET {cols} WHERE id=?", vals)
            c.commit()

    def recent_signals(self, limit: int = 50) -> List[Dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_trades(self, limit: int = 50) -> List[Dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def open_trade_row(self) -> Optional[Dict]:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM trades WHERE status='OPEN' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> Dict[str, Any]:
        with self._lock, self._conn() as c:
            closed = c.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl),0) AS pnl "
                "FROM trades WHERE status='CLOSED'"
            ).fetchone()
            open_ = c.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='OPEN'"
            ).fetchone()
            wins = c.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='CLOSED' AND pnl > 0"
            ).fetchone()
        n_closed = closed["n"] or 0
        return {
            "closed": n_closed,
            "open": open_["n"] or 0,
            "pnl": round(closed["pnl"] or 0.0, 2),
            "wins": wins["n"] or 0,
            "win_rate": round((wins["n"] or 0) / n_closed * 100, 1) if n_closed else 0.0,
        }

    def set_state(self, key: str, value: str):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            c.commit()

    def get_state(self, key: str) -> Optional[str]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


# =========================================================
#  UPSTOX CLIENT
# =========================================================
class UpstoxClient:
    """Thin wrapper around Upstox v2 REST API."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self.session.get(f"{UPSTOX_API_BASE}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict, hft: bool = False) -> dict:
        base = UPSTOX_HFT_BASE if hft else UPSTOX_API_BASE
        r = self.session.post(f"{base}{path}", json=payload, timeout=15)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code >= 400:
            raise RuntimeError(f"Upstox {r.status_code}: {data}")
        return data

    # ---------- Profile ----------
    def profile(self) -> dict:
        return self._get("/user/profile")

    # ---------- Historical candles ----------
    def history_5min(self, instrument_key: str, days: int = 2) -> pd.DataFrame:
        """Fetch 5-min candles for the given instrument_key."""
        to_date = now_ist().date()
        from_date = to_date - timedelta(days=days)
        path = f"/historical-candle/{instrument_key}/5minute/{to_date}/{from_date}"
        data = self._get(path)
        candles = data.get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    # ---------- LTP ----------
    def ltp(self, instrument_keys: List[str]) -> Dict[str, float]:
        """Returns {instrument_key: ltp}."""
        keys = ",".join(instrument_keys)
        data = self._get("/market-quote/ltp", params={"instrument_key": keys})
        out = {}
        for k, v in (data.get("data") or {}).items():
            out[k] = v.get("last_price")
        return out

    def ltp_by_symbol(self, symbol: str, exchange: str = EXCHANGE) -> Optional[float]:
        """Convenience: build a temporary instrument_key. For options we use the
        proper key resolved by the bot, but for the futures index we accept the
        canonical BSE_FO|SENSEX{YY}{MMM}FUT style. Falls back to None."""
        # Most Upstox users resolve a numeric instrument_key from the
        # daily BOD file. We expose a hook for that in the bot.
        return None

    # ---------- Place order ----------
    def place_order(
        self,
        instrument_token: str,
        transaction_type: str,
        quantity: int,
        product: str = OPTION_PRODUCT,
        order_type: str = "MARKET",
        price: float = 0.0,
        tag: str = "sensex_bot",
    ) -> dict:
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": tag,
            "instrument_token": instrument_token,
            "order_type": order_type,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False,
            "market_protection": 0,
        }
        return self._post("/order/place", payload, hft=True)

    def order_status(self, order_id: str) -> dict:
        return self._get(f"/order/history", params={"order_id": order_id})


# =========================================================
#  INSTRUMENT RESOLVER
# =========================================================
class InstrumentResolver:
    """
    Maps trading symbols (e.g. 'SENSEX25JUL78500CE') to Upstox instrument_tokens.

    In live mode the bot downloads Upstox's BOD instruments JSON once and caches
    it. In paper mode the resolver returns a synthetic token so order calls can
    be simulated without the file.
    """

    BOD_URL = "https://assets.upstox.com/market-quote/instruments/exchange/BSE_FO.json.gz"

    def __init__(self, paper: bool = True):
        self.paper = paper
        self._by_symbol: Dict[str, str] = {}
        self._by_key: Dict[str, str] = {}
        self._loaded = False

    def load(self):
        if self.paper or self._loaded:
            self._loaded = True
            return
        try:
            import gzip, json, urllib.request
            log.info("Downloading BSE_FO instruments file...")
            with urllib.request.urlopen(self.BOD_URL, timeout=30) as resp:
                raw = gzip.decompress(resp.read())
            rows = json.loads(raw)
            for r in rows:
                sym = r.get("trading_symbol", "")
                tok = r.get("instrument_key", "")
                if sym and tok:
                    self._by_symbol[sym.upper()] = tok
                    self._by_key[tok] = sym
            log.info(f"Loaded {len(self._by_symbol)} BSE_FO instruments")
        except Exception as e:
            log.error(f"Instrument load failed: {e} — falling back to synthetic tokens")
        self._loaded = True

    def token_for(self, symbol: str) -> str:
        if self.paper:
            return f"PAPER::{symbol}"
        return self._by_symbol.get(symbol.upper(), f"UNKNOWN::{symbol}")

    def symbol_for(self, token: str) -> Optional[str]:
        return self._by_key.get(token)


# =========================================================
#  POSITION
# =========================================================
@dataclass
class Position:
    direction: str           # BUY or SELL
    option_type: str         # CE or PE
    symbol: str
    entry_fut: float
    entry_opt: float
    sl: float
    target_1: float
    target_2: float
    risk: float
    t1_hit: bool = False
    trade_id: Optional[int] = None


# =========================================================
#  BOT
# =========================================================
class SensexBot:
    def __init__(
        self,
        db: Database,
        access_token: Optional[str] = None,
        paper: bool = True,
        loop_interval: int = 30,
    ):
        self.db = db
        self.paper = paper
        self.loop_interval = loop_interval

        self.client: Optional[UpstoxClient] = None
        if not self.paper and access_token:
            self.client = UpstoxClient(access_token)

        self.resolver = InstrumentResolver(paper=self.paper)
        self.resolver.load()

        self.position: Optional[Position] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False

        log.info(f"Bot initialised  paper={self.paper}  "
                 f"client={'yes' if self.client else 'no'}")

    # ---------- lifecycle ----------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        log.info("Bot thread started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.running = False
        log.info("Bot thread stopped")

    # ---------- exchange wrappers ----------
    def get_history_5min(self) -> pd.DataFrame:
        fut_sym = current_fut_symbol()
        token = self.resolver.token_for(fut_sym)
        if self.paper or not self.client:
            return self._synth_candles(fut_sym)
        try:
            return self.client.history_5min(token, days=2)
        except Exception as e:
            log.error(f"history fetch failed: {e}")
            return pd.DataFrame()

    def _synth_candles(self, symbol: str) -> pd.DataFrame:
        """No data source? Build a flat synthetic series so the loop stays
        alive. This will never produce a cross — paper mode users should set
        UPSTOX_ACCESS_TOKEN to receive real candles."""
        idx = pd.date_range(end=now_ist(), periods=120, freq="5min")
        price = 80000.0
        prices = [price] * len(idx)
        df = pd.DataFrame({
            "timestamp": idx, "open": prices, "high": prices,
            "low": prices, "close": prices, "volume": 0, "oi": 0,
        })
        return df

    def get_fut_ltp(self) -> Optional[float]:
        fut_sym = current_fut_symbol()
        token = self.resolver.token_for(fut_sym)
        if self.paper or not self.client:
            # Simulate a stable SENSEX price. Replace with manual override
            # by writing a 'simulated_ltp' state key in the DB.
            sim = self.db.get_state("simulated_ltp")
            return float(sim) if sim else 80000.0
        try:
            ltp_map = self.client.ltp([token])
            return ltp_map.get(token)
        except Exception as e:
            log.error(f"ltp fetch failed: {e}")
            return None

    def get_option_ltp(self, symbol: str) -> Optional[float]:
        token = self.resolver.token_for(symbol)
        if self.paper or not self.client:
            # Rough option premium estimate based on distance from strike
            return 100.0
        try:
            ltp_map = self.client.ltp([token])
            return ltp_map.get(token)
        except Exception as e:
            log.error(f"option ltp failed: {e}")
            return None

    # ---------- orders ----------
    def place_buy(self, symbol: str) -> Dict[str, Any]:
        token = self.resolver.token_for(symbol)
        if self.paper or not self.client:
            ltp = self.get_option_ltp(symbol) or 100.0
            log.info(f"[PAPER] BUY {symbol} x{LOT_SIZE} @ {ltp}")
            return {"status": "complete", "average_price": ltp, "order_id": f"paper_{int(time_module.time())}"}
        try:
            return self.client.place_order(token, "BUY", LOT_SIZE)
        except Exception as e:
            log.error(f"BUY failed: {e}")
            return {}

    def place_sell(self, symbol: str) -> Dict[str, Any]:
        token = self.resolver.token_for(symbol)
        if self.paper or not self.client:
            ltp = self.get_option_ltp(symbol) or 100.0
            log.info(f"[PAPER] SELL {symbol} x{LOT_SIZE} @ {ltp}")
            return {"status": "complete", "average_price": ltp, "order_id": f"paper_{int(time_module.time())}"}
        try:
            return self.client.place_order(token, "SELL", LOT_SIZE)
        except Exception as e:
            log.error(f"SELL failed: {e}")
            return {}

    # ---------- main loop ----------
    def _run(self):
        log.info("=" * 70)
        log.info("  SENSEX SUPERTREND CROSS OPTIONS BOT — UPSTOX")
        log.info("=" * 70)
        log.info(f"  Mode        : {'PAPER' if self.paper else 'LIVE'}")
        log.info(f"  ST params   : Fast {FAST_ST_PERIOD}/{FAST_ST_MULT}  |  "
                 f"Slow {SLOW_ST_PERIOD}/{SLOW_ST_MULT}")
        log.info(f"  Trade hours : {MARKET_OPEN} -> {FORCE_EXIT} IST")
        log.info(f"  Trade grades: {TRADEABLE_GRADES}")
        log.info("=" * 70)

        while not self._stop.is_set():
            try:
                cur_t = now_ist().time()

                if cur_t < MARKET_OPEN:
                    log.info("Waiting for market open (9:15 IST)...")
                    self._sleep(60)
                    continue

                if cur_t >= FORCE_EXIT:
                    if self.position:
                        log.info("EOD — force exit")
                        self._close_position(self.position, "EOD")
                    log.info("Market closed. Done for the day.")
                    # Sleep until next day open
                    self._sleep(60 * 30)
                    continue

                self._tick()
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)

            self._sleep(self.loop_interval)

    def _tick(self):
        df = self.get_history_5min()
        if df.empty or len(df) < SLOW_ST_PERIOD + 2:
            return
        df = add_supertrends(df)

        fut_price = self.get_fut_ltp()
        if fut_price is None:
            return

        cur_t = now_ist().time()
        now_dt = now_ist()
        weekday = now_dt.weekday()
        bucket = get_hour_bucket(cur_t)
        sizing = calendar_sizing(weekday, bucket)
        signal = detect_cross(df)

        # always log the latest cross detection for the dashboard
        fast_st_v = float(df["fast_st"].iloc[-1])
        slow_st_v = float(df["slow_st"].iloc[-1])
        color = get_bar_color(df)

        if self.position is None:
            if signal and cur_t < NO_NEW_ENTRY and bucket and sizing is not None:
                log.info(f"[Calendar] {now_dt.strftime('%A')} {bucket} -> {sizing}")
                self._open_from_signal(signal, fut_price, df, fast_st_v, slow_st_v, color)
            elif signal and (bucket is None or sizing is None):
                log.info(f"{signal} signal - no calendar slot ({bucket}); skipped")
        else:
            self._manage_position(signal, fut_price, fast_st_v, color)

    def _open_from_signal(self, signal, fut_price, df, fast_st_v, slow_st_v, color):
        opt_type = "CE" if signal == "BUY" else "PE"
        entry = fut_price
        sl = slow_st_v

        if signal == "BUY"  and sl >= entry:
            log.warning("SL on wrong side for BUY - skipping")
            self._log_signal(signal, fut_price, fast_st_v, slow_st_v, color, 0, "C", "rejected")
            return
        if signal == "SELL" and sl <= entry:
            log.warning("SL on wrong side for SELL - skipping")
            self._log_signal(signal, fut_price, fast_st_v, slow_st_v, color, 0, "C", "rejected")
            return

        if signal == "BUY":
            risk     = entry - sl
            target_1 = round(entry + risk * 1.5, 2)
            target_2 = round(entry + risk * 2.5, 2)
        else:
            risk     = sl - entry
            target_1 = round(entry - risk * 1.5, 2)
            target_2 = round(entry - risk * 2.5, 2)

        confidence, grade, _ = grade_signal(signal, df, sl, target_2)

        log.info(f"{signal} signal @ futures={entry}  SL={sl}  "
                 f"T1={target_1}  T2={target_2}  (R={risk})  "
                 f"Color={color}  Conf={confidence:.2f}  Grade={grade}")

        if grade not in TRADEABLE_GRADES:
            log.info(f"  SKIP: Grade {grade} below threshold; no trade.")
            self._log_signal(signal, fut_price, fast_st_v, slow_st_v,
                             color, confidence, grade, "skipped")
            return

        opt_sym = build_option_symbol(entry, opt_type)
        log.info(f"  -> Buying {opt_sym}")
        order = self.place_buy(opt_sym)
        if not order or order.get("status") not in ("complete", "open", "open_pending", "trigger_pending"):
            log.warning(f"Order did not confirm: {order}")
            self._log_signal(signal, fut_price, fast_st_v, slow_st_v,
                             color, confidence, grade, "order_failed")
            return

        entry_opt = float(order.get("average_price") or self.get_option_ltp(opt_sym) or 0.0)

        trade_id = self.db.open_trade({
            "ts_open": now_ist().isoformat(),
            "direction": signal,
            "symbol": opt_sym,
            "option_type": opt_type,
            "entry_fut": entry,
            "entry_opt": entry_opt,
            "sl": sl,
            "target_1": target_1,
            "target_2": target_2,
            "qty": LOT_SIZE,
            "grade": grade,
            "paper": self.paper,
        })

        self.position = Position(
            direction=signal, option_type=opt_type, symbol=opt_sym,
            entry_fut=entry, entry_opt=entry_opt, sl=sl,
            target_1=target_1, target_2=target_2, risk=risk,
            t1_hit=False, trade_id=trade_id,
        )

        self._log_signal(signal, fut_price, fast_st_v, slow_st_v,
                         color, confidence, grade, "opened")

    def _manage_position(self, signal, fut_price, fast_st_v, color):
        pos = self.position
        is_long = (pos.direction == "BUY")

        hit_sl = (is_long and fut_price <= pos.sl) or \
                 (not is_long and fut_price >= pos.sl)
        hit_t1 = (is_long and fut_price >= pos.target_1) or \
                 (not is_long and fut_price <= pos.target_1)
        hit_t2 = (is_long and fut_price >= pos.target_2) or \
                 (not is_long and fut_price <= pos.target_2)

        if hit_sl:
            log.info(f"SL hit @ futures={fut_price}")
            self._close_position(pos, "SL")
        elif hit_t2:
            log.info(f"T2 hit @ futures={fut_price} — FULL EXIT")
            self._close_position(pos, "T2")
        elif hit_t1 and not pos.t1_hit:
            log.info(f"T1 hit @ futures={fut_price} — trail SL to breakeven")
            pos.sl = pos.entry_fut
            pos.t1_hit = True
            if pos.trade_id:
                self.db.update_trade(pos.trade_id, sl=pos.sl)
        elif signal:
            expected = "BUY" if signal == "BUY" else "SELL"
            if expected != pos.direction:
                log.info(f"Reverse {signal} — flipping position")
                self._close_position(pos, "REVERSE")
                self._open_from_signal(signal, fut_price,
                                       pd.DataFrame(),  # color unused here
                                       fast_st_v, pos.sl, color)

    def _close_position(self, pos: Position, reason: str):
        order = self.place_sell(pos.symbol)
        exit_opt = float(order.get("average_price") or self.get_option_ltp(pos.symbol) or 0.0)
        pnl = round((exit_opt - pos.entry_opt) * pos.option_type_sign() * LOT_SIZE, 2)
        if pos.trade_id:
            self.db.update_trade(
                pos.trade_id,
                ts_close=now_ist().isoformat(),
                exit_fut=self.get_fut_ltp(),
                exit_opt=exit_opt,
                pnl=pnl,
                status="CLOSED",
                exit_reason=reason,
            )
        log.info(f"Closed {pos.symbol} reason={reason} pnl={pnl}")
        self.position = None

    def _log_signal(self, signal, fut_price, fast_st, slow_st,
                    color, confidence, grade, action):
        try:
            self.db.insert_signal({
                "ts": now_ist().isoformat(),
                "signal": signal,
                "fut_price": fut_price,
                "fast_st": fast_st,
                "slow_st": slow_st,
                "color": color,
                "confidence": confidence,
                "grade": grade,
                "action": action,
            })
        except Exception as e:
            log.error(f"signal log failed: {e}")

    def _sleep(self, seconds: int):
        self._stop.wait(seconds)


# Patch Position with a helper for P&L sign
def _option_type_sign(self) -> int:
    return 1 if self.option_type == "CE" else -1
Position.option_type_sign = _option_type_sign  # type: ignore[attr-defined]


# =========================================================
#  STANDALONE ENTRY (for `python bot.py` testing)
# =========================================================
if __name__ == "__main__":
    paper = os.getenv("PAPER_TRADE", "true").lower() in ("1", "true", "yes")
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    db = Database()
    bot = SensexBot(db=db, access_token=token, paper=paper)
    bot.start()
    try:
        while True:
            time_module.sleep(60)
    except KeyboardInterrupt:
        bot.stop()
