# streaming_server.py
"""
Production WebSocket streaming server for Sara AI outbound cold-callers.
- Accepts Twilio Media Stream events: start, media, stop
- Stores base64 audio chunks in Redis per session
- Performs non-blocking quick GPT checks on small buffers
- Schedules Celery background processing (process_audio) for TTS/ASR/GPT
- Returns an ack JSON on 'start' so diagnostics see the session_id
"""

from __future__ import annotations

import os
import json
import uuid
import logging
import asyncio
from collections import deque
from functools import partial

import redis
import websockets

from gpt_client import generate_reply as call_gpt
from json_loader import sara_store
from tasks import process_audio

# Logging
LOG = logging.getLogger("streaming_server")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))
h = logging.StreamHandler()
h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
LOG.addHandler(h)

# Config
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", os.getenv("STREAMING_SERVER_PORT", "8765")))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL, decode_responses=True)

AUDIO_LIST_PREFIX = "sara:audio:"
META_PREFIX = "sara:meta:"
CALLMAP_KEY = "sara:callmap:"

IN_MEMORY_BUFFER_MAX = int(os.getenv("IN_MEMORY_BUFFER_MAX", "600"))
QUICK_GPT_FRAME_THRESHOLD = int(os.getenv("QUICK_GPT_FRAME_THRESHOLD", "10"))
MAX_REDIS_AUDIO_CHUNKS = int(os.getenv("MAX_REDIS_AUDIO_CHUNKS", "3000"))

ALLOWED_WS_PATHS = {"/", "/health", "/ws", "/media", "/stream"}
ALLOW_ALL_PATHS = os.getenv("ALLOW_ALL_PATHS", "false").lower() == "true"

def audio_list_key(session_id: str) -> str:
    return f"{AUDIO_LIST_PREFIX}{session_id}"

def meta_key(session_id: str) -> str:
    return f"{META_PREFIX}{session_id}"

async def async_redis_rpush_trim(key: str, value: str) -> None:
    loop = asyncio.get_running_loop()
    def op():
        redis_conn.rpush(key, value)
        redis_conn.ltrim(key, -MAX_REDIS_AUDIO_CHUNKS, -1)
    await loop.run_in_executor(None, op)

async def async_redis_hset(key: str, mapping):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, partial(redis_conn.hset, key, mapping=mapping))

async def process_request(path, request_headers):
    try:
        LOG.debug("Handshake headers path=%s headers=%s", path, dict(request_headers))
    except Exception:
        LOG.exception("Failed reading handshake headers")

    # health
    if path in ("/", "/health"):
        body = b"ok"
        headers = [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        return 200, headers, body

    if not (ALLOW_ALL_PATHS or path in ALLOWED_WS_PATHS or any(path.startswith(p) for p in ("/ws", "/media", "/stream"))):
        body = b"Bad Request - Invalid WS path"
        headers = [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        LOG.warning("Rejecting handshake for path %s", path)
        return 400, headers, body

    return None

async def handler(ws: websockets.WebSocketServerProtocol, path: str):
    session_id = str(uuid.uuid4())
    LOG.info("WS connected session=%s path=%s peer=%s", session_id, path, getattr(ws, "remote_address", None))

    buffer = deque(maxlen=IN_MEMORY_BUFFER_MAX)

    try:
        async for raw in ws:
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                LOG.debug("Non-JSON message received for session %s", session_id)
                continue

            event = msg.get("event")
            # START
            if event == "start":
                start = msg.get("start", {}) or {}
                call_sid = start.get("callSid") or start.get("call_sid") or start.get("sessionId") or start.get("session_id") or ""
                try:
                    if call_sid:
                        # store CallSid -> session mapping
                        await asyncio.get_running_loop().run_in_executor(None, lambda: redis_conn.hset(CALLMAP_KEY, call_sid, session_id))
                    await async_redis_hset(meta_key(session_id), {"call_sid": call_sid or "", "started_at": start.get("timestamp", "")})
                    LOG.info("Stream start session=%s call_sid=%s", session_id, call_sid)
                except Exception:
                    LOG.exception("Failed to save start meta for %s", session_id)

                # ack back
                try:
                    await ws.send(json.dumps({"event": "ack", "status": "ok", "session_id": session_id}))
                except Exception:
                    LOG.exception("Failed to send ack for %s", session_id)

            # MEDIA
            elif event == "media":
                media = msg.get("media") or {}
                payload = media.get("payload")
                ts = msg.get("timestamp", "")
                if payload:
                    try:
                        await async_redis_rpush_trim(audio_list_key(session_id), json.dumps({"ts": ts, "b64": payload}))
                    except Exception:
                        LOG.exception("Failed to push audio chunk for session %s", session_id)

                    buffer.append(payload)
                    if len(buffer) >= QUICK_GPT_FRAME_THRESHOLD:
                        buffer.clear()
                        try:
                            if isinstance(sara_store.system_prompt, dict):
                                prompt_short = sara_store.system_prompt.get("realtime_short", "") or ""
                            else:
                                prompt_short = str(sara_store.system_prompt or "")
                        except Exception:
                            LOG.exception("Failed to read realtime prompt")
                            prompt_short = ""

                        placeholder_transcript = "[short audio captured]"
                        quick_prompt = f"{prompt_short}\nTranscript: {placeholder_transcript}\nReturn strict JSON with keys 'intent' and 'action'."

                        loop = asyncio.get_running_loop()
                        try:
                            resp = await loop.run_in_executor(None, partial(call_gpt, [{"role":"system","content":prompt_short},{"role":"user","content":placeholder_transcript}], int(os.getenv("QUICK_GPT_TOKENS","48")), float(os.getenv("QUICK_GPT_TEMP","1.0"))))
                            LOG.info("Quick GPT session=%s preview=%s", session_id, str(resp)[:300])
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
                                    LOG.exception("Failed to schedule quick process_audio for %s", session_id)
                        except Exception:
                            LOG.exception("Quick GPT check failed for %s", session_id)

            # STOP
            elif event == "stop":
                LOG.info("Stream stop for session %s", session_id)
                enable_asr = os.environ.get("ENABLE_ASR", "false").lower() == "true"
                try:
                    process_audio.delay(session_id, do_asr=enable_asr)
                except Exception:
                    LOG.exception("Failed to schedule final process_audio for %s", session_id)
                break

            else:
                LOG.debug("Unhandled event for session %s: %s", session_id, event)

    except websockets.exceptions.ConnectionClosedOK:
        LOG.info("Connection closed gracefully for session %s", session_id)
    except websockets.exceptions.ConnectionClosedError as e:
        LOG.warning("Connection closed with error for session %s: %s", session_id, e)
    except Exception:
        LOG.exception("Unexpected handler error for session %s", session_id)
    finally:
        LOG.info("Cleanup complete for session %s", session_id)

async def main():
    LOG.info("Starting streaming server on %s:%s (redis=%s)", HOST, PORT, REDIS_URL)
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
