import os
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytz
import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange

try:
    from pywebpush import webpush
except ImportError:
    webpush = None

# ============================================================
# CONFIGURATION - V4 BALANCED QUALITY
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003903509447")

IST = pytz.timezone("Asia/Kolkata")

# All pairs used by the session selector. The scanner only scans
# two pairs at a time to keep Twelve Data usage controlled.
PAIRS = {
    "EUR/USD": "5min",
    "GBP/USD": "5min",
    "USD/JPY": "5min",
    "AUD/JPY": "5min",
    "AUD/USD": "5min",
    "NZD/USD": "5min",
    "USD/CAD": "5min",
}

SESSION_PAIRS = {
    "Sydney": ["AUD/USD", "NZD/USD"],
    "Tokyo": ["USD/JPY", "AUD/JPY"],
    "London": ["EUR/USD", "GBP/USD"],
    "London / New York": ["EUR/USD", "GBP/USD"],
    "New York": ["EUR/USD", "USD/CAD"],
}

CONFIDENCE_THRESHOLD = 70
EXPIRY_MINUTES = 2
SCAN_INTERVAL = 300  # 5 minutes
USE_TRADING_HOURS = True
DUPLICATE_COOLDOWN_MINUTES = 20
DB_FILE = "signals.db"

# Web Push configuration. These can be added later in Render.
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# Latest scan information is kept in memory for the dashboard.
market_lock = threading.Lock()
latest_market = {
    pair: {
        "price": None,
        "rsi": None,
        "score": None,
        "signal": None,
        "updated_at": None,
        "active": False,
    }
    for pair in PAIRS
}

# Last sent signal time per pair/direction.
last_signal_meta = {}
last_signal_lock = threading.Lock()

# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    with db_lock:
        conn = get_db()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                pair TEXT NOT NULL,
                signal TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                rsi REAL,
                session TEXT NOT NULL,
                reasons TEXT,
                result TEXT DEFAULT 'PENDING',
                expiry_minutes INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT UNIQUE NOT NULL,
                subscription_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.commit()
        conn.close()


# ============================================================
# TIME / SESSIONS
# ============================================================


def current_time():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")


def _utc_hour_minute():
    now = datetime.now(timezone.utc)
    return now.hour * 60 + now.minute


def _in_window(minutes, start, end):
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def get_active_session_and_pairs():
    """
    Approximate major forex session windows in UTC.
    During overlaps, choose the most useful 2-pair set so the
    bot does not scan every pair simultaneously.
    """
    minutes = _utc_hour_minute()

    sydney = _in_window(minutes, 22 * 60, 7 * 60)
    tokyo = _in_window(minutes, 0, 9 * 60)
    london = _in_window(minutes, 8 * 60, 17 * 60)
    new_york = _in_window(minutes, 13 * 60, 22 * 60)

    if london and new_york:
        return "London / New York", SESSION_PAIRS["London / New York"]
    if london:
        return "London", SESSION_PAIRS["London"]
    if new_york:
        return "New York", SESSION_PAIRS["New York"]
    if tokyo:
        return "Tokyo", SESSION_PAIRS["Tokyo"]
    if sydney:
        return "Sydney", SESSION_PAIRS["Sydney"]

    return "Outside Session", []


def get_session():
    return get_active_session_and_pairs()[0]


def trading_hours():
    return get_session() != "Outside Session"

# ============================================================
# TWELVE DATA
# ============================================================


