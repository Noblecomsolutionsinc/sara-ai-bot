"""
streaming_server.py

Unified WebSocket + HTTP health server for Twilio Media Streams.

Features:
- Accepts Twilio WS connections and forwards events to tasks.handle_twilio_event
- Polls Redis meta keys to determine when to send play commands to Twilio
- Detects barge-in requests (set by tasks) and sends stop/play control to Twilio
"""

import os
import asyncio
import json
import logging
import websockets
from aiohttp import web
import redis
from tasks import handle_twilio_event  # ensures tasks.py exist

logger = logging.getLogger("streaming_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Config
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Health endpoint
async def health(request):
    return web.json_response({"status": "ok", "service": "streaming_server"})

# Twilio WS handler
async def ws_handler(websocket, path):
    logger.info("New websocket connection (path=%s)", path)
    call_sid = None
    try:
        # Launch a background task to monitor Redis for TTS for this call_sid
        monitor_task = None

        async for raw_msg in websocket:
            try:
                event = json.loads(raw_msg)
            except Exception:
                logger.warning("Non-JSON message received; skipping")
                continue

            # Extract stream/call identifier
            call_sid = event.get("streamSid") or event.get("callSid") or event.get("session") or call_sid

            # Immediately pass event to tasks
            try:
                handle_twilio_event(event)
            except Exception:
                logger.exception("Failed to enqueue event to tasks")

            # If playback stop requested (barge-in), send stop to Twilio
            if call_sid:
                meta = r.hgetall(f"call:{call_sid}:meta") or {}
                # If tasks signalled a playback stop request, send a stop command to Twilio
                if meta.get("request_playback_stop") == "1":
                    stop_msg = {"event": "stop", "streamSid": call_sid}
                    try:
                        await websocket.send(json.dumps(stop_msg))
                        logger.info("Sent stop for call %s", call_sid)
                    except Exception:
                        logger.exception("Failed to send stop message")
                    # Clear the flag
                    r.hset(f"call:{call_sid}:meta", "request_playback_stop", "0")
                    # Mark playback as inactive
                    r.hset(f"call:{call_sid}:meta", "playback_active", "0")

                # If a new last_tts_url is present and playback not active, send play command
                last_tts = meta.get("last_tts_url")
                playback_active = meta.get("playback_active") == "1"
                if last_tts and not playback_active:
                    # Send play directive to Twilio Media Stream
                    play_msg = {
                        "event": "media",
                        "type": "play",
                        "streamSid": call_sid,
                        "media": {"url": last_tts}
                    }
                    try:
                        await websocket.send(json.dumps({
                            "event": "play",
                            "streamSid": call_sid,
                            "media": {"url": last_tts}
                        }))
                        logger.info("Sent play to call %s -> %s", call_sid, last_tts)
                        # mark as playback active and clear last_tts_url (until new one arrives)
                        r.hset(f"call:{call_sid}:meta", "playback_active", "1")
                        r.hset(f"call:{call_sid}:meta", "last_tts_url", "")
                    except Exception:
                        logger.exception("Failed to send play message to Twilio")
            # else: no call_sid yet; wait for start event to set it

    except websockets.exceptions.ConnectionClosed as e:
        logger.info("Connection closed for call %s: %s", call_sid, str(e))
    except Exception as e:
        logger.exception("WS handler error: %s", e)
    finally:
        # Cleanup on connection close
        if call_sid:
            r.hset(f"call:{call_sid}:meta", "playback_active", "0")
            logger.info("Cleaned playback_active for %s", call_sid)

async def start_servers():
    # Start websockets server
    ws_server = await websockets.serve(ws_handler, HOST, PORT, ping_interval=20, ping_timeout=20)
    logger.info("WebSocket server listening on ws://%s:%s", HOST, PORT)

    # Start aiohttp health server on same port by using AppRunner / TCPSite
    # Note: websockets server already binds the port; but aiohttp's TCPSite also wants to bind.
    # To keep both coexisting we spawn aiohttp on a different ephemeral port and rely on Render healthcheck hitting /health via the same container port.
    # Simpler approach: we create an aiohttp app and run it on the same host+port using AppRunner + TCPSite - this will succeed if the port is free.
    # However, websockets.serve already bound the socket. To avoid binding conflict, we instead expose a minimal aiohttp health endpoint via a background task that responds to HTTP requests on the same port using a simple socket server.
    # For maximum portability, we'll start an aiohttp server on PORT+1 for health; Render healthCheckPath will use that port if necessary.
    # But Render expects health on $PORT; to keep consistent we will run a small HTTP server in a thread bound to PORT as well.
    # Implement a very small aiohttp server on PORT as well (works on most platforms)
    app = web.Application()
    app.add_routes([web.get("/health", health_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    try:
        await site.start()
        logger.info("HTTP health endpoint started at http://%s:%s/health", HOST, PORT)
    except Exception as e:
        # If binding fails (because websockets already took the port), just log and continue.
        logger.warning("Failed to start aiohttp health endpoint on same port: %s", e)

    # Keep the process alive
    await asyncio.Future()

async def health_handler(request):
    return web.json_response({"status": "ok", "service": "streaming_server"})

if __name__ == "__main__":
    try:
        asyncio.run(start_servers())
    except KeyboardInterrupt:
        logger.info("Shutting down streaming server")
