# test_stream_long.py
"""
Sends a continuous stream of small media frames for ~8 seconds to force processing.
Run: python test_stream_long.py
"""
import asyncio, base64, json, time, sys
import websockets

STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"
DURATION = 8          # seconds of sending media frames
FRAME_CHUNK = 320     # bytes per media frame (s16le) — small chunks
SLEEP_BETWEEN = 0.02  # seconds between frames
WAIT_AFTER = 60       # seconds to wait for any server replies after stop

async def run_test():
    print("Connecting to", STREAM_URL)
    try:
        async with websockets.connect(STREAM_URL, ping_interval=10, close_timeout=5) as ws:
            print("CONNECTED ✅")
            start = {"event":"start","start":{"callSid":"TEST_LONG","sample_rate":8000}}
            await ws.send(json.dumps(start))
            print("sent: start")

            # send continuous frames for DURATION seconds
            end_time = time.time() + DURATION
            frames = 0
            while time.time() < end_time:
                payload = base64.b64encode(b"\x00" * FRAME_CHUNK).decode("ascii")
                media = {"event":"media","media":{"payload": payload}}
                await ws.send(json.dumps(media))
                frames += 1
                await asyncio.sleep(SLEEP_BETWEEN)
            print(f"sent ~{frames} media frames ({DURATION}s)")

            # send stop
            await ws.send(json.dumps({"event":"stop"}))
            print("sent: stop — now waiting up to", WAIT_AFTER, "s for server responses...")

            got_any = False
            timeout_end = time.time() + WAIT_AFTER
            while time.time() < timeout_end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    got_any = True
                    print("RECV:", msg[:4000])
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
        print("ERROR connecting or sending:", repr(e))
        return 2

if __name__ == "__main__":
    code = asyncio.run(run_test())
    sys.exit(code)
