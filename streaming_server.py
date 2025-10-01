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
from tasks import process_audio
from gpt_client import call_gpt
from json_loader import sara_store

LOG = logging.getLogger("streaming_server")
logging.basicConfig(level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL)

# Render sets PORT env
PORT = int(os.getenv("PORT", os.getenv("STREAMING_SERVER_PORT", "8765")))
HOST = "0.0.0.0"

AUDIO_LIST_PREFIX = "sara:audio:"  # redis lists
META_PREFIX = "sara:meta:"

def audio_list_key(session_id):
    return AUDIO_LIST_PREFIX + session_id

def meta_key(session_id):
    return META_PREFIX + session_id

def set_meta(session_id, data: dict):
    redis_conn.hset(meta_key(session_id), mapping=data)

async def handler(ws, path):
    session_id = str(uuid.uuid4())
    LOG.info("ws connected session %s path %s", session_id, path)
    buffer = deque(maxlen=600)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            ev = msg.get("event")
            if ev == "start":
                start = msg.get("start", {})
                call_sid = start.get("callSid") or start.get("sessionId")
                set_meta(session_id, {"call_sid": call_sid, "started_at": start.get("timestamp", "")})
                LOG.info("stream start: session=%s call_sid=%s", session_id, call_sid)
            elif ev == "media":
                payload = msg.get("media", {}).get("payload")
                ts = msg.get("timestamp") or ""
                if payload:
                    # push base64 chunk into Redis list as JSON
                    redis_conn.rpush(audio_list_key(session_id), json.dumps({"ts": ts, "b64": payload}))
                    buffer.append(payload)
                    # every N frames run a quick intent check
                    if len(buffer) >= 12:
                        buffer.clear()
                        prompt_short = ""
                        if isinstance(sara_store.system_prompt, dict):
                            prompt_short = sara_store.system_prompt.get("realtime_short", "") or sara_store.system_prompt.get("text", "")
                        else:
                            prompt_short = str(sara_store.system_prompt or "")
                        # we only provide a placeholder transcript to keep latency down
                        placeholder = "[short audio segment captured]"
                        prompt = f"{prompt_short}\nTranscript: {placeholder}\nReturn strict JSON with keys 'intent' and 'action' (action can be 'process_audio' or 'none')."
                        resp = call_gpt(prompt, max_tokens=48, temperature=0.0)
                        LOG.info("quick_gpt session=%s resp=%s", session_id, str(resp)[:200])
                        # Simple heuristic: if model mentions process_audio schedule heavy job
                        if resp and ("process_audio" in json.dumps(resp).lower() or "summary" in json.dumps(resp).lower()):
                            process_audio.delay(session_id, do_asr=False)
                            LOG.info("scheduled process_audio for session %s", session_id)
            elif ev == "stop":
                LOG.info("stream stop for session %s", session_id)
                # schedule final processing
                process_audio.delay(session_id, do_asr=True if os.environ.get("ENABLE_ASR", "false").lower() == "true" else False)
                break
    except websockets.exceptions.ConnectionClosed:
        LOG.info("ws closed for session %s", session_id)
    except Exception:
        LOG.exception("ws handler error for %s", session_id)
    finally:
        LOG.info("cleaning session %s", session_id)
        try:
            # optional cleanup
            pass
        except Exception:
            pass

if __name__ == "__main__":
    LOG.info("Starting streaming server on %s:%s", HOST, PORT)
    start_server = websockets.serve(handler, HOST, PORT, max_size=2**20, max_queue=64)
    asyncio.get_event_loop().run_until_complete(start_server)
    asyncio.get_event_loop().run_forever()
