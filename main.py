import os
import requests
import pandas as pd
import numpy as np
from fastapi import FastAPI, BackgroundTasks
import uvicorn
from sklearn.ensemble import RandomForestClassifier

app = FastAPI()

# Fetch parameters directly from your secure cloud environment
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_alert(msg):
    if TOKEN and CHAT_ID:
        url = f"https://telegram.org{TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

@app.get("/")
def home():
    return {"status": "AI Core Active"}

@app.get("/tick/{symbol}/{open_p}/{high_p}/{low_p}/{close_p}")
def process_tick(symbol: str, open_p: float, high_p: float, low_p: float, close_p: float):
    """Processes live incoming candle ticks and tracks them natively"""
    # Simply mapping structural logic into readable strings for execution
    msg = f"⚡ *AI SIGNAL UPDATE*\nAsset: {symbol}\nPrice: {close_p}\nStatus: Analyzing Flow..."
    send_alert(msg)
    return {"processed": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
