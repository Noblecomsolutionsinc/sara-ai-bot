# test_stream_0p1s.py
import asyncio, base64, json, time, sys
import websockets

STREAM_URL = "wss://sara-ai-streaming.onrender.com/ws"

async def main():
    print("Connecting to", STREAM_URL)
    try:
        async with websockets.connect(STREAM_URL, ping_interval=10, close_timeout=5) as ws:
            print("CONNECTED")
            await ws.send(json.dumps({"event":"start","start":{"callSid":"TEST_0P1","sample_rate":8000}}))
            print("sent: start")
            # 0.1s @ 8000Hz, 16-bit mono => 8000 samples * 2 bytes * 0.1s = 1600 bytes
            payload = base64.b64encode(b"\x00" * 1600).decode("ascii")
            await ws.send(json.dumps({"event":"media","media":{"payload":payload}}))
            print("sent: media (1600 bytes - ~0.1s)")
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"event":"stop"}))
            print("sent: stop - waiting up to 30s for server replies...")
            # collect responses
            end = time.time() + 30
            got = False
            while time.time() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    print("RECV:", msg[:2000])
                    got = True
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed as e:
                    print("Connection closed by server:", e)
                    break
            if not got:
                print("No server messages received in 30s.")
            return 0
    except Exception as e:
        print("ERROR:", e)
        return 2

if __name__ == "__main__":
    code = asyncio.run(main())
    sys.exit(code)
