from fastapi import FastAPI, Request
import os
import httpx

app = FastAPI()

# Automatically pulls your secret keys from your Render Environment tab
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

@app.get("/")
def home():
    # This keeps your browser status working perfectly
    return {"status": "AI Core Active"}

@app.post("/")
async def receive_telegram_message(request: Request):
    try:
        # 1. Listen to the data payload coming from Telegram
        data = await request.json()
        
        # 2. Check if a text message was sent to the bot
        if "message" in data and "text" in data["message"]:
            incoming_text = data["message"]["text"]
            
            # 3. Format the message for your channel
            alert_text = f"🤖 Bot Signal Received:\n{incoming_text}"
            
            # 4. Push the message directly into your Telegram Channel
            telegram_url = f"https://telegram.org{TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": alert_text}
            
            async with httpx.AsyncClient() as client:
                await client.post(telegram_url, json=payload)
                
    except Exception as e:
        print(f"Error handling message: {e}")
        
    return {"status": "success"}