def get_market_data(symbol, interval):
    if not TWELVE_DATA_API_KEY:
        print("ERROR: TWELVE_DATA_API_KEY is missing.")
        return None

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY,
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if "values" not in data:
            print(f"Twelve Data error for {symbol}: {data}")
            return None

        df = pd.DataFrame(data["values"])
        for column in ["open", "high", "low", "close"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df.iloc[::-1].reset_index(drop=True)
        return df

    except requests.RequestException as exc:
        print(f"Network error getting {symbol}: {exc}")
    except Exception as exc:
        print(f"Unexpected market-data error for {symbol}: {exc}")

    return None


def get_current_price(symbol):
    if not TWELVE_DATA_API_KEY:
        return None

    url = "https://api.twelvedata.com/quote"
    params = {"symbol": symbol, "apikey": TWELVE_DATA_API_KEY}

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        price = data.get("close") or data.get("price")
        if price is None:
            return None

        return float(price)

    except Exception as exc:
        print(f"Quote error for {symbol}: {exc}")
        return None

# ============================================================
# INDICATORS
# ============================================================


def add_indicators(df):
    df["ema20"] = EMAIndicator(close=df["close"], window=20).ema_indicator()
    df["ema50"] = EMAIndicator(close=df["close"], window=50).ema_indicator()
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()

    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14,
    )
    df["atr"] = atr.average_true_range()
    return df

# ============================================================
# V4 SIGNAL ENGINE
# No candle-pattern gate. Everything is calculated from numerical
# OHLC/indicator values returned by Twelve Data.
# ============================================================


def _rsi_score_call(rsi):
    if rsi >= 60:
        return 20
    if rsi >= 57:
        return 16
    if rsi >= 55:
        return 12
    if rsi >= 52:
        return 7
    return 0


def _rsi_score_put(rsi):
    if rsi <= 40:
        return 20
    if rsi <= 43:
        return 16
    if rsi <= 45:
        return 12
    if rsi <= 48:
        return 7
    return 0


def _pullback_score(price, ema20, atr):
    distance = abs(price - ema20)
    if distance <= atr * 0.75:
        return 15
    if distance <= atr * 1.5:
        return 12
    if distance <= atr * 2.0:
        return 8
    return 0


def _atr_score(atr, atr_average):
    if atr >= atr_average * 1.05:
        return 15
    if atr >= atr_average * 0.85:
        return 10
    if atr >= atr_average * 0.70:
        return 5
    return 0


def generate_signal(df):
    if len(df) < 60:
        return None

    last = df.iloc[-1]
    previous = df.iloc[-2]
    previous2 = df.iloc[-3]

    price = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi = float(last["rsi"])
    atr = float(last["atr"])
    prev_ema20 = float(previous["ema20"])
    atr_average = float(df["atr"].tail(30).mean())

    values = [price, ema20, ema50, rsi, atr, prev_ema20, atr_average]
    if any(pd.isna(value) for value in values):
        return None

    pullback_score = _pullback_score(price, ema20, atr)
    atr_score = _atr_score(atr, atr_average)

    uptrend = ema20 > ema50
    downtrend = ema20 < ema50
    ema_slope_up = ema20 > prev_ema20
    ema_slope_down = ema20 < prev_ema20

    short_momentum_up = float(last["close"]) > float(previous["close"]) > float(previous2["close"])
    short_momentum_down = float(last["close"]) < float(previous["close"]) < float(previous2["close"])

    # Minimum directional RSI prevents weak cases like RSI 50.1 CALL.
    if uptrend and rsi >= 52:
        score = 30
        reasons = ["EMA20 > EMA50"]

        if ema_slope_up:
            score += 10
            reasons.append("EMA20 Rising")

        rsi_points = _rsi_score_call(rsi)
        score += rsi_points
        if rsi_points:
            reasons.append("RSI Bullish")

        score += pullback_score
        if pullback_score:
            reasons.append("Pullback EMA20")

        score += atr_score
        if atr_score:
            reasons.append("Healthy ATR")

        if short_momentum_up:
            score += 10
            reasons.append("Momentum Up")

        if score >= CONFIDENCE_THRESHOLD:
            return {
                "signal": "CALL",
                "confidence": min(score, 100),
                "reasons": reasons,
                "price": price,
                "rsi": rsi,
            }

    if downtrend and rsi <= 48:
        score = 30
        reasons = ["EMA20 < EMA50"]

        if ema_slope_down:
            score += 10
            reasons.append("EMA20 Falling")

        rsi_points = _rsi_score_put(rsi)
        score += rsi_points
        if rsi_points:
            reasons.append("RSI Bearish")

        score += pullback_score
        if pullback_score:
            reasons.append("Pullback EMA20")

        score += atr_score
        if atr_score:
            reasons.append("Healthy ATR")

        if short_momentum_down:
            score += 10
            reasons.append("Momentum Down")

        if score >= CONFIDENCE_THRESHOLD:
            return {
                "signal": "PUT",
                "confidence": min(score, 100),
                "reasons": reasons,
                "price": price,
                "rsi": rsi,
            }

    return None

