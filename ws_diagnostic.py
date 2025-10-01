# ws_diagnostic.py
import asyncio
import websockets
import ssl
import json
import platform

ws_url = "wss://sara-ai-streaming.onrender.com/ws"  # update if needed

async def test_ws():
    print(f"[INFO] Python: {platform.python_version()} | websockets: {websockets.__version__}")
    try:
        ssl_ctx = ssl.SSLContext() if ws_url.startswith("wss") else None
        async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
            print("✅ Connected:", ws_url)
            msg = {"event": "start", "start": {"callSid": "diag-123", "timestamp": ""}}
            await ws.send(json.dumps(msg))
            print("➡️ Sent start")
            try:
                reply = await asyncio.wait_for(ws.recv(), timeout=5)
                print("⬅️ Received:", reply)
            except asyncio.TimeoutError:
                print("⚠️ No reply within 5s (server may not send explicit replies)")
    except Exception as e:
        print("❌ Connection failed:", e)

if __name__ == "__main__":
    asyncio.run(test_ws())
