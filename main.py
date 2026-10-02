import os
import time
import threading
import sqlite3
from datetime import datetime

import pandas as pd
import pytz
import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# ============================================================
# CONFIGURATION
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "-1003903509447"
)

IST = pytz.timezone("Asia/Kolkata")

PAIRS = {
    "EUR/USD": "5min",
    "GBP/USD": "5min",
}

CONFIDENCE_THRESHOLD = 70

SCAN_INTERVAL = 300
EXPIRY_MINUTES = 2

USE_TRADING_HOURS = True

DB_FILE = "signals.db"


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    connection = sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    return connection


def init_database():

    with db_lock:

        conn = get_db()

        conn.execute("""
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
        """)

        conn.commit()
        conn.close()


# ============================================================
# HOME
# ============================================================

@app.get("/", response_class=HTMLResponse)
def home():

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>AI Forex Signal Bot</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #0f172a;
                color: white;
                text-align: center;
                padding: 50px 20px;
            }

            h1 {
                color: #38bdf8;
            }

            a {
                display: inline-block;
                margin-top: 20px;
                padding: 14px 22px;
                background: #2563eb;
                color: white;
                text-decoration: none;
                border-radius: 10px;
            }
        </style>
    </head>

    <body>

        <h1>AI Forex Signal Bot</h1>

        <p>EUR/USD • GBP/USD</p>
        <p>5-minute candles • 2-minute expiry</p>

        <a href="/dashboard">
            Open Dashboard
        </a>

    </body>
    </html>
    """


@app.get("/health")
def health():

    return {
        "status": "healthy",
        "time": datetime.now(IST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    }


# ============================================================
# TIME
# ============================================================

def current_time():

    return datetime.now(IST).strftime(
        "%d-%m-%Y %H:%M:%S"
    )


def get_session():

    now = datetime.now(IST)

    minutes = now.hour * 60 + now.minute

    london_start = 13 * 60 + 30
    london_end = 17 * 60 + 30

    new_york_start = 18 * 60 + 30
    new_york_end = 22 * 60 + 30

    if london_start <= minutes <= london_end:
        return "London"

    if new_york_start <= minutes <= new_york_end:
        return "New York"

    return "Outside Session"


def trading_hours():

    return get_session() != "Outside Session"


# ============================================================
# TWELVE DATA
# ============================================================

def get_market_data(symbol, interval):

    if not TWELVE_DATA_API_KEY:

        print(
            "ERROR: TWELVE_DATA_API_KEY is missing."
        )

        return None

    url = (
        "https://api.twelvedata.com/time_series"
    )

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "apikey": TWELVE_DATA_API_KEY
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        if "values" not in data:

            print(
                f"Twelve Data error for {symbol}: "
                f"{data}"
            )

            return None

        df = pd.DataFrame(
            data["values"]
        )

        for column in [
            "open",
            "high",
            "low",
            "close"
        ]:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close"
            ]
        )

        df = df.iloc[::-1].reset_index(
            drop=True
        )

        return df

    except requests.RequestException as e:

        print(
            f"Network error getting {symbol}: {e}"
        )

        return None

    except Exception as e:

        print(
            f"Unexpected market-data error: {e}"
        )

        return None


# ============================================================
# CURRENT PRICE
# ============================================================

def get_current_price(symbol):

    if not TWELVE_DATA_API_KEY:
        return None

    url = "https://api.twelvedata.com/quote"

    params = {
        "symbol": symbol,
        "apikey": TWELVE_DATA_API_KEY
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        response.raise_for_status()

        data = response.json()

        price = data.get("close")

        if price is None:
            price = data.get("price")

        if price is None:
            return None

        return float(price)

    except Exception as e:

        print(
            f"Quote error for {symbol}: {e}"
        )

        return None


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    df["ema20"] = EMAIndicator(
        close=df["close"],
        window=20
    ).ema_indicator()

    df["ema50"] = EMAIndicator(
        close=df["close"],
        window=50
    ).ema_indicator()

    df["rsi"] = RSIIndicator(
        close=df["close"],
        window=14
    ).rsi()

    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14
    )

    df["atr"] = atr.average_true_range()

    return df


# ============================================================
# CANDLE PATTERNS
# ============================================================

def bullish_engulfing(df):

    if len(df) < 2:
        return False

    previous = df.iloc[-2]
    current = df.iloc[-1]

    return (
        previous["close"] < previous["open"]
        and current["close"] > current["open"]
        and current["open"] < previous["close"]
        and current["close"] > previous["open"]
    )


def bearish_engulfing(df):

    if len(df) < 2:
        return False

    previous = df.iloc[-2]
    current = df.iloc[-1]

    return (
        previous["close"] > previous["open"]
        and current["close"] < current["open"]
        and current["open"] > previous["close"]
        and current["close"] < previous["open"]
    )


def hammer(df):

    candle = df.iloc[-1]

    body = abs(
        candle["close"] -
        candle["open"]
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"]
        )
        -
        candle["low"]
    )

    upper_wick = (
        candle["high"]
        -
        max(
            candle["open"],
            candle["close"]
        )
    )

    if body <= 0:
        return False

    return (
        lower_wick > body * 2
        and upper_wick < body
    )


def shooting_star(df):

    candle = df.iloc[-1]

    body = abs(
        candle["close"] -
        candle["open"]
    )

    upper_wick = (
        candle["high"]
        -
        max(
            candle["open"],
            candle["close"]
        )
    )

    lower_wick = (
        min(
            candle["open"],
            candle["close"]
        )
        -
        candle["low"]
    )

    if body <= 0:
        return False

    return (
        upper_wick > body * 2
        and lower_wick < body
    )


# ============================================================
# SIGNAL ENGINE
# ============================================================

def generate_signal(df):

    if len(df) < 60:
        return None

    last = df.iloc[-1]

    price = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi = float(last["rsi"])
    atr = float(last["atr"])

    values = [
        price,
        ema20,
        ema50,
        rsi,
        atr
    ]

    if any(
        pd.isna(value)
        for value in values
    ):
        return None

    atr_average = float(
        df["atr"].mean()
    )

    confidence = 0

    reasons = []

    uptrend = ema20 > ema50
    downtrend = ema20 < ema50

    # Trend
    if uptrend:

        confidence += 25
        reasons.append("Trend Up")

    elif downtrend:

        confidence += 25
        reasons.append("Trend Down")

    # Pullback
    if abs(
        price - ema20
    ) <= atr * 2:

        confidence += 20
        reasons.append("Pullback EMA20")

    # Volatility
    if atr > atr_average:

        confidence += 20
        reasons.append("ATR Confirmed")

    # CALL
    if uptrend and rsi > 50:

        if bullish_engulfing(df):

            confidence += 35
            reasons.append(
                "Bullish Engulfing"
            )

        elif hammer(df):

            confidence += 35
            reasons.append("Hammer")

        if confidence >= CONFIDENCE_THRESHOLD:

            return {
                "signal": "CALL",
                "confidence": confidence,
                "reasons": reasons,
                "price": price,
                "rsi": rsi
            }

    # PUT
    if downtrend and rsi < 50:

        if bearish_engulfing(df):

            confidence += 35
            reasons.append(
                "Bearish Engulfing"
            )

        elif shooting_star(df):

            confidence += 35
            reasons.append(
                "Shooting Star"
            )

        if confidence >= CONFIDENCE_THRESHOLD:

            return {
                "signal": "PUT",
                "confidence": confidence,
                "reasons": reasons,
                "price": price,
                "rsi": rsi
            }

    return None


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:

        print(
            "ERROR: TELEGRAM_BOT_TOKEN missing."
        )

        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        "/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:

        response = requests.post(
            url,
            data=payload,
            timeout=30
        )

        print(
            "Telegram:",
            response.status_code,
            response.text
        )

        return response.ok

    except Exception as e:

        print(
            f"Telegram error: {e}"
        )

        return False


# ============================================================
# SAVE SIGNAL
# ============================================================

def save_signal(
    pair,
    signal,
    confidence,
    entry_price,
    rsi,
    session,
    reasons
):

    with db_lock:

        conn = get_db()

        cursor = conn.execute(
            """
            INSERT INTO signals
            (
                created_at,
                pair,
                signal,
                confidence,
                entry_price,
                rsi,
                session,
                reasons,
                result,
                expiry_minutes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                EXPIRY_MINUTES
            )
        )

        signal_id = cursor.lastrowid

        conn.commit()

        conn.close()

    return signal_id


