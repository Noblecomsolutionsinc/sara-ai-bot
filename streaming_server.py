# streaming_server.py
"""
Async Streaming Server for Twilio Media Streams

- Runs a WebSocket server (websockets) that responds to Twilio Media Streams.
- Provides a /health HTTP response via process_request so Render health checks succeed.
- Uses OpenAI Whisper for STT, OpenAI ChatCompletions for GPT replies,
  ElevenLabs for TTS (saved to static/tts/), and Redis for per-call state.

Notes:
- Requires ffmpeg installed in PATH.
- Uses redis.asyncio for async Redis ops.
"""

import os
import asyncio
import json
import base64
import tempfile
import uuid
import time
import logging
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import httpx
import websockets
from websockets.http import Headers
import redis.asyncio as aioredis
import shutil
from asyncio import Lock

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("streaming_server")

# ---------- Config from ENV ----------
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", os.environ.get("STREAMING_SERVER_PORT", 8765)))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_TRANSCRIBE_URL = os.getenv("OPENAI_TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", os.getenv("GPT_MODEL", "gpt-5-mini-2025-08-07"))  # change if needed
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "512"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_TTS_TIMEOUT = int(os.getenv("ELEVENLABS_TTS_TIMEOUT", "120"))

PUBLIC_STREAMING_URL = os.getenv("PUBLIC_STREAMING_URL")  # e.g. https://sara-ai-streaming.onrender.com

# STT / buffering tuning
STT_CHUNK_COUNT = int(os.getenv("STT_CHUNK_COUNT", "8"))  # how many Twilio media frames to accumulate
STT_DEBOUNCE_SECONDS = float(os.getenv("STT_DEBOUNCE_SECONDS", "0.8"))

# Local storage for TTS
STATIC_TTS_DIR = Path("static/tts")
STATIC_TTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Async clients & redis ----------
redis = aioredis.from_url(REDIS_URL, decode_responses=True)
httpx_client = httpx.AsyncClient(timeout=OPENAI_TIMEOUT + 10)  # shared client

# per-call locks to avoid concurrent STT/GPT runs
call_locks: Dict[str, Lock] = {}

# ---------- Helpers ----------


