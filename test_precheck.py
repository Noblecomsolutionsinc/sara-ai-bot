import os
import asyncio
import websockets
import requests
from dotenv import load_dotenv

load_dotenv()

async def test_connection():
    stream_url = os.environ["PUBLIC_STREAMING_URL"]
    flask_url = os.environ["SERVER_URL"]
    
    print(f"Testing Flask: {flask_url}/health")
    try:
        resp = requests.get(f"{flask_url}/health", timeout=5)
        print(f"Flask OK: {resp.status_code}")
    except Exception as e:
        print(f"Flask FAILED: {e}")
        return
    
    print(f"Testing WebSocket: {stream_url}")
    try:
        # Fix: Use connect_timeout instead of timeout
        async with websockets.connect(stream_url, connect_timeout=10) as ws:
            print("WebSocket connected successfully!")
            # Test if we can send and receive
            await ws.send('{"event": "test"}')
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"Received: {response}")
    except Exception as e:
        print(f"WebSocket FAILED: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())