# ============================================================
# UPDATE RESULT
# ============================================================

def update_result(
    signal_id,
    exit_price,
    result
):

    with db_lock:

        conn = get_db()

        conn.execute(
            """
            UPDATE signals
            SET exit_price = ?,
                result = ?
            WHERE id = ?
            """,
            (
                exit_price,
                result,
                signal_id
            )
        )

        conn.commit()

        conn.close()


# ============================================================
# RESULT CHECKER
# ============================================================

def check_signal_result(
    signal_id,
    pair,
    signal,
    entry_price
):

    print(
        f"[RESULT] Signal #{signal_id} "
        f"waiting {EXPIRY_MINUTES} minutes..."
    )

    time.sleep(
        EXPIRY_MINUTES * 60
    )

    exit_price = get_current_price(
        pair
    )

    if exit_price is None:

        print(
            f"[RESULT] Could not get "
            f"exit price for {pair}"
        )

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

    update_result(
        signal_id,
        exit_price,
        result
    )

    print(
        f"[RESULT] #{signal_id} "
        f"{pair} {signal} "
        f"Entry={entry_price:.5f} "
        f"Exit={exit_price:.5f} "
        f"Result={result}"
    )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

last_signals = {}


def is_duplicate(
    pair,
    signal
):

    previous = last_signals.get(
        pair
    )

    if previous == signal:

        return True

    last_signals[pair] = signal

    return False