async def write_raw_chunks_to_wav(chunks_b64: list, sample_rate: int = 8000) -> Optional[str]:
    """
    Write base64 raw PCM chunks to a temp raw file, use ffmpeg to convert to wav,
    return path to wav file (caller must remove it eventually) or None on failure.
    Assumes Twilio sends s16le mono at sample_rate (often 8000).
    """
    try:
        raw_fd, raw_path = tempfile.mkstemp(suffix=".raw")
        os.close(raw_fd)
        with open(raw_path, "wb") as f:
            for b64 in chunks_b64:
                try:
                    f.write(base64.b64decode(b64))
                except Exception as e:
                    logger.warning("Failed to decode chunk: %s", e)

        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(wav_fd)

        # Run ffmpeg asynchronously to convert raw s16le mono to wav
        # Command: ffmpeg -f s16le -ar {sample_rate} -ac 1 -i raw_path -ar 16000 -ac 1 wav_path -y
        cmd = [
            "ffmpeg",
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", "1",
            "-i", raw_path,
            "-ar", "16000",
            "-ac", "1",
            wav_path,
            "-y",
            "-loglevel",
            "error",
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("ffmpeg conversion failed: %s", stderr.decode(errors="ignore"))
            try:
                os.remove(raw_path)
                os.remove(wav_path)
            except Exception:
                pass
            return None

        # cleanup raw file
        try:
            os.remove(raw_path)
        except Exception:
            pass

        return wav_path
    except Exception as e:
        logger.exception("write_raw_chunks_to_wav error: %s", e)
        return None


async def transcribe_wav_with_whisper(wav_path: str) -> str:
    """
    Upload wav file to OpenAI Whisper transcription endpoint (async).
    Returns transcribed text or empty string on failure.
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set; cannot transcribe")
        return ""

    try:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        # Use multipart/form upload; httpx requires files to be tuples (name, (filename, content, content_type))
        # But AsyncClient supports `files` param like synchronous client
        with open(wav_path, "rb") as f:
            files = {"file": ("audio.wav", f, "audio/wav")}
            data = {"model": WHISPER_MODEL}
            resp = await httpx_client.post(OPENAI_TRANSCRIBE_URL, headers=headers, files=files, data=data, timeout=OPENAI_TIMEOUT + 10)
        if resp.status_code != 200:
            logger.error("Whisper transcription failed: %s %s", resp.status_code, await resp.aread() if hasattr(resp, "aread") else resp.text)
            return ""
        payload = resp.json()
        text = payload.get("text", "").strip()
        return text
    except Exception as e:
        logger.exception("Whisper transcription exception: %s", e)
        return ""
    finally:
        # cleanup wav_path
        try:
            os.remove(wav_path)
        except Exception:
            pass


async def call_gpt_async(history: list, system_prompt: Optional[str] = None) -> str:
    """
    Call OpenAI Chat Completions (HTTP API) with history.
    history: list of {"role":"user"/"assistant","content":...}
    system_prompt: optional system-level instruction
    Returns assistant text (or fallback).
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set; cannot call GPT")
        return "[System Error: missing OpenAI key]"

    try:
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)

        payload = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "max_completion_tokens": int(OPENAI_MAX_TOKENS),
            "temperature": float(OPENAI_TEMPERATURE),
        }

        resp = await httpx_client.post(OPENAI_API_URL, headers=headers, json=payload, timeout=OPENAI_TIMEOUT + 10)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if choices:
            first = choices[0]
            msg = first.get("message") or {}
            content = msg.get("content") or first.get("text") or first.get("content")
            if isinstance(content, str):
                return content.strip()
        logger.warning("GPT returned no choices: %s", data)
        return "[System Error: GPT returned nothing]"
    except Exception as e:
        logger.exception("call_gpt_async error: %s", e)
        return "[System Error: GPT failure]"


async def tts_elevenlabs_async(text: str, call_sid: str) -> Optional[str]:
    """
    Call ElevenLabs TTS async (httpx) and save mp3 to static/tts.
    Returns public URL to MP3 or None on error.
    """
    if not ELEVENLABS_API_KEY or not PUBLIC_STREAMING_URL:
        logger.error("ELEVENLABS_API_KEY or PUBLIC_STREAMING_URL not set; cannot produce TTS")
        return None

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
        payload = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.7},
        }
        # ElevenLabs may return raw audio bytes - use stream response
        resp = await httpx_client.post(url, headers=headers, json=payload, timeout=ELEVENLABS_TTS_TIMEOUT)
        resp.raise_for_status()
        mp3_bytes = resp.content
        ts = int(time.time())
        filename = f"{call_sid}_{ts}.mp3"
        filepath = STATIC_TTS_DIR / filename
        with open(filepath, "wb") as f:
            f.write(mp3_bytes)
        public_url = PUBLIC_STREAMING_URL.rstrip("/") + f"/static/tts/{filename}"
        return public_url
    except Exception as e:
        logger.exception("ElevenLabs TTS error: %s", e)
        return None


# ---------- WebSocket helper: process HTTP requests (health) ----------
async def process_request(path: str, request_headers: Headers) -> Optional[Tuple[int, dict, bytes]]:
    """
    Called by websockets server before handshake. If path is /health respond 200 with JSON.
    Return tuple (status, headers, body_bytes) to bypass WS handshake.
    """
    if path == "/health":
        body = json.dumps({"status": "ok", "service": "streaming_server"}).encode("utf-8")
        headers = [("Content-Type", "application/json")]
        return 200, headers, body
    # For other paths return None to continue with WebSocket handshake
    return None


