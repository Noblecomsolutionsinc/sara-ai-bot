import os
import asyncio
import websockets
from dotenv import load_dotenv

load_dotenv()

async def test_websocket():
    stream_url = os.environ["PUBLIC_STREAMING_URL"]
    print(f"Testing WebSocket: {stream_url}")
    
    try:
        async with websockets.connect(stream_url) as ws:
            print("✅ WebSocket connected successfully!")
            return True
    except Exception as e:
        print(f"❌ WebSocket failed: {e}")
        return False

if __name__ == "__main__":
    result = asyncio.run(test_websocket())
    print(f"Result: {'SUCCESS' if result else 'FAILED'}")