# ============================================================
# SCAN PAIR
# ============================================================

def scan_pair(
    pair,
    timeframe
):

    print()
    print(
        f"Scanning {pair}..."
    )

    df = get_market_data(
        pair,
        timeframe
    )

    if df is None:

        print(
            f"{pair}: No market data."
        )

        return

    try:

        df = add_indicators(df)

    except Exception as e:

        print(
            f"{pair}: Indicator error: {e}"
        )

        return

    last = df.iloc[-1]

    price = float(
        last["close"]
    )

    rsi = float(
        last["rsi"]
    )

    print(
        f"{pair} | "
        f"Price={price:.5f} | "
        f"RSI={rsi:.1f}"
    )

    result = generate_signal(df)

    if result is None:

        print(
            f"{pair}: No valid signal."
        )

        return

    signal = result["signal"]

    confidence = result["confidence"]

    reasons = result["reasons"]

    if is_duplicate(
        pair,
        signal
    ):

        print(
            f"{pair}: Duplicate "
            f"{signal} ignored."
        )

        return

    session = get_session()

    signal_id = save_signal(
        pair=pair,
        signal=signal,
        confidence=confidence,
        entry_price=result["price"],
        rsi=result["rsi"],
        session=session,
        reasons=reasons
    )

    emoji = (
        "🟢"
        if signal == "CALL"
        else "🔴"
    )

    message = (
        f"{emoji} {signal} {pair}\n\n"
        f"Confidence: {confidence}%\n\n"
        f"Expiry: "
        f"{EXPIRY_MINUTES} Minutes\n\n"
        f"Session: {session}\n\n"
        f"Reasons:\n"
    )

    for reason in reasons:

        message += (
            f"✓ {reason}\n"
        )

    message += (
        f"\nEntry Price: "
        f"{result['price']:.5f}"
        f"\nRSI: "
        f"{result['rsi']:.1f}"
        f"\nTime: "
        f"{current_time()}"
    )

    print()
    print(message)

    success = send_telegram(
        message
    )

    if success:

        print(
            f"{pair}: Telegram "
            f"signal sent."
        )

    else:

        print(
            f"{pair}: Telegram "
            f"signal FAILED."
        )

    # Result checker runs separately
    result_thread = threading.Thread(
        target=check_signal_result,
        args=(
            signal_id,
            pair,
            signal,
            result["price"]
        ),
        daemon=True
    )

    result_thread.start()


# ============================================================
# MARKET SCANNER
# ============================================================

def run_scan():

    if (
        USE_TRADING_HOURS
        and not trading_hours()
    ):

        print(
            f"[{current_time()}] "
            "Outside trading session."
        )

        return

    print(
        f"[{current_time()}] "
        "Starting market scan..."
    )

    for pair, timeframe in PAIRS.items():

        try:

            scan_pair(
                pair,
                timeframe
            )

        except Exception as e:

            print(
                f"{pair}: "
                f"Unexpected error: {e}"
            )

        time.sleep(3)

    print(
        f"[{current_time()}] "
        "Scan complete."
    )


