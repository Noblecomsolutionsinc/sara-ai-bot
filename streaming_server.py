"""
streaming_server.py

Unified aiohttp server handling:
- Twilio Media Stream WebSocket events
- Redis-driven TTS playback & barge-in
- Health check endpoint
"""

import os
import json
import logging
import redis
from aiohttp import web, WSMsgType
from tasks import handle_twilio_event  # ensure tasks.py exists

logger = logging.getLogger("streaming_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Config
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8765"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


async def health_handler(request):
    """Health check for Render"""
    return web.json_response({"status": "ok", "service": "streaming_server"})


async def ws_handler(request):
    """Handle Twilio Media Stream WebSocket"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    call_sid = None
    logger.info("New WebSocket connection established")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except Exception:
                    logger.warning("Non-JSON message received; skipping")
                    continue

                # Extract stream/call identifier
                call_sid = (
                    event.get("streamSid")
                    or event.get("callSid")
                    or event.get("session")
                    or call_sid
                )

                # Pass event into tasks.py
                try:
                    handle_twilio_event(event)
                except Exception:
                    logger.exception("Failed to handle Twilio event")

                # Redis playback logic
                if call_sid:
                    meta = r.hgetall(f"call:{call_sid}:meta") or {}

                    # Handle barge-in stop
                    if meta.get("request_playback_stop") == "1":
                        stop_msg = {"event": "stop", "streamSid": call_sid}
                        await ws.send_json(stop_msg)
                        logger.info("Sent STOP for call %s", call_sid)
                        r.hset(f"call:{call_sid}:meta", "request_playback_stop", "0")
                        r.hset(f"call:{call_sid}:meta", "playback_active", "0")

                    # Handle TTS playback
                    last_tts = meta.get("last_tts_url")
                    playback_active = meta.get("playback_active") == "1"
                    if last_tts and not playback_active:
                        play_msg = {
                            "event": "play",
                            "streamSid": call_sid,
                            "media": {"url": last_tts},
                        }
                        await ws.send_json(play_msg)
                        logger.info("Sent PLAY to call %s -> %s", call_sid, last_tts)
                        r.hset(f"call:{call_sid}:meta", "playback_active", "1")
                        r.hset(f"call:{call_sid}:meta", "last_tts_url", "")

            elif msg.type == WSMsgType.ERROR:
                logger.error("WebSocket error: %s", ws.exception())

    except Exception as e:
        logger.exception("WS handler error: %s", e)
    finally:
        if call_sid:
            r.hset(f"call:{call_sid}:meta", "playback_active", "0")
            logger.info("Cleaned playback_active for %s", call_sid)

    return ws


def create_app():
    """Build aiohttp app"""
    app = web.Application()
    app.add_routes([
        web.get("/healthz", health_handler),
        web.get("/media", ws_handler),
    ])
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host=HOST, port=PORT)
