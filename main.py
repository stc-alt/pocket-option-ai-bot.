import os
import time
import threading
from datetime import datetime

import pandas as pd
import pytz
import requests
from fastapi import FastAPI
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange


# ============================================================
# CONFIGURATION
# ============================================================

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "-1003903509447")

IST = pytz.timezone("Asia/Kolkata")

PAIRS = {
    "EUR/USD": "5min",
    "GBP/USD": "5min",
}

CONFIDENCE_THRESHOLD = 70
SCAN_INTERVAL = 300          # 5 minutes
EXPIRY_MINUTES = 2

USE_TRADING_HOURS = True


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI()


@app.get("/")
def home():
    return {
        "status": "AI Forex Signal Bot is running",
        "pairs": list(PAIRS.keys()),
        "timeframe": "5min",
        "expiry": "2 minutes"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "time": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    }


# ============================================================
# TIME
# ============================================================

def current_time():
    return datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")


def trading_hours():
    now = datetime.now(IST)

    minutes = now.hour * 60 + now.minute

    # London session
    london_start = 13 * 60 + 30
    london_end = 17 * 60 + 30

    # New York session
    new_york_start = 18 * 60 + 30
    new_york_end = 22 * 60 + 30

    london = london_start <= minutes <= london_end
    new_york = new_york_start <= minutes <= new_york_end

    return london or new_york


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

        df = pd.DataFrame(data["values"])

        for column in ["open", "high", "low", "close"]:

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

        # Twelve Data returns newest candle first.
        df = df.iloc[::-1].reset_index(drop=True)

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
        and
        current["close"] > current["open"]
        and
        current["open"] < previous["close"]
        and
        current["close"] > previous["open"]
    )


def bearish_engulfing(df):

    if len(df) < 2:
        return False

    previous = df.iloc[-2]
    current = df.iloc[-1]

    return (
        previous["close"] > previous["open"]
        and
        current["close"] < current["open"]
        and
        current["open"] > previous["close"]
        and
        current["close"] < previous["open"]
    )


def hammer(df):

    candle = df.iloc[-1]

    body = abs(
        candle["close"] -
        candle["open"]
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        -
        candle["low"]
    )

    upper_wick = (
        candle["high"]
        -
        max(candle["open"], candle["close"])
    )

    if body <= 0:
        return False

    return (
        lower_wick > body * 2
        and
        upper_wick < body
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
        max(candle["open"], candle["close"])
    )

    lower_wick = (
        min(candle["open"], candle["close"])
        -
        candle["low"]
    )

    if body <= 0:
        return False

    return (
        upper_wick > body * 2
        and
        lower_wick < body
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

    if any(pd.isna(value) for value in values):
        return None

    atr_average = float(df["atr"].mean())

    confidence = 0
    reasons = []

    uptrend = ema20 > ema50
    downtrend = ema20 < ema50

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    if uptrend:

        confidence += 25
        reasons.append("Trend Up")

    elif downtrend:

        confidence += 25
        reasons.append("Trend Down")

    # --------------------------------------------------------
    # EMA20 PULLBACK
    # --------------------------------------------------------

    if abs(price - ema20) <= atr * 2:

        confidence += 20
        reasons.append("Pullback EMA20")

    # --------------------------------------------------------
    # ATR
    # --------------------------------------------------------

    if atr > atr_average:

        confidence += 20
        reasons.append("ATR Confirmed")

    # --------------------------------------------------------
    # CALL
    # --------------------------------------------------------

    if uptrend and rsi > 50:

        if bullish_engulfing(df):

            confidence += 35
            reasons.append("Bullish Engulfing")

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

    # --------------------------------------------------------
    # PUT
    # --------------------------------------------------------

    if downtrend and rsi < 50:

        if bearish_engulfing(df):

            confidence += 35
            reasons.append("Bearish Engulfing")

        elif shooting_star(df):

            confidence += 35
            reasons.append("Shooting Star")

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
            "ERROR: TELEGRAM_BOT_TOKEN is missing."
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
# DUPLICATE PROTECTION
# ============================================================

last_signals = {}


def is_duplicate(pair, signal):

    previous = last_signals.get(pair)

    if previous == signal:
        return True

    last_signals[pair] = signal

    return False


# ============================================================
# SCAN
# ============================================================

def scan_pair(pair, timeframe):

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

    price = float(last["close"])
    rsi = float(last["rsi"])

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

    if is_duplicate(pair, signal):

        print(
            f"{pair}: Duplicate {signal} ignored."
        )

        return

    emoji = (
        "🟢"
        if signal == "CALL"
        else
        "🔴"
    )

    message = (
        f"{emoji} {signal} {pair}\n\n"
        f"Confidence: {confidence}%\n\n"
        f"Expiry: {EXPIRY_MINUTES} Minutes\n\n"
        f"Reasons:\n"
    )

    for reason in reasons:

        message += (
            f"✓ {reason}\n"
        )

    message += (
        f"\nPrice: {price:.5f}"
        f"\nRSI: {rsi:.1f}"
        f"\nTime: {current_time()}"
    )

    print()
    print(message)

    success = send_telegram(message)

    if success:

        print(
            f"{pair}: Telegram signal sent."
        )

    else:

        print(
            f"{pair}: Telegram signal FAILED."
        )


# ============================================================
# MARKET SCANNER
# ============================================================

def run_scan():

    if (
        USE_TRADING_HOURS
        and
        not trading_hours()
    ):

        print(
            f"[{current_time()}] "
            "Outside trading session."
        )

        return

    print()
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

    print()
    print(
        f"[{current_time()}] "
        "Scan complete."
    )


# ============================================================
# BACKGROUND BOT
# ============================================================

def scanner_loop():

    print()
    print(
        "======================================"
    )

    print(
        "FOREX SIGNAL BOT"
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
        f"Confidence: {CONFIDENCE_THRESHOLD}%"
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

        print()
        print(
            f"Next scan in "
            f"{SCAN_INTERVAL // 60} minutes..."
        )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# START BACKGROUND THREAD
# ============================================================

scanner_thread = threading.Thread(
    target=scanner_loop,
    daemon=True
)

scanner_thread.start()