# ============================================================
# DASHBOARD API
# ============================================================

@app.get("/api/dashboard")
def dashboard_data():

    with db_lock:

        conn = get_db()

        total = conn.execute(
            "SELECT COUNT(*) FROM signals"
        ).fetchone()[0]

        wins = conn.execute(
            """
            SELECT COUNT(*)
            FROM signals
            WHERE result = 'WIN'
            """
        ).fetchone()[0]

        losses = conn.execute(
            """
            SELECT COUNT(*)
            FROM signals
            WHERE result = 'LOSS'
            """
        ).fetchone()[0]

        pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM signals
            WHERE result = 'PENDING'
            """
        ).fetchone()[0]

        draws = conn.execute(
            """
            SELECT COUNT(*)
            FROM signals
            WHERE result = 'DRAW'
            """
        ).fetchone()[0]

        rows = conn.execute(
            """
            SELECT *
            FROM signals
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()

        conn.close()

    completed = wins + losses

    win_rate = (
        round(
            (wins / completed) * 100,
            1
        )
        if completed > 0
        else 0
    )

    signals = []

    for row in rows:

        signals.append({
            "id": row["id"],
            "created_at": row["created_at"],
            "pair": row["pair"],
            "signal": row["signal"],
            "confidence": row["confidence"],
            "entry_price": row["entry_price"],
            "exit_price": row["exit_price"],
            "rsi": row["rsi"],
            "session": row["session"],
            "reasons": row["reasons"],
            "result": row["result"]
        })

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "draws": draws,
        "win_rate": win_rate,
        "signals": signals
    }


# ============================================================
# DASHBOARD
# ============================================================

