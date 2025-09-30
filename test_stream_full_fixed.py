# test_stream_full_fixed.py
"""
Full mini-flow test: start -> 1s of silence -> stop.
Checks if the server processes audio and replies.
Run: python test_stream_full_fixed.py
"""

import asyncio, base64, json, time, sys
import websockets

# Your streaming server URL
STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"
TIMEOUT = 20  # seconds to wait for server responses after stop

async def run_test():
    print("Connecting to", STREAM_URL)
    try:
        async with websockets.connect(STREAM_URL, ping_interval=10, close_timeout=5) as ws:
            print("CONNECTED ✅")

            # 1) Send START event (like Twilio)
            start = {"event": "start", "start": {"callSid": "TEST_LONG", "sample_rate": 8000}}
            await ws.send(json.dumps(start))
            print("sent: start")

            # 2) Send 1 second of silence at 8 kHz (16000 samples = 2 bytes each)
            one_sec_pcm = b"\x00" * 16000  # 1 second, 8kHz mono, 16-bit PCM
            payload = base64.b64encode(one_sec_pcm).decode("ascii")
            media = {"event": "media", "media": {"payload": payload}}
            await ws.send(json.dumps(media))
            print("sent: 1 second of silence (16000 bytes)")

            # 3) Send STOP event
            await ws.send(json.dumps({"event": "stop"}))
            print("sent: stop")

            # 4) Collect messages from server
            print(f"Waiting up to {TIMEOUT}s for server responses...")
            end = time.time() + TIMEOUT
            got_any = False
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    got_any = True
                    print("RECV:", msg[:2000])  # print up to 2000 chars
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed as e:
                    print("Connection closed by server:", e)
                    break

            if not got_any:
                print("No server messages received during wait period.")
            print("Done.")
            return 0
    except Exception as e:
        print("ERROR:", repr(e))
        return 2

if __name__ == "__main__":
    sys.exit(asyncio.run(run_test()))
