# streaming_server.py
import os
import asyncio
import json
import logging
import uuid
from collections import deque
from pathlib import Path

import websockets
import redis

from tasks import process_audio  # celery task - call .delay(...)
from gpt_client import call_gpt
from json_loader import sara_store

# Logging
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("streaming_server")

# Configs
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL)

PORT = int(os.getenv("PORT", os.getenv("STREAMING_SERVER_PORT", "8765")))
HOST = "0.0.0.0"

AUDIO_LIST_PREFIX = "sara:audio:"     # redis list per session: sara:audio:{session}
META_PREFIX = "sara:meta:"            # redis hash per session meta
CALLMAP_KEY = "sara:callmap:"         # hash mapping CallSid -> session

def audio_list_key(session_id: str) -> str:
    return AUDIO_LIST_PREFIX + session_id

def meta_key(session_id: str) -> str:
    return META_PREFIX + session_id

def set_meta(session_id: str, data: dict):
    try:
        redis_conn.hset(meta_key(session_id), mapping=data)
    except Exception:
        LOG.exception("failed to set meta for %s", session_id)

async def handler(ws, path):
    session = str(uuid.uuid4())
    LOG.info("ws connected session %s path %s", session, path)
    buffer = deque(maxlen=600)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            ev = msg.get("event")
            if ev == "start":
                start = msg.get("start", {}) or {}
                # Twilio may send callSid in different fields depending on stream version
                call_sid = start.get("callSid") or start.get("call_sid") or start.get("sessionId") or start.get("session_id")
                if call_sid:
                    try:
                        # record mapping CallSid -> our generated session id
                        redis_conn.hset(CALLMAP_KEY, call_sid, session)
                        LOG.info("mapped callSid %s -> session %s", call_sid, session)
                    except Exception:
                        LOG.exception("failed to store callmap for %s -> %s", call_sid, session)
                set_meta(session, {"call_sid": call_sid or "", "started_at": start.get("timestamp", "")})
                LOG.info("stream start: session=%s call_sid=%s", session, call_sid)
            elif ev == "media":
                media = msg.get("media", {}) or {}
                payload = media.get("payload")
                ts = msg.get("timestamp", "")
                if payload:
                    # each payload is base64 PCM chunk; push JSON to Redis list
                    try:
                        redis_conn.rpush(audio_list_key(session), json.dumps({"ts": ts, "b64": payload}))
                    except Exception:
                        LOG.exception("failed to push audio chunk to redis for session %s", session)
                    buffer.append(payload)
                    # Do a quick intent check every N frames to decide if heavy processing is needed
                    if len(buffer) >= 12:
                        buffer.clear()
                        # Use short system prompt if present to keep latency low
                        if isinstance(sara_store.system_prompt, dict):
                            prompt_short = sara_store.system_prompt.get("realtime_short") or sara_store.system_prompt.get("text", "")
                        else:
                            prompt_short = str(sara_store.system_prompt or "")
                        placeholder_transcript = "[short audio segment captured]"
                        prompt = f"{prompt_short}\nTranscript: {placeholder_transcript}\nReturn strict JSON with keys 'intent' and 'action'."
                        try:
                            resp = call_gpt(prompt, max_tokens=48, temperature=0.0)
                            LOG.info("quick_gpt session=%s resp=%s", session, str(resp)[:200])
                            resp_text = json.dumps(resp).lower() if resp else ""
                            if "process_audio" in resp_text or "summary" in resp_text or "intent" in resp_text:
                                # schedule background processing to do heavy ASR/GPT and TTS
                                process_audio.delay(session, do_asr=False)
                                LOG.info("scheduled process_audio for session %s", session)
                        except Exception:
                            LOG.exception("quick_gpt failed for session %s", session)
            elif ev == "stop":
                LOG.info("stream stop for session %s", session)
                # schedule final processing; enable ASR if configured
                enable_asr = os.environ.get("ENABLE_ASR", "false").lower() == "true"
                process_audio.delay(session, do_asr=enable_asr)
                break
    except websockets.exceptions.ConnectionClosed:
        LOG.info("ws connection closed for session %s", session)
    except Exception:
        LOG.exception("ws handler error for session %s", session)
    finally:
        # optional cleanup hooks could go here
        LOG.info("cleanup complete for session %s", session)

if __name__ == "__main__":
    LOG.info("Starting streaming server on %s:%s", HOST, PORT)

    async def process_request(path, request_headers):
        try:
            debug_info = {
                "path": path,
                "Host": request_headers.get("Host"),
                "Upgrade": request_headers.get("Upgrade"),
                "Connection": request_headers.get("Connection"),
                "Sec-WebSocket-Key": request_headers.get("Sec-WebSocket-Key"),
                "Sec-WebSocket-Version": request_headers.get("Sec-WebSocket-Version"),
                "Sec-WebSocket-Protocol": request_headers.get("Sec-WebSocket-Protocol"),
                "User-Agent": request_headers.get("User-Agent"),
                "Origin": request_headers.get("Origin"),
            }
            LOG.info("ws-handshake attempt: %s", json.dumps(debug_info))
        except Exception:
            LOG.exception("failed to log handshake headers")

        if path in ("/", "/health"):
            body = b"ok"
            headers = [
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
            ]
            return 200, headers, body
        return None

    # NOTE: no explicit subprotocols list here — we accept whatever Twilio requests.
    start_server = websockets.serve(
        handler,
        HOST,
        PORT,
        process_request=process_request,
        max_size=2**20,
        max_queue=64,
        origins=None,
    )

    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()



