# test_services.py
import asyncio
import requests
import websockets

BOT_URL = "https://sara-ai-bot.onrender.com/health"
STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"

# --- 1️⃣ Check Flask health ---
def check_flask():
    try:
        r = requests.get(BOT_URL, timeout=5)
        print("Flask /health status:", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        print("Flask check failed:", e)
        return False

# --- 2️⃣ Check WebSocket streaming ---
async def check_stream():
    try:
        async with websockets.connect(STREAM_URL) as ws:
            print("Connected to streaming server ✅")
            # send a dummy start event to see if it accepts JSON
            await ws.send('{"event":"start","start":{"callSid":"TEST123","sampleRate":8000}}')
            resp = await asyncio.wait_for(ws.recv(), timeout=5)
            print("Streaming server replied:", resp[:200])
            return True
    except Exception as e:
        print("Streaming server check failed:", e)
        return False

async def main():
    print("Testing Flask service...")
    flask_ok = check_flask()

    print("\nTesting Streaming service...")
    stream_ok = await check_stream()

    print("\n--- SUMMARY ---")
    print("Flask OK:", flask_ok)
    print("Streaming OK:", stream_ok)

if __name__ == "__main__":
    asyncio.run(main())