@app.get(
    "/dashboard",
    response_class=HTMLResponse
)
def dashboard():

    return """
<!DOCTYPE html>

<html>

<head>

<title>AI Forex Signal Dashboard</title>

<meta
    name="viewport"
    content="width=device-width, initial-scale=1"
/>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    font-family:
        Arial,
        Helvetica,
        sans-serif;

    background:
        linear-gradient(
            135deg,
            #020617,
            #0f172a
        );

    color: #f8fafc;
}

.container {

    max-width: 1250px;

    margin: auto;

    padding: 25px;
}

.header {

    display: flex;

    justify-content:
        space-between;

    align-items:
        center;

    gap: 20px;

    margin-bottom: 25px;

    flex-wrap: wrap;
}

.title {

    font-size: 30px;

    font-weight: 800;

    color: #38bdf8;
}

.subtitle {

    color: #94a3b8;

    margin-top: 5px;
}

.refresh {

    border: 0;

    background:
        #2563eb;

    color: white;

    padding:
        11px 18px;

    border-radius:
        10px;

    cursor: pointer;

    font-weight: 700;
}

.cards {

    display:
        grid;

    grid-template-columns:
        repeat(
            5,
            1fr
        );

    gap: 15px;
}

.card {

    background:
        rgba(
            15,
            23,
            42,
            0.9
        );

    border:
        1px solid
        #1e293b;

    border-radius:
        16px;

    padding: 20px;

    box-shadow:
        0 10px 30px
        rgba(
            0,
            0,
            0,
            0.2
        );
}

.label {

    color: #94a3b8;

    font-size: 12px;

    font-weight: 700;

    letter-spacing:
        0.5px;
}

.number {

    font-size: 32px;

    font-weight: 800;

    margin-top: 8px;
}

.blue {
    color: #38bdf8;
}

.green {
    color: #22c55e;
}

.red {
    color: #ef4444;
}

.yellow {
    color: #eab308;
}

.purple {
    color: #a855f7;
}

.section {

    margin-top: 18px;
}

.section-title {

    font-size: 18px;

    font-weight: 800;

    margin-bottom: 15px;
}

.analytics {

    display:
        grid;

    grid-template-columns:
        repeat(
            2,
            1fr
        );

    gap: 18px;
}

.stat-row {

    display:
        grid;

    grid-template-columns:
        100px
        1fr
        55px;

    align-items:
        center;

    gap: 10px;

    margin:
        13px 0;

    font-size: 13px;
}

.bar {

    height: 10px;

    background:
        #1e293b;

    border-radius:
        50px;

    overflow:
        hidden;
}

.fill {

    height: 100%;

    border-radius:
        50px;
}

.green-fill {
    background: #22c55e;
}

.red-fill {
    background: #ef4444;
}

.blue-fill {
    background: #3b82f6;
}

.purple-fill {
    background: #a855f7;
}

.table-container {

    overflow-x:
        auto;

    border:
        1px solid
        #1e293b;

    border-radius:
        14px;
}

table {

    width: 100%;

    border-collapse:
        collapse;

    min-width:
        900px;
}

th {

    text-align:
        left;

    padding:
        13px;

    background:
        #111827;

    color:
        #94a3b8;

    font-size:
        12px;
}

td {

    padding:
        13px;

    border-top:
        1px solid
        #1e293b;

    font-size:
        12px;
}

.pill {

    display:
        inline-block;

    padding:
        5px 9px;

    border-radius:
        20px;

    font-weight:
        800;

    font-size:
        11px;
}

.call {

    background:
        rgba(
            34,
            197,
            94,
            0.15
        );

    color:
        #22c55e;
}

.put {

    background:
        rgba(
            239,
            68,
            68,
            0.15
        );

    color:
        #ef4444;
}

.win {

    background:
        rgba(
            34,
            197,
            94,
            0.15
        );

    color:
        #22c55e;
}

.loss {

    background:
        rgba(
            239,
            68,
            68,
            0.15
        );

    color:
        #ef4444;
}

.pending {

    background:
        rgba(
            234,
            179,
            8,
            0.15
        );

    color:
        #eab308;
}

.draw {

    background:
        rgba(
            148,
            163,
            184,
            0.15
        );

    color:
        #94a3b8;
}

.empty {

    text-align:
        center;

    padding:
        30px;

    color:
        #64748b;
}

@media(max-width:900px) {

    .cards {

        grid-template-columns:
            repeat(
                3,
                1fr
            );
    }

}

@media(max-width:650px) {

    .container {
        padding: 15px;
    }

    .title {
        font-size: 23px;
    }

    .cards {

        grid-template-columns:
            repeat(
                2,
                1fr
            );
    }

    .analytics {

        grid-template-columns:
            1fr;
    }

}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div>

<div class="title">
AI Forex Signal Dashboard
</div>

<div class="subtitle">
EUR/USD • GBP/USD • 5M • 2M Expiry
</div>

</div>

<button
    class="refresh"
    onclick="loadDashboard()"
>
↻ Refresh
</button>

</div>


<div class="cards">

<div class="card">

<div class="label">
TOTAL SIGNALS
</div>

<div
    id="total"
    class="number blue"
>
0
</div>

</div>


<div class="card">

<div class="label">
WINS
</div>

<div
    id="wins"
    class="number green"
>
0
</div>

</div>


<div class="card">

<div class="label">
LOSSES
</div>

<div
    id="losses"
    class="number red"
>
0
</div>

</div>


<div class="card">

<div class="label">
PENDING
</div>

<div
    id="pending"
    class="number yellow"
>
0
</div>

</div>


<div class="card">

<div class="label">
WIN RATE
</div>

<div
    id="rate"
    class="number purple"
>
0%
</div>

</div>

</div>


<div class="analytics section">


<div class="card">

<div class="section-title">
Results
</div>

<div class="stat-row">

<span>Wins</span>

<div class="bar">
<div
    id="winsBar"
    class="fill green-fill"
    style="width:0%"
></div>
</div>

<strong id="winsPct">
0%
</strong>

</div>


<div class="stat-row">

<span>Losses</span>

<div class="bar">
<div
    id="lossBar"
    class="fill red-fill"
    style="width:0%"
></div>
</div>

<strong id="lossPct">
0%
</strong>

</div>

</div>


<div class="card">

<div class="section-title">
Directions
</div>

<div class="stat-row">

<span>CALL</span>

<div class="bar">
<div
    id="callBar"
    class="fill green-fill"
    style="width:50%"
></div>
</div>

<strong id="callCount">
0
</strong>

</div>


<div class="stat-row">

<span>PUT</span>

<div class="bar">
<div
    id="putBar"
    class="fill red-fill"
    style="width:50%"
></div>
</div>

<strong id="putCount">
0
</strong>

</div>

</div>


<div class="card">

<div class="section-title">
Pairs
</div>

<div class="stat-row">

<span>EUR/USD</span>

<div class="bar">
<div
    id="eurBar"
    class="fill blue-fill"
    style="width:50%"
></div>
</div>

<strong id="eurCount">
0
</strong>

</div>


<div class="stat-row">

<span>GBP/USD</span>

<div class="bar">
<div
    id="gbpBar"
    class="fill purple-fill"
    style="width:50%"
></div>
</div>

<strong id="gbpCount">
0
</strong>

</div>

</div>


<div class="card">

<div class="section-title">
Sessions
</div>

<div class="stat-row">

<span>London</span>

<div class="bar">
<div
    id="londonBar"
    class="fill purple-fill"
    style="width:50%"
></div>
</div>

<strong id="londonCount">
0
</strong>

</div>


<div class="stat-row">

<span>New York</span>

<div class="bar">
<div
    id="nyBar"
    class="fill blue-fill"
    style="width:50%"
></div>
</div>

<strong id="nyCount">
0
</strong>

</div>

</div>


</div>


<div class="card section">

<div class="section-title">
Signal History
</div>

<div class="table-container">

<table>

<thead>

<tr>

<th>Time</th>
<th>Pair</th>
<th>Signal</th>
<th>Confidence</th>
<th>Entry</th>
<th>Exit</th>
<th>RSI</th>
<th>Session</th>
<th>Result</th>

</tr>

</thead>

<tbody id="signalRows">

<tr>

<td
    colspan="9"
    class="empty"
>
No signals yet
</td>

</tr>

</tbody>

</table>

</div>

</div>

</div>


<script>

async function loadDashboard() {

    try {

        const response =
            await fetch(
                "/api/dashboard"
            );

        const data =
            await response.json();


        document.getElementById(
            "total"
        ).textContent =
            data.total;


        document.getElementById(
            "wins"
        ).textContent =
            data.wins;


        document.getElementById(
            "losses"
        ).textContent =
            data.losses;


        document.getElementById(
            "pending"
        ).textContent =
            data.pending;


        document.getElementById(
            "rate"
        ).textContent =
            data.win_rate + "%";


        const completed =
            data.wins +
            data.losses;


        const winPercent =
            completed
                ? (
                    data.wins /
                    completed *
                    100
                )
                : 0;


        const lossPercent =
            completed
                ? (
                    data.losses /
                    completed *
                    100
                )
                : 0;


        document.getElementById(
            "winsBar"
        ).style.width =
            winPercent + "%";


        document.getElementById(
            "lossBar"
        ).style.width =
            lossPercent + "%";


        document.getElementById(
            "winsPct"
        ).textContent =
            Math.round(
                winPercent
            ) + "%";


        document.getElementById(
            "lossPct"
        ).textContent =
            Math.round(
                lossPercent
            ) + "%";


        const signals =
            data.signals;


        const callCount =
            signals.filter(
                s =>
                    s.signal === "CALL"
            ).length;


        const putCount =
            signals.filter(
                s =>
                    s.signal === "PUT"
            ).length;


        const eurCount =
            signals.filter(
                s =>
                    s.pair === "EUR/USD"
            ).length;


        const gbpCount =
            signals.filter(
                s =>
                    s.pair === "GBP/USD"
            ).length;


        const londonCount =
            signals.filter(
                s =>
                    s.session === "London"
            ).length;


        const nyCount =
            signals.filter(
                s =>
                    s.session === "New York"
            ).length;


        document.getElementById(
            "callCount"
        ).textContent =
            callCount;


        document.getElementById(
            "putCount"
        ).textContent =
            putCount;


        document.getElementById(
            "eurCount"
        ).textContent =
            eurCount;


        document.getElementById(
            "gbpCount"
        ).textContent =
            gbpCount;


        document.getElementById(
            "londonCount"
        ).textContent =
            londonCount;


        document.getElementById(
            "nyCount"
        ).textContent =
            nyCount;


        const directionTotal =
            callCount +
            putCount;


        const pairTotal =
            eurCount +
            gbpCount;


        const sessionTotal =
            londonCount +
            nyCount;


        document.getElementById(
            "callBar"
        ).style.width =
            directionTotal
                ? callCount /
                    directionTotal *
                    100 + "%"
                : "0%";


        document.getElementById(
            "putBar"
        ).style.width =
            directionTotal
                ? putCount /
                    directionTotal *
                    100 + "%"
                : "0%";


        document.getElementById(
            "eurBar"
        ).style.width =
            pairTotal
                ? eurCount /
                    pairTotal *
                    100 + "%"
                : "0%";


        document.getElementById(
            "gbpBar"
        ).style.width =
            pairTotal
                ? gbpCount /
                    pairTotal *
                    100 + "%"
                : "0%";


        document.getElementById(
            "londonBar"
        ).style.width =
            sessionTotal
                ? londonCount /
                    sessionTotal *
                    100 + "%"
                : "0%";


        document.getElementById(
            "nyBar"
        ).style.width =
            sessionTotal
                ? nyCount /
                    sessionTotal *
                    100 + "%"
                : "0%";


        const rows =
            document.getElementById(
                "signalRows"
            );


        if (!signals.length) {

            rows.innerHTML = `
                <tr>
                    <td
                        colspan="9"
                        class="empty"
                    >
                        No signals yet
                    </td>
                </tr>
            `;

            return;
        }


        rows.innerHTML =
            signals.map(
                signal => {

                    let resultClass =
                        "pending";


                    if (
                        signal.result ===
                        "WIN"
                    ) {
                        resultClass =
                            "win";
                    }


                    if (
                        signal.result ===
                        "LOSS"
                    ) {
                        resultClass =
                            "loss";
                    }


                    if (
                        signal.result ===
                        "DRAW"
                    ) {
                        resultClass =
                            "draw";
                    }


                    const signalClass =
                        signal.signal ===
                        "CALL"
                            ? "call"
                            : "put";


                    return `
                    <tr>

                        <td>
                            ${signal.created_at}
                        </td>

                        <td>
                            ${signal.pair}
                        </td>

                        <td>
                            <span
                                class="pill ${signalClass}"
                            >
                                ${signal.signal}
                            </span>
                        </td>

                        <td>
                            ${signal.confidence}%
                        </td>

                        <td>
                            ${
                                signal.entry_price
                                    ? Number(
                                        signal.entry_price
                                      ).toFixed(5)
                                    : "-"
                            }
                        </td>

                        <td>
                            ${
                                signal.exit_price
                                    ? Number(
                                        signal.exit_price
                                      ).toFixed(5)
                                    : "-"
                            }
                        </td>

                        <td>
                            ${
                                signal.rsi
                                    ? Number(
                                        signal.rsi
                                      ).toFixed(1)
                                    : "-"
                            }
                        </td>

                        <td>
                            ${signal.session}
                        </td>

                        <td>
                            <span
                                class="pill ${resultClass}"
                            >
                                ${signal.result}
                            </span>
                        </td>

                    </tr>
                    `;

                }
            ).join("");

    }

    catch (error) {

        console.error(
            "Dashboard error:",
            error
        );

    }

}


loadDashboard();


setInterval(
    loadDashboard,
    15000
);

</script>

</body>

</html>
"""


# ============================================================
# BACKGROUND BOT
# ============================================================

def scanner_loop():

    init_database()

    print()
    print(
        "======================================"
    )

    print(
        "AI FOREX SIGNAL BOT"
    )

    print(
        "======================================"
    )

    print(
        "Pairs: EUR/USD, GBP/USD"
    )

    print(
        "Timeframe: 5 minutes"
    )

    print(
        "Expiry: 2 minutes"
    )

    print(
        f"Confidence: "
        f"{CONFIDENCE_THRESHOLD}%"
    )

    print(
        "Dashboard: /dashboard"
    )

    print(
        "Timezone: Asia/Kolkata"
    )

    print(
        "======================================"
    )

    while True:

        try:

            run_scan()

        except Exception as e:

            print(
                f"Scanner error: {e}"
            )

        print(
            f"Next scan in "
            f"{SCAN_INTERVAL // 60} "
            f"minutes..."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START DATABASE
# ============================================================

init_database()


# ============================================================
# START BACKGROUND THREAD
# ============================================================

scanner_thread = threading.Thread(
    target=scanner_loop,
    daemon=True
)

scanner_thread.start()