# ---------- Per-call processing ----------
async def flush_and_process_audio(call_sid: str):
    """
    Called when buffer reaches threshold or upon debounce. Pops available chunks,
    transcribes, updates history, calls GPT, generates TTS, updates Redis.
    This function is safe to be scheduled multiple times but guarded by per-call lock.
    """
    lock = call_locks.setdefault(call_sid, Lock())
    if lock.locked():
        return  # another worker is processing
    async with lock:
        try:
            buf_key = f"call:{call_sid}:audio_buf"
            chunks = []
            while True:
                b = await redis.lpop(buf_key)
                if b is None:
                    break
                chunks.append(b)
            if not chunks:
                return

            # Write to wav (async)
            wav_path = await write_raw_chunks_to_wav(chunks, sample_rate=int(os.getenv("TWILIO_SAMPLE_RATE", "8000")))
            if not wav_path:
                logger.warning("No wav produced for call %s", call_sid)
                return

            # Transcribe with Whisper
            transcript = await transcribe_wav_with_whisper(wav_path)
            if not transcript:
                logger.info("Empty transcript for call %s", call_sid)
                return

            logger.info("[Twilio->AI] %s: %s", call_sid, transcript)

            # Append transcript to history in Redis
            hist_key = f"call:{call_sid}:history"
            history_json = await redis.get(hist_key) or "[]"
            try:
                history = json.loads(history_json)
            except Exception:
                history = []
            history.append({"role": "user", "content": transcript})

            # Get system prompt (optional)
            system_prompt = await redis.hget(f"call:{call_sid}:meta", "system_prompt") or None

            # Call GPT
            reply = await call_gpt_async(history, system_prompt=system_prompt)
            logger.info("[AI->Twilio] %s -> %s", call_sid, reply)

            # Append assistant reply to history
            history.append({"role": "assistant", "content": reply})
            await redis.set(hist_key, json.dumps(history))

            # Generate TTS
            mp3_url = await tts_elevenlabs_async(reply, call_sid)
            if mp3_url:
                await redis.hset(f"call:{call_sid}:meta", mapping={"last_tts_url": mp3_url})
                logger.info("[AI->MP3] %s saved %s", call_sid, mp3_url)
            else:
                logger.warning("TTS generation failed for %s", call_sid)

        except Exception as e:
            logger.exception("flush_and_process_audio error for %s: %s", call_sid, e)