# ============================================================
# TELEGRAM
# ============================================================


def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN missing.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}

    try:
        response = requests.post(url, data=payload, timeout=30)
        print("Telegram:", response.status_code, response.text)
        return response.ok
    except Exception as exc:
        print(f"Telegram error: {exc}")
        return False

# ============================================================
# WEB PUSH
# ============================================================


def save_push_subscription(subscription):
    endpoint = subscription.get("endpoint")
    if not endpoint:
        return False

    with db_lock:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO push_subscriptions(endpoint, subscription_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                subscription_json = excluded.subscription_json,
                created_at = excluded.created_at
            """,
            (endpoint, json.dumps(subscription), current_time()),
        )
        conn.commit()
        conn.close()
    return True


def remove_push_subscription(endpoint):
    with db_lock:
        conn = get_db()
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        conn.commit()
        conn.close()


def send_web_push(signal, pair, confidence, entry_price, reasons):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        print("[PUSH] VAPID keys are not configured.")
        return

    if webpush is None:
        print("[PUSH] pywebpush is not installed.")
        return

    with db_lock:
        conn = get_db()
        rows = conn.execute(
            "SELECT endpoint, subscription_json FROM push_subscriptions"
        ).fetchall()
        conn.close()

    if not rows:
        print("[PUSH] No subscribed devices.")
        return

    payload = json.dumps(
        {
            "title": f"{signal} • {pair}",
            "body": (
                f"Score {confidence}% • Entry {entry_price:.5f} • "
                f"Expiry {EXPIRY_MINUTES} min"
            ),
            "signal": signal,
            "pair": pair,
            "confidence": confidence,
            "entry_price": entry_price,
            "reasons": reasons,
            "url": "/app",
        }
    )

    for row in rows:
        try:
            webpush(
                subscription_info=json.loads(row["subscription_json"]),
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=300,
            )
            print("[PUSH] Notification sent.")
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code in (404, 410):
                remove_push_subscription(row["endpoint"])
                print("[PUSH] Removed expired subscription.")
            else:
                print(f"[PUSH] Failed: {exc}")

# ============================================================
# SIGNAL STORAGE / RESULTS
# ============================================================


def save_signal(pair, signal, confidence, entry_price, rsi, session, reasons):
    with db_lock:
        conn = get_db()
        cursor = conn.execute(
            """
            INSERT INTO signals(
                created_at, pair, signal, confidence, entry_price,
                rsi, session, reasons, result, expiry_minutes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                current_time(),
                pair,
                signal,
                confidence,
                entry_price,
                rsi,
                session,
                ", ".join(reasons),
                "PENDING",
                EXPIRY_MINUTES,
            ),
        )
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
    return signal_id


def update_result(signal_id, exit_price, result):
    with db_lock:
        conn = get_db()
        conn.execute(
            "UPDATE signals SET exit_price = ?, result = ? WHERE id = ?",
            (exit_price, result, signal_id),
        )
        conn.commit()
        conn.close()


