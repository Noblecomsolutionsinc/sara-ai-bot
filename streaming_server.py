#!/usr/bin/env python3
"""
streaming_server.py

aiohttp WebSocket server for Twilio Media Streams.

Routes:
  - GET /ws      -> Twilio Media Streams connect here (wss://host/ws)
  - GET /healthz -> returns {"status": "ok"} and enqueues a Celery ping

Behavior:
  - Validates Twilio signature unless SKIP_TWILIO_VALIDATION env var is set
  - For incoming JSON events, determines callSid and enqueues process_event.delay(event)
  - Checks Redis meta for playback flags and sends control messages to Twilio via WebSocket
"""
from __future__ import annotations

import os
import json
import logging
import asyncio
import time
from typing import Optional

from aiohttp import web, WSMsgType
import redis

from tasks import process_event  # tasks will import celery_app.worker internally

# Twilio validator optional import
try:
    from twilio.request_validator import RequestValidator  # type: ignore
    TWILIO_VALIDATOR_AVAILABLE = True
except Exception:
    RequestValidator = None
    TWILIO_VALIDATOR_AVAILABLE = False

# Config
HOST = "0.0.0.0"
PORT = int(os.getenv("STREAMING_PORT", os.getenv("PORT", "8765")))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
SKIP_TWILIO_VALIDATION = os.getenv("SKIP_TWILIO_VALIDATION", "false").lower() in ("1", "true", "yes")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("streaming_server")

# Redis
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Twilio validator
TWILIO_VALIDATOR = None
if not SKIP_TWILIO_VALIDATION and TWILIO_VALIDATOR_AVAILABLE and TWILIO_AUTH_TOKEN:
    TWILIO_VALIDATOR = RequestValidator(TWILIO_AUTH_TOKEN)
    logger.info("Twilio RequestValidator initialized for streaming_server.")
else:
    if SKIP_TWILIO_VALIDATION:
        logger.info("SKIP_TWILIO_VALIDATION set — skipping Twilio signature checks for streaming_server.")
    else:
        logger.info("Twilio RequestValidator not available or auth token missing; signature validation disabled for streaming_server.")


async def health_handler(request: web.Request) -> web.Response:
    """Health endpoint that also enqueues a ping to Celery."""
    try:
        redis_ok = False
        try:
            redis_ok = r.ping()
        except Exception:
            redis_ok = False

        celery_ok = False
        try:
            process_event.delay({"type": "ping", "ts": time.time()})
            celery_ok = True
        except Exception:
            celery_ok = False
            logger.exception("Failed to enqueue ping to Celery")

        return web.json_response({"status": "ok", "redis_ok": bool(redis_ok), "celery_ok": bool(celery_ok)})
    except Exception:
        logger.exception("Health check failed")
        return web.json_response({"status": "error"}, status=500)


def _validate_twilio_ws_request(request: web.Request) -> bool:
    if SKIP_TWILIO_VALIDATION:
        return True
    if not TWILIO_VALIDATOR:
        logger.warning("Twilio validator not configured; rejecting WS request")
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    params = dict(request.query)
    try:
        ok = TWILIO_VALIDATOR.validate(url, params, signature)
        if not ok:
            logger.warning("Twilio WS signature validation failed for URL=%s", url)
        return ok
    except Exception:
        logger.exception("Exception in Twilio WS validation")
        return False


async def ws_handler(request: web.Request) -> web.StreamResponse:
    """
    Handle Twilio WS messages. Expect Twilio Media Stream JSON events.
    After enqueuing the event in Celery, examine Redis meta for playback flags and last_tts_url,
    and send appropriate control messages to Twilio over the WebSocket.
    """
    # Validate Twilio signature on WS upgrade (optional)
    if not _validate_twilio_ws_request(request):
        return web.json_response({"error": "invalid_twilio_signature"}, status=403)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    call_sid: Optional[str] = None
    logger.info("WebSocket connection opened from %s", request.remote)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except Exception:
                    logger.warning("Received non-JSON websocket message; ignoring")
                    continue

                # Determine callSid
                call_sid = event.get("callSid") or event.get("streamSid") or event.get("session")
                if not call_sid:
                    logger.debug("Event without callSid; enqueueing event anyway")
                # Enqueue event for processing (non-blocking)
                try:
                    process_event.delay(event)
                except Exception:
                    logger.exception("Failed to enqueue process_event for call %s", call_sid)

                # Playback control logic (read Redis meta)
                if call_sid:
                    meta_key = f"call:{call_sid}:meta"
                    try:
                        meta = r.hgetall(meta_key) or {}
                    except Exception:
                        meta = {}
                        logger.exception("Failed to read meta for %s", call_sid)

                    # Handle playback stop (barge-in)
                    if meta.get("request_playback_stop") == "1":
                        stop_msg = {"event": "stop", "streamSid": call_sid}
                        try:
                            await ws.send_json(stop_msg)
                            logger.info("Sent STOP control for %s", call_sid)
                            # reset flags atomically
                            pipe = r.pipeline()
                            pipe.hset(meta_key, "request_playback_stop", "0")
                            pipe.hset(meta_key, "playback_active", "0")
                            pipe.execute()
                        except Exception:
                            logger.exception("Failed to send STOP or reset playback flags for %s", call_sid)

                    # Handle TTS playback if available and not active
                    last_tts = meta.get("last_tts_url")
                    playback_active = meta.get("playback_active") == "1"
                    if last_tts and not playback_active:
                        play_msg = {"event": "play", "streamSid": call_sid, "media": {"url": last_tts}}
                        try:
                            await ws.send_json(play_msg)
                            logger.info("Sent PLAY for %s -> %s", call_sid, last_tts)
                            pipe = r.pipeline()
                            pipe.hset(meta_key, "playback_active", "1")
                            pipe.hset(meta_key, "last_tts_url", "")
                            pipe.execute()
                        except Exception:
                            logger.exception("Failed to send PLAY or set flags for %s", call_sid)

            elif msg.type == WSMsgType.ERROR:
                logger.error("WebSocket error: %s", ws.exception())

    except Exception:
        logger.exception("Unexpected error in ws_handler for call %s", call_sid)
    finally:
        if call_sid:
            try:
                r.hset(f"call:{call_sid}:meta", "playback_active", "0")
                logger.info("Cleared playback_active for %s on disconnect", call_sid)
            except Exception:
                logger.exception("Failed to clear playback_active for %s", call_sid)

    return ws


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/healthz", health_handler),
        web.get("/ws", ws_handler),
    ])
    return app


if __name__ == "__main__":
    logger.info("Starting streaming_server on %s:%s", HOST, PORT)
    web_app = create_app()
    web.run_app(web_app, host=HOST, port=PORT)
