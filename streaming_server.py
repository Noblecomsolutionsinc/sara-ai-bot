# streaming_server.py
"""
Production-ready WebSocket streaming server for Sara AI (outbound cold caller).
Receives Twilio Media Stream events (start, media, stop).
Stores audio chunks in Redis per-session.
Performs quick GPT intent checks (non-blocking).
Schedules Celery tasks for heavier processing (ASR + GPT + TTS).
Sends ack on 'start' so diagnostics know the session_id.
"""

from __future__ import annotations

import os
import json
import uuid
import logging
import asyncio
from collections import deque
from functools import partial
from typing import Any, Dict, Optional

import redis
import websockets

# Local imports (must exist)
from gpt_client import call_gpt        # robust GPT wrapper
from json_loader import sara_store     # persona & prompts
from tasks import process_audio        # Celery task (process_audio)

# -------------------- Config --------------------
LOG = logging.getLogger("sara.streaming")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
LOG.addHandler(handler)

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", os.getenv("STREAMING_SERVER_PORT", "8765")))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL, decode_responses=True)

AUDIO_LIST_PREFIX = "sara:audio:"
META_PREFIX = "sara:meta:"
CALLMAP_KEY = "sara:callmap:"    # hash mapping CallSid -> session_id
RESULT_PREFIX = "sara:result:"

# Tuneables for snappy behavior
IN_MEMORY_BUFFER_MAX = int(os.getenv("IN_MEMORY_BUFFER_MAX", "600"))
QUICK_GPT_FRAME_THRESHOLD = int(os.getenv("QUICK_GPT_FRAME_THRESHOLD", "10"))
MAX_REDIS_AUDIO_CHUNKS = int(os.getenv("MAX_REDIS_AUDIO_CHUNKS", "3000"))

ALLOWED_WS_PATHS = {"/", "/health", "/ws", "/media", "/stream"}
ALLOW_ALL_PATHS = os.getenv("ALLOW_ALL_PATHS", "false").lower() == "true"

ORIGIN_ALLOWLIST = [o.strip() for o in os.getenv("ORIGIN_ALLOWLIST", "").split(",") if o.strip()]

# -------------------- Helpers --------------------
def audio_list_key(session_id: str) -> str:
    return AUDIO_LIST_PREFIX + session_id

def meta_key(session_id: str) -> str:
    return META_PREFIX + session_id

def result_key(session_id: str) -> str:
    return RESULT_PREFIX + session_id

def is_path_allowed(path: str) -> bool:
    if ALLOW_ALL_PATHS:
        return True
    if path in ALLOWED_WS_PATHS:
        return True
    for p in ("/ws", "/media", "/stream"):
        if path.startswith(p):
            return True
    return False

def is_origin_allowed(origin: Optional[str]) -> bool:
    if not ORIGIN_ALLOWLIST:
        return True
    if not origin:
        return False
    return origin in ORIGIN_ALLOWLIST

# -------------------- Async Redis wrappers --------------------
async def async_redis_hset(key: str, mapping: Dict[str, Any]) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(redis_conn.hset, key, mapping))

async def async_redis_hset_field(key: str, field: str, value: Any) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(redis_conn.hset, key, field, value))

async def async_redis_rpush_trim(key: str, value: str) -> None:
    loop = asyncio.get_running_loop()
    def _push_and_trim():
        redis_conn.rpush(key, value)
        redis_conn.ltrim(key, -MAX_REDIS_AUDIO_CHUNKS, -1)
    await loop.run_in_executor(None, _push_and_trim)

async def async_redis_get_all_hash(key: str) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(redis_conn.hgetall, key))

