import asyncio
import websockets

STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"

async def test_connection():
    try:
        async with websockets.connect(STREAM_URL) as ws:
            print("✅ Connected successfully to streaming server")
            await ws.send("ping")
            msg = await ws.recv()
            print("Received:", msg)
    except Exception as e:
        print("❌ Connection failed:", e)

asyncio.run(test_connection())
