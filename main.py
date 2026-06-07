from fastapi import FastAPI, Request
import httpx

app = FastAPI()

# PASTE YOUR REAL KEYS DIRECTLY BETWEEN THE QUOTES BELOW:
TOKEN = "8809538407:AAGEzIhAppNzu6SsWtXsDB2mwmDXQKo0lFI"
CHAT_ID = "-1003903509447" 

@app.get("/")
def home():
    return {"status": "AI Core Active"}

@app.post("/")
async def receive_telegram_message(request: Request):
    try:
        data = await request.json()
        
        if "message" in data and "text" in data["message"]:
            incoming_text = data["message"]["text"]
            alert_text = f"🤖 Bot Signal Received:\n{incoming_text}"
            
            telegram_url = f"https://telegram.org{TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": alert_text}
            
            async with httpx.AsyncClient() as client:
                await client.post(telegram_url, json=payload)
                
    except Exception as e:
        print(f"Error handling message: {e}")
        
    return {"status": "success"}