# -------------------- Pre-handshake --------------------
async def process_request(path: str, request_headers) -> Optional[tuple]:
    try:
        LOG.debug("Handshake headers for path %s", path)
    except Exception:
        LOG.exception("Failed to inspect handshake headers")

    if path in ("/", "/health"):
        body = b"ok"
        headers = [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        return 200, headers, body

    if not is_path_allowed(path):
        body = b"Bad Request - Invalid WS path"
        headers = [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        LOG.warning("Rejecting handshake for disallowed path: %s", path)
        return 400, headers, body

    origin = request_headers.get("Origin")
    if not is_origin_allowed(origin):
        body = b"Forbidden - origin not allowed"
        headers = [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        LOG.warning("Rejecting handshake due to origin: %s", origin)
        return 403, headers, body

    return None

# -------------------- Core handler --------------------
async def handler(ws: websockets.WebSocketServerProtocol, path: str):
    session_id = str(uuid.uuid4())
    LOG.info("New WS connection: session=%s path=%s peer=%s", session_id, path, getattr(ws, "remote_address", None))

    buffer = deque(maxlen=IN_MEMORY_BUFFER_MAX)

    try:
        async for raw in ws:
            if not raw:
                continue

            try:
                msg = json.loads(raw)
            except Exception:
                LOG.debug("Skipping non-json WS message for session %s", session_id)
                continue

            event = msg.get("event")
            # START
            if event == "start":
                start_info = msg.get("start", {}) or {}
                call_sid = start_info.get("callSid") or start_info.get("call_sid") or start_info.get("sessionId") or start_info.get("session_id") or ""
                try:
                    if call_sid:
                        await asyncio.get_running_loop().run_in_executor(None, lambda: redis_conn.hset(CALLMAP_KEY, call_sid, session_id))
                    await async_redis_hset(meta_key(session_id), {"call_sid": call_sid or "", "started_at": start_info.get("timestamp", "")})
                    LOG.info("Stream started: session=%s call_sid=%s", session_id, call_sid)
                except Exception:
                    LOG.exception("Failed to store start meta for session %s", session_id)

                try:
                    ack = {"event": "ack", "status": "ok", "session_id": session_id}
                    await ws.send(json.dumps(ack))
                except Exception:
                    LOG.exception("Failed to send ack for session %s", session_id)

            # MEDIA
            elif event == "media":
                media = msg.get("media") or {}
                payload = media.get("payload")
                ts = msg.get("timestamp", "")
                if payload:
                    try:
                        await async_redis_rpush_trim(audio_list_key(session_id), json.dumps({"ts": ts, "b64": payload}))
                    except Exception:
                        LOG.exception("Failed to push audio chunk to redis for session %s", session_id)

                    buffer.append(payload)
                    if len(buffer) >= QUICK_GPT_FRAME_THRESHOLD:
                        buffer.clear()
                        try:
                            if isinstance(sara_store.system_prompt, dict):
                                prompt_short = sara_store.system_prompt.get("realtime_short", "") or ""
                            else:
                                prompt_short = str(sara_store.system_prompt or "")
                        except Exception:
                            LOG.exception("Unable to read realtime prompt")
                            prompt_short = ""

                        placeholder_transcript = "[short audio captured]"
                        quick_prompt = f"{prompt_short}\nTranscript: {placeholder_transcript}\nReturn strict JSON with keys 'intent' and 'action'."

                        loop = asyncio.get_running_loop()
                        try:
                            resp = await loop.run_in_executor(None, partial(call_gpt, quick_prompt, 48, 1.0))
                            LOG.info("Quick GPT session=%s preview=%s", session_id, str(resp)[:200])
                            resp_text = ""
                            try:
                                resp_text = json.dumps(resp).lower() if resp is not None else ""
                            except Exception:
                                resp_text = str(resp).lower() if resp else ""
                            if any(k in resp_text for k in ("process_audio", "speak", "says", "summary", "intent")):
                                try:
                                    process_audio.delay(session_id, do_asr=False)
                                    LOG.info("Scheduled quick process_audio for session %s", session_id)
                                except Exception:
                                    LOG.exception("Failed to schedule quick process_audio for session %s", session_id)
                        except Exception:
                            LOG.exception("Quick GPT check failed for session %s", session_id)

            # STOP
            elif event == "stop":
                LOG.info("Stream stop for session %s", session_id)
                enable_asr = os.environ.get("ENABLE_ASR", "false").lower() == "true"
                try:
                    process_audio.delay(session_id, do_asr=enable_asr)
                    LOG.info("Scheduled final process_audio for session %s (ASR=%s)", session_id, enable_asr)
                except Exception:
                    LOG.exception("Failed to schedule final process_audio for session %s", session_id)
                break

            else:
                LOG.debug("Unhandled event for session %s: %s", session_id, event)

    except websockets.exceptions.ConnectionClosedOK:
        LOG.info("Connection closed gracefully for session %s", session_id)
    except websockets.exceptions.ConnectionClosedError as e:
        LOG.warning("Connection closed with error for session %s: %s", session_id, e)
    except Exception:
        LOG.exception("Unexpected error in handler for session %s", session_id)
    finally:
        LOG.info("Cleanup complete for session %s", session_id)

# -------------------- Server lifecycle --------------------
async def main() -> None:
    LOG.info("Starting Sara AI streaming server on %s:%s (redis=%s)", HOST, PORT, REDIS_URL)
    server = await websockets.serve(
        handler,
        HOST,
        PORT,
        process_request=process_request,
        max_size=2**20,
        max_queue=64,
        origins=None
    )
    try:
        await asyncio.Future()  # run forever
    finally:
        LOG.info("Shutting down streaming server")
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOG.info("Interrupted, shutting down")
    except Exception:
        LOG.exception("Fatal error in streaming server")