def check_signal_result(signal_id, pair, signal, entry_price):
    print(f"[RESULT] Signal #{signal_id} waiting {EXPIRY_MINUTES} minutes...")
    time.sleep(EXPIRY_MINUTES * 60)

    exit_price = get_current_price(pair)
    if exit_price is None:
        print(f"[RESULT] Could not get exit price for {pair}")
        return

    if signal == "CALL":
        if exit_price > entry_price:
            result = "WIN"
        elif exit_price < entry_price:
            result = "LOSS"
        else:
            result = "DRAW"
    else:
        if exit_price < entry_price:
            result = "WIN"
        elif exit_price > entry_price:
            result = "LOSS"
        else:
            result = "DRAW"

    update_result(signal_id, exit_price, result)
    print(
        f"[RESULT] #{signal_id} {pair} {signal} "
        f"Entry={entry_price:.5f} Exit={exit_price:.5f} Result={result}"
    )


def is_duplicate(pair, signal):
    now = datetime.now(timezone.utc)
    key = (pair, signal)

    with last_signal_lock:
        previous = last_signal_meta.get(key)
        if previous is not None:
            age_minutes = (now - previous).total_seconds() / 60
            if age_minutes < DUPLICATE_COOLDOWN_MINUTES:
                return True
        last_signal_meta[key] = now
        return False


def has_pending_trade(pair):
    with db_lock:
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM signals WHERE pair = ? AND result = 'PENDING' LIMIT 1",
            (pair,),
        ).fetchone()
        conn.close()
    return row is not None

# ============================================================
# SCANNER
# ============================================================


def update_latest_market(pair, price, rsi, signal=None, score=None, active=False):
    with market_lock:
        latest_market[pair].update(
            {
                "price": price,
                "rsi": rsi,
                "score": score,
                "signal": signal,
                "updated_at": current_time(),
                "active": active,
            }
        )


def scan_pair(pair, timeframe, active_session):
    print(f"\nScanning {pair}...")

    df = get_market_data(pair, timeframe)
    if df is None:
        print(f"{pair}: No market data.")
        return

    try:
        df = add_indicators(df)
    except Exception as exc:
        print(f"{pair}: Indicator error: {exc}")
        return

    last = df.iloc[-1]
    price = float(last["close"])
    rsi = float(last["rsi"])

    print(f"{pair} | Price={price:.5f} | RSI={rsi:.1f}")

    result = generate_signal(df)
    if result is None:
        update_latest_market(pair, price, rsi, active=True)
        print(f"{pair}: No valid signal.")
        return

    signal = result["signal"]
    confidence = result["confidence"]
    reasons = result["reasons"]

    update_latest_market(pair, price, rsi, signal=signal, score=confidence, active=True)

    if has_pending_trade(pair):
        print(f"{pair}: Pending trade already exists; skipping new signal.")
        return

    if is_duplicate(pair, signal):
        print(
            f"{pair}: Duplicate {signal} ignored "
            f"(cooldown {DUPLICATE_COOLDOWN_MINUTES} min)."
        )
        return

    signal_id = save_signal(
        pair=pair,
        signal=signal,
        confidence=confidence,
        entry_price=result["price"],
        rsi=result["rsi"],
        session=active_session,
        reasons=reasons,
    )

    emoji = "🟢" if signal == "CALL" else "🔴"
    message_lines = [
        f"{emoji} {signal} {pair}",
        "",
        f"Score: {confidence}%",
        f"Expiry: {EXPIRY_MINUTES} Minutes",
        f"Session: {active_session}",
        "",
        "Reasons:",
    ]
    message_lines.extend(f"✓ {reason}" for reason in reasons)
    message_lines.extend(
        [
            "",
            f"Entry Price: {result['price']:.5f}",
            f"RSI: {result['rsi']:.1f}",
            f"Time: {current_time()}",
        ]
    )
    message = "\n".join(message_lines)

    print("\n" + message)

    if send_telegram(message):
        print(f"{pair}: Telegram signal sent.")
    else:
        print(f"{pair}: Telegram signal FAILED.")

    send_web_push(
        signal=signal,
        pair=pair,
        confidence=confidence,
        entry_price=result["price"],
        reasons=reasons,
    )

    result_thread = threading.Thread(
        target=check_signal_result,
        args=(signal_id, pair, signal, result["price"]),
        daemon=True,
    )
    result_thread.start()


