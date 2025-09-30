# test_stream_full.py
"""
Sends start -> tiny media -> stop to the streaming server and prints any replies.
Run: python test_stream_full.py
"""
import asyncio, base64, json, time, sys
import websockets

STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"  # your streaming URL
TIMEOUT = 12  # seconds to wait for responses after stop

async def run_test():
    print("Connecting to", STREAM_URL)
    try:
        async with websockets.connect(STREAM_URL, ping_interval=10, close_timeout=5) as ws:
            print("CONNECTED ✅")
            # 1) send start (Twilio-style)
            start = {"event":"start","start":{"callSid":"TEST123","sample_rate":8000}}
            await ws.send(json.dumps(start))
            print("sent: start")

            # 2) send a small 'media' payload: 160 bytes of zeros -> base64
            payload = base64.b64encode(b"\x00" * 160).decode("ascii")
            media = {"event":"media","media":{"payload": payload}}
            await ws.send(json.dumps(media))
            print("sent: media (160 bytes)")

            # 3) wait a short moment and send stop
            await asyncio.sleep(0.5)
            await ws.send(json.dumps({"event":"stop"}))
            print("sent: stop")

            # 4) collect messages from server for TIMEOUT seconds
            print(f"Waiting up to {TIMEOUT}s for server responses...")
            end = time.time() + TIMEOUT
            got_any = False
            while time.time() < end:
                try:
                    # use small recv timeout to allow periodic checks
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    got_any = True
                    print("RECV:", msg[:2000])
                except asyncio.TimeoutError:
                    # no message in this interval, keep waiting until overall timeout
                    continue
                except websockets.ConnectionClosed as e:
                    print("Connection closed by server:", e)
                    break

            if not got_any:
                print("No server messages received during wait period.")
            print("Done.")
            return 0
    except Exception as e:
        print("ERROR connecting or sending:", repr(e))
        return 2

if __name__ == "__main__":
    code = asyncio.run(run_test())
    sys.exit(code)