# ---------- WebSocket handler ----------
async def ws_handler(websocket: websockets.WebSocketServerProtocol, path: str):
    logger.info("New WS connection path=%s", path)
    call_sid: Optional[str] = None
    # We'll track last media time for debounce logic
    last_media_ts = 0.0

    try:
        async for raw_msg in websocket:
            # Some clients may open a connection but send non-JSON (health probes) - ignore
            try:
                event = json.loads(raw_msg)
            except Exception:
                logger.debug("Received non-JSON message; ignoring")
                continue

            evt_type = event.get("event")
            # Twilio often sends 'start' with streamSid and callSid info
            if evt_type == "start" or evt_type == "connected":
                call_sid = event.get("streamSid") or event.get("callSid") or event.get("session") or call_sid
                logger.info("[Twilio] Call started: %s", call_sid)
                # Initialize meta in Redis
                await redis.hset(f"call:{call_sid}:meta", mapping={
                    "status": "started",
                    "playback_active": "0",
                    "request_playback_stop": "0",
                })
                # If Twilio provided query params in /voice webhook we expect app.py to set system_prompt in Redis
                # (app.py should set call:{call_sid}:meta.system_prompt prior to streaming start)
                continue

            if evt_type == "stop" or evt_type == "disconnected":
                # mark stopped
                call_sid = event.get("streamSid") or call_sid
                logger.info("[Twilio] Call stopped: %s", call_sid)
                if call_sid:
                    await redis.hset(f"call:{call_sid}:meta", mapping={"status": "stopped", "playback_active": "0"})
                    # optionally cleanup buffers
                    await redis.delete(f"call:{call_sid}:audio_buf")
                continue

            if evt_type == "media":
                call_sid = event.get("streamSid") or event.get("callSid") or call_sid
                media = event.get("media", {})
                payload_b64 = media.get("payload")
                if not payload_b64:
                    continue

                # store payload into redis list for this call
                await redis.rpush(f"call:{call_sid}:audio_buf", payload_b64)
                last_media_ts = time.time()

                # If playback is active, user speaking => barge-in: request stop
                meta = await redis.hgetall(f"call:{call_sid}:meta") or {}
                if meta.get("playback_active") == "1":
                    logger.info("BARGE-IN detected for %s; requesting playback stop", call_sid)
                    await redis.hset(f"call:{call_sid}:meta", "request_playback_stop", "1")

                # If enough chunks, trigger processing (do not await here)
                buf_len = await redis.llen(f"call:{call_sid}:audio_buf")
                if buf_len >= STT_CHUNK_COUNT:
                    # schedule processing in background
                    asyncio.create_task(flush_and_process_audio(call_sid))
                else:
                    # schedule a debounce flush after STT_DEBOUNCE_SECONDS of silence
                    async def debounce(fl_call_sid: str, prev_ts: float):
                        await asyncio.sleep(STT_DEBOUNCE_SECONDS)
                        # if no new media since prev_ts, flush remaining audio
                        new_ts = float(await redis.hget(f"call:{fl_call_sid}:meta", "last_media_ts") or 0.0)
                        # Note: we store last_media_ts below for visibility
                        if time.time() - prev_ts >= STT_DEBOUNCE_SECONDS:
                            await flush_and_process_audio(fl_call_sid)

                    # store last_media_ts
                    await redis.hset(f"call:{call_sid}:meta", "last_media_ts", time.time())
                    # schedule debounce
                    asyncio.create_task(debounce(call_sid, time.time()))

            # After handling incoming event, check for playback/stop signals and send commands to Twilio
            if call_sid:
                meta = await redis.hgetall(f"call:{call_sid}:meta") or {}

                # If tasks set request_playback_stop -> instruct Twilio to stop playing
                if meta.get("request_playback_stop") == "1":
                    try:
                        stop_msg = {"event": "stop", "streamSid": call_sid}
                        await websocket.send(json.dumps(stop_msg))
                        logger.info("Sent stop to Twilio for %s", call_sid)
                    except Exception:
                        logger.exception("Failed to send stop message for %s", call_sid)
                    # clear flags
                    await redis.hset(f"call:{call_sid}:meta", mapping={"request_playback_stop": "0", "playback_active": "0"})

                # If new TTS available and not currently playing -> send play
                last_tts_url = meta.get("last_tts_url") or ""
                playback_active = meta.get("playback_active") == "1"
                if last_tts_url and last_tts_url.strip() and not playback_active:
                    # send play event to Twilio Media Streams
                    play_msg = {"event": "play", "streamSid": call_sid, "media": {"url": last_tts_url}}
                    try:
                        await websocket.send(json.dumps(play_msg))
                        logger.info("[AI->Twilio] Playing response for %s -> %s", call_sid, last_tts_url)
                        # mark as playback active
                        await redis.hset(f"call:{call_sid}:meta", mapping={"playback_active": "1", "last_tts_url": ""})
                    except Exception:
                        logger.exception("Failed to send play message for %s", call_sid)

    except websockets.exceptions.ConnectionClosed as e:
        logger.info("WebSocket closed for call %s: %s", call_sid, e)
    except Exception as e:
        logger.exception("Unhandled error in ws_handler: %s", e)
    finally:
        if call_sid:
            try:
                await redis.hset(f"call:{call_sid}:meta", "playback_active", "0")
            except Exception:
                pass
        logger.info("Connection cleanup done for %s", call_sid)


# ---------- Start server ----------
async def main():
    logger.info("Starting streaming server on ws://%s:%s", HOST, PORT)
    async with websockets.serve(ws_handler, HOST, PORT, process_request=process_request, ping_interval=20, ping_timeout=20):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down streaming server")