def run_scan():
    if USE_TRADING_HOURS:
        active_session, active_pairs = get_active_session_and_pairs()
        if not active_pairs:
            print(f"[{current_time()}] Outside trading session.")
            return
    else:
        active_session = "24H"
        active_pairs = list(PAIRS.keys())[:2]

    print(
        f"[{current_time()}] Starting market scan... "
        f"Session={active_session} Pairs={', '.join(active_pairs)}"
    )

    for pair in active_pairs:
        try:
            scan_pair(pair, PAIRS[pair], active_session)
        except Exception as exc:
            print(f"{pair}: Unexpected error: {exc}")
        time.sleep(3)

    print(f"[{current_time()}] Scan complete.")

# ============================================================
# PUSH ROUTES
# ============================================================


@app.get("/api/push/public-key")
def push_public_key():
    return {"public_key": VAPID_PUBLIC_KEY or ""}


@app.post("/api/push/subscribe")
def push_subscribe(subscription: dict):
    if not save_push_subscription(subscription):
        return {"success": False, "message": "Invalid subscription."}
    return {"success": True, "message": "Notifications enabled."}


@app.delete("/api/push/subscribe")
def push_unsubscribe(subscription: dict):
    endpoint = subscription.get("endpoint")
    if endpoint:
        remove_push_subscription(endpoint)
    return {"success": True}

# ============================================================
# PWA / HOME
# ============================================================


