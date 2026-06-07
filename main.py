from fastapi import FastAPI, Request
import urllib.request
import json

app = FastAPI()

TOKEN = "8809538407:AAGEzIhAppNzu6SsWtXsDB2mwmDXQKo0lFI"
CHAT_ID = "-1003903509447" 
RENDER_URL = "https://onrender.com"

@app.on_event("startup")
def setup_webhook():
    try:
        # Pushing the registration payload securely inside a POST request body
        telegram_url = f"https://telegram.org{TOKEN}/setWebhook"
        payload = {"url": RENDER_URL}
        
        data_bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            telegram_url, 
            data=data_bytes, 
            headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
            print(f"🤖 WEBHOOK REGISTRATION RESULT: {result}")
    except Exception as e:
        print(f"🤖 Webhook setup failed: {e}")

@app.get("/")
def home():
    return {"status": "AI Core Active"}

@app.post("/")
async def receive_telegram_message(request: Request):
    try:
        data = await request.json()
        print(f"🤖 RECEIVED FROM TELEGRAM: {data}")
        
        if "message" in data and "text" in data["message"]:
            incoming_text = data["message"]["text"]
            alert_text = f"🤖 Bot Signal Received:\n{incoming_text}"
            
            telegram_url = f"https://telegram.org{TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": alert_text}
            
            data_bytes = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                telegram_url, 
                data=data_bytes, 
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req) as response:
                response.read()
                
    except Exception as e:
        print(f"Error handling message: {e}")
        
    return {"status": "success"}