@app.get("/app", response_class=HTMLResponse)
def app_page():
    return FileResponse("static/index.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return FileResponse("static/index.html")


@app.get("/manifest.json")
def manifest():
    return FileResponse(
        "static/manifest.json",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
    )


@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <!doctype html>
    <html>
    <head>
      <meta name='viewport' content='width=device-width,initial-scale=1'>
      <title>AI Forex Signal Bot</title>
      <style>
        body{font-family:Arial;background:#080c17;color:white;text-align:center;padding:50px 20px}
        a{display:inline-block;margin:8px;padding:13px 20px;border-radius:10px;background:#2563eb;color:#fff;text-decoration:none}
      </style>
    </head>
    <body>
      <h1>AI Forex Signal Bot</h1>
      <p>V4 Balanced Quality • 5-minute candles • 2-minute expiry</p>
      <a href='/app'>Open App</a>
      <a href='/dashboard'>Open Dashboard</a>
    </body>
    </html>
    """


@app.get("/health")
def health():
    session, active_pairs = get_active_session_and_pairs()
    return {
        "status": "healthy",
        "time": current_time(),
        "session": session,
        "active_pairs": active_pairs,
        "threshold": CONFIDENCE_THRESHOLD,
        "expiry_minutes": EXPIRY_MINUTES,
    }

# ============================================================
# DASHBOARD DATA - MULTI-DAY HISTORY
# ============================================================


def parse_created_at(value):
    try:
        return IST.localize(datetime.strptime(value, "%d-%m-%Y %H:%M:%S"))
    except Exception:
        return None


def calc_stats(rows):
    total = len(rows)
    wins = sum(1 for row in rows if row["result"] == "WIN")
    losses = sum(1 for row in rows if row["result"] == "LOSS")
    pending = sum(1 for row in rows if row["result"] == "PENDING")
    draws = sum(1 for row in rows if row["result"] == "DRAW")
    completed = wins + losses
    win_rate = round((wins / completed) * 100, 1) if completed else 0
    avg_score = round(sum(int(row["confidence"]) for row in rows) / total, 1) if total else 0

    return {
        "signals": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "draws": draws,
        "win_rate": win_rate,
        "avg_score": avg_score,
        "completed": completed,
    }


@app.get("/api/dashboard")
def dashboard_data(days: int = 7):
    days = max(0, min(days, 3650))

    with db_lock:
        conn = get_db()
        db_rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT 5000"
        ).fetchall()
        conn.close()

    rows = list(db_rows)

    now = datetime.now(IST)
    start_dt = None if days == 0 else (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    selected = []
    for row in rows:
        dt = parse_created_at(row["created_at"])
        if dt is None:
            continue
        if start_dt is None or dt >= start_dt:
            selected.append(row)

    overall = calc_stats(rows)
    selected_stats = calc_stats(selected)

    # Pair breakdown for the selected period.
    pair_stats = {}
    for pair in PAIRS:
        pair_rows = [row for row in selected if row["pair"] == pair]
        pair_stats[pair] = calc_stats(pair_rows)

    # Session breakdown for the selected period.
    session_names = ["Sydney", "Tokyo", "London", "London / New York", "New York"]
    session_stats = {}
    for session in session_names:
        session_rows = [row for row in selected if row["session"] == session]
        session_stats[session] = calc_stats(session_rows)

    # Daily breakdown: every day represented in the selected period.
    daily_map = {}
    for row in selected:
        dt = parse_created_at(row["created_at"])
        if dt is None:
            continue
        day = dt.strftime("%Y-%m-%d")
        daily_map.setdefault(day, []).append(row)

    daily = []
    for day, day_rows in sorted(daily_map.items(), reverse=True):
        stats = calc_stats(day_rows)
        daily.append({"date": day, **stats})

    history = []
    for row in selected[:300]:
        history.append(
            {
                "id": row["id"],
                "time": row["created_at"],
                "pair": row["pair"],
                "signal": row["signal"],
                "confidence": row["confidence"],
                "entry_price": row["entry_price"],
                "exit_price": row["exit_price"],
                "rsi": row["rsi"],
                "result": row["result"],
                "session": row["session"],
                "reasons": row["reasons"],
            }
        )

    active_session, active_pairs = get_active_session_and_pairs()

    with market_lock:
        pair_data = {
            pair: {
                **latest_market[pair],
                "is_active_pair": pair in active_pairs,
            }
            for pair in PAIRS
        }

    range_label = "All Time" if days == 0 else f"Last {days} Day" + ("" if days == 1 else "s")

    return {
        # Backward-compatible summary fields
        "total": selected_stats["signals"],
        "wins": selected_stats["wins"],
        "losses": selected_stats["losses"],
        "pending": selected_stats["pending"],
        "draws": selected_stats["draws"],
        "win_rate": selected_stats["win_rate"],
        "signals": history,

        # New multi-day dashboard data
        "range": {
            "days": days,
            "label": range_label,
        },
        "stats": {
            "signals": selected_stats["signals"],
            "signals_today": selected_stats["signals"],
            "wins": selected_stats["wins"],
            "losses": selected_stats["losses"],
            "pending": selected_stats["pending"],
            "draws": selected_stats["draws"],
            "win_rate": selected_stats["win_rate"],
            "avg_score": selected_stats["avg_score"],
            "completed": selected_stats["completed"],
        },
        "all_time": overall,
        "session": active_session,
        "active_pairs": active_pairs,
        "pairs": pair_data,
        "pair_stats": pair_stats,
        "session_stats": session_stats,
        "daily": daily,
        "history": history,
    }

# ============================================================
# BACKGROUND BOT
# ============================================================


def scanner_loop():
    init_database()
    print("======================================")
    print("AI FOREX SIGNAL BOT V4")
    print("======================================")
    print("Pairs: session-based multi-pair scanning")
    print("Timeframe: 5 minutes")
    print("Expiry: 2 minutes")
    print(f"Score threshold: {CONFIDENCE_THRESHOLD}%")
    print("Candle patterns: REMOVED as a required filter")
    print(f"Duplicate cooldown: {DUPLICATE_COOLDOWN_MINUTES} minutes")
    print("App: /app")
    print("Dashboard: /dashboard")
    print("Timezone: Asia/Kolkata")
    print("======================================")

    while True:
        try:
            run_scan()
        except Exception as exc:
            print(f"Scanner error: {exc}")

        print(f"Next scan in {SCAN_INTERVAL // 60} minutes...")
        time.sleep(SCAN_INTERVAL)


init_database()
threading.Thread(target=scanner_loop, daemon=True).start()
