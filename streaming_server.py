# streaming_server.py
"""
Production-ready streaming server for Sara AI (drop-in replacement).
Features:
 - strict env checks
 - persona loader -> SYSTEM_PROMPT
 - per-connection asyncio.Queue for audio chunks
 - controlled pipeline: convert -> whisper -> LLM -> Eleven -> Twilio Play
 - worker semaphore to limit concurrent LLM/TTS
 - mp3/raw cleanup loop
 - fallback TTS for failures
 - graceful shutdown
 - tuned defaults for Render (1CPU, 2GB)
"""

import os
import json
import uuid
import time
import base64
import logging
import pathlib
import subprocess
import asyncio
import signal
from datetime import datetime, timedelta
from aiohttp import web, WSMsgType, ClientSession, FormData
from twilio.rest import Client as TwilioClient

# ---------- Logging ----------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
log = logging.getLogger("sara-streaming-prod")

# ---------- Paths ----------
ROOT = pathlib.Path(".").resolve()
STATIC_DIR = ROOT / "static"
RAW_DIR = ROOT / "raw"
DATA_DIR = ROOT / "data"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Config / Env ----------
REQUIRED = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "PUBLIC_STREAMING_URL"   # e.g. https://sara-ai-streaming.onrender.com
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))

ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
ELEVEN_TIMEOUT = int(os.environ.get("ELEVEN_TIMEOUT", "120"))

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")

PORT = int(os.environ.get("PORT", "6000"))
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "24"))
BUFFER_FLUSH_BYTES = int(os.environ.get("BUFFER_FLUSH_BYTES", str(100 * 1024)))  # flush threshold
PROCESS_SEMAPHORE = int(os.environ.get("PROCESS_SEMAPHORE", "2"))  # concurrent processing limit
MAX_QUEUE_CHUNKS = int(os.environ.get("MAX_QUEUE_CHUNKS", "200"))  # prevent runaway queues
RETRY_COUNT = int(os.environ.get("RETRY_COUNT", "2"))

# Twilio client
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---------- Utilities ----------
def ensure_ffmpeg():
    try:
        p = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise FileNotFoundError
    except Exception:
        log.exception("ffmpeg not found on PATH. Install ffmpeg or use an image that includes it.")
        raise SystemExit("ffmpeg not available")

def convert_raw_to_wav_blocking(raw_path: str, wav_path: str, sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", str(raw_path),
        "-ar", "16000", "-ac", "1",
        str(wav_path)
    ]
    log.info("ffmpeg cmd: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        log.error("ffmpeg failed: %s", proc.stderr.decode(errors="ignore")[:1000])
        raise RuntimeError("ffmpeg conversion failed")
    return wav_path

# ---------- Persona loader ----------
def load_persona(data_dir: pathlib.Path):
    persona = {}
    if not data_dir.exists():
        log.warning("No data/ folder found; continuing without persona files")
        return persona
    for p in sorted(data_dir.glob("*.json")):
        try:
            persona[p.name] = json.loads(p.read_text(encoding="utf-8"))
            log.info("Loaded persona: %s", p.name)
        except Exception:
            log.exception("Failed to load persona file %s", p.name)
    return persona

PERSONA = load_persona(DATA_DIR)

def build_system_prompt(persona_map):
    key = "Sara_SystemPrompt_Production.json"
    if key in persona_map:
        obj = persona_map[key]
        if isinstance(obj, dict) and "prompt" in obj:
            return obj["prompt"]
        return json.dumps(obj, ensure_ascii=False)
    out = {}
    for k, v in persona_map.items():
        try:
            out[k] = v if isinstance(v, (str, int, float)) else "<object>"
        except Exception:
            out[k] = "<error>"
    return "Sara persona summary:\n" + json.dumps(out, ensure_ascii=False)

SYSTEM_PROMPT = build_system_prompt(PERSONA)
log.info("SYSTEM_PROMPT length: %d", len(SYSTEM_PROMPT))

# ---------- HTTP wrappers for OpenAI / Eleven ----------
# Using aiohttp ClientSession inside each call for simplicity (short-lived sessions are fine on Render)
async def transcribe_with_openai(wav_path: str):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = FormData()
    with open(wav_path, "rb") as fh:
        data.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        data.add_field("model", WHISPER_MODEL)
        async with ClientSession() as session:
            try:
                async with session.post(url, headers=headers, data=data, timeout=OPENAI_TIMEOUT) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        log.error("Whisper failed %s: %s", resp.status, txt[:1000])
                        return None
                    j = await resp.json()
                    return j.get("text")
            except asyncio.TimeoutError:
                log.error("Whisper request timed out")
                return None

async def ask_llm(user_text: str):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.2,
        "max_tokens": 450
    }
    async with ClientSession() as session:
        for attempt in range(RETRY_COUNT + 1):
            try:
                async with session.post(url, headers=headers, json=payload, timeout=OPENAI_TIMEOUT) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        log.error("LLM failed %s: %s", resp.status, txt[:1000])
                        await asyncio.sleep(1 + attempt)
                        continue
                    j = await resp.json()
                    try:
                        return j["choices"][0]["message"]["content"].strip()
                    except Exception:
                        log.error("Unexpected LLM response: %s", j)
                        return None
            except asyncio.TimeoutError:
                log.warning("LLM request timed out, attempt %s", attempt)
                await asyncio.sleep(1 + attempt)
    return None

async def eleven_synthesize_mp3(text: str, out_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with ClientSession() as session:
        for attempt in range(RETRY_COUNT + 1):
            try:
                async with session.post(url, headers=headers, json=payload, timeout=ELEVEN_TIMEOUT) as resp:
                    data = await resp.read()
                    if resp.status != 200:
                        log.error("ElevenLabs TTS failed %s: %s", resp.status, data[:1000])
                        await asyncio.sleep(1 + attempt)
                        continue
                    with open(out_path, "wb") as fh:
                        fh.write(data)
                    return out_path
            except asyncio.TimeoutError:
                log.warning("ElevenLabs request timed out (attempt %s)", attempt)
                await asyncio.sleep(1 + attempt)
    return None

# ---------- Twilio blocking play wrapper ----------
def twilio_play_blocking(call_sid: str, public_url: str):
    try:
        log.info("Twilio update: call=%s play=%s", call_sid, public_url)
        twilio_client.calls(call_sid).update(twiml=f"<Response><Play>{public_url}</Play></Response>")
    except Exception:
        log.exception("Twilio update failed for %s", call_sid)
        raise

async def twilio_play(call_sid: str, public_url: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, twilio_play_blocking, call_sid, public_url)

# ---------- Fallback TTS (short polite audio) ----------
async def fallback_tts_save(out_path: str, text: str = "I didn't catch that, please repeat."):
    # Use ElevenLabs for fallback too but keep it tiny and synchronous-friendly
    try:
        return await eleven_synthesize_mp3(text, out_path)
    except Exception:
        log.exception("Fallback TTS failed")
        return None

# ---------- Connection state ----------
# Each connection has: queue (audio chunks), call_sid, sample_rate, last_media_ts, worker_task
CONNS = {}  # ws_id -> dict
PROCESS_SEM = asyncio.Semaphore(PROCESS_SEMAPHORE)

# ---------- Processing pipeline ----------
async def process_queue_loop(ws_id: str):
    meta = CONNS.get(ws_id)
    if not meta:
        return
    q: asyncio.Queue = meta["queue"]
    call_sid = meta.get("call_sid")
    sample_rate = meta.get("sample_rate", 8000)
    while True:
        try:
            # Wait for flush event dict or chunk
            item = await q.get()
            if item is None:
                # None is sentinel to stop processing and flush
                log.debug("Queue sentinel received for %s", ws_id)
                q.task_done()
                break

            # 'item' is a bytes chunk; write to a temp raw file
            stamp = int(time.time()*1000)
            raw_fn = RAW_DIR / f"{ws_id}_{stamp}.s16le"
            wav_fn = RAW_DIR / f"{ws_id}_{stamp}.wav"
            mp3_fn = STATIC_DIR / f"{ws_id}_{stamp}.mp3"

            raw_fn.write_bytes(item)

            # Convert in executor (blocking ffmpeg)
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, convert_raw_to_wav_blocking, str(raw_fn), str(wav_fn), int(sample_rate))
            except Exception:
                log.exception("Conversion failed for %s", ws_id)
                q.task_done()
                continue

            # Transcribe
            transcript = await transcribe_with_openai(str(wav_fn))
            if not transcript:
                log.warning("No transcript for %s", ws_id)
                # produce fallback or continue
                fallback_mp3 = await fallback_tts_save(str(mp3_fn), "I couldn't understand. Please repeat.")
                if fallback_mp3 and call_sid:
                    public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
                    await twilio_play(call_sid, public_url)
                q.task_done()
                continue

            log.info("Transcript (call=%s): %.250s", call_sid or "unknown", transcript)

            # LLM + TTS are rate-limited by PROCESS_SEM to keep memory/CPU under control
            async with PROCESS_SEM:
                reply = await ask_llm(transcript)
                if not reply:
                    log.warning("LLM empty reply for %s", ws_id)
                    fallback_mp3 = await fallback_tts_save(str(mp3_fn), "I'm still processing. Please wait a moment.")
                    if fallback_mp3 and call_sid:
                        public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
                        await twilio_play(call_sid, public_url)
                    q.task_done()
                    continue

                log.info("LLM reply (call=%s): %.250s", call_sid or "unknown", reply)

                out_mp3 = await eleven_synthesize_mp3(reply, str(mp3_fn))
                if not out_mp3:
                    log.warning("TTS failed for %s", ws_id)
                    fallback_mp3 = await fallback_tts_save(str(mp3_fn), "An error occurred. Please hold.")
                    if fallback_mp3 and call_sid:
                        public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
                        await twilio_play(call_sid, public_url)
                    q.task_done()
                    continue

                public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
                if call_sid:
                    try:
                        await twilio_play(call_sid, public_url)
                        log.info("Played mp3 for call %s: %s", call_sid, public_url)
                    except Exception:
                        log.exception("Failed to instruct Twilio to play for %s", call_sid)

            # cleanup small raw/wav files? keep retention loop to remove
            q.task_done()

        except asyncio.CancelledError:
            log.info("Processor task cancelled for %s", ws_id)
            break
        except Exception:
            log.exception("Processing loop exception for %s", ws_id)
            await asyncio.sleep(0.5)

# ---------- Web handlers ----------
routes = web.RouteTableDef()

@routes.get("/health")
async def health(request):
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})

@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse(autoclose=True)
    await ws.prepare(request)
    ws_id = str(uuid.uuid4())

    q = asyncio.Queue(maxsize=MAX_QUEUE_CHUNKS)
    CONNS[ws_id] = {
        "queue": q,
        "call_sid": None,
        "sample_rate": 8000,
        "last_media_ts": time.time(),
        "worker": None
    }
    # start processing worker
    CONNS[ws_id]["worker"] = asyncio.create_task(process_queue_loop(ws_id))
    log.info("WS connection opened %s", ws_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    log.debug("Non-JSON text received: %s", msg.data[:200])
                    continue
                event = j.get("event")
                if event == "start":
                    s = j.get("start", {})
                    CONNS[ws_id]["call_sid"] = s.get("callSid")
                    sr = s.get("sample_rate") or s.get("sampleRate") or 8000
                    CONNS[ws_id]["sample_rate"] = int(sr)
                    CONNS[ws_id]["last_media_ts"] = time.time()
                    log.info("Stream START ws=%s call_sid=%s sample_rate=%s", ws_id, CONNS[ws_id]["call_sid"], sr)

                elif event == "media":
                    media = j.get("media", {})
                    payload = media.get("payload")
                    if not payload:
                        log.debug("media event missing payload")
                        continue
                    try:
                        chunk = base64.b64decode(payload)
                    except Exception:
                        log.exception("Failed to decode payload")
                        continue
                    # push chunk to queue, drop oldest if full (avoid blocking ws)
                    try:
                        CONNS[ws_id]["queue"].put_nowait(chunk)
                    except asyncio.QueueFull:
                        # drop oldest and push new
                        try:
                            _ = CONNS[ws_id]["queue"].get_nowait()
                            CONNS[ws_id]["queue"].task_done()
                            CONNS[ws_id]["queue"].put_nowait(chunk)
                            log.warning("Queue full for %s — dropped oldest chunk", ws_id)
                        except Exception:
                            log.exception("Queue overflow handling failed for %s", ws_id)
                    CONNS[ws_id]["last_media_ts"] = time.time()

                    # if queue reached flush threshold (approx), we let worker process automatically
                elif event == "stop":
                    log.info("Stream STOP ws=%s", ws_id)
                    # signal worker to flush and exit
                    await CONNS[ws_id]["queue"].put(None)
                else:
                    log.debug("Unhandled event: %s", event)

            elif msg.type == WSMsgType.ERROR:
                log.error("WS error %s: %s", ws_id, ws.exception())

    except Exception:
        log.exception("Exception in WS loop for %s", ws_id)
    finally:
        # cleanup: cancel worker if alive, drain queue
        try:
            w = CONNS[ws_id].get("worker")
            if w and not w.done():
                w.cancel()
                try:
                    await w
                except Exception:
                    pass
        except Exception:
            log.exception("Error cancelling worker for %s", ws_id)
        CONNS.pop(ws_id, None)
        await ws.close()
        log.info("WS closed %s", ws_id)
    return ws

# ---------- Cleanup loop ----------
async def cleanup_loop():
    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=MP3_RETENTION_HOURS)
            for p in list(STATIC_DIR.glob("*.mp3")) + list(RAW_DIR.glob("*")):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        log.info("Removing old file %s", p.name)
                        p.unlink(missing_ok=True)
                except Exception:
                    log.exception("Error cleaning file %s", p.name)
        except Exception:
            log.exception("Cleanup loop failure")
        await asyncio.sleep(3600)

# ---------- Graceful shutdown ----------
def _setup_signals(loop, app_task):
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_graceful_shutdown(s, app_task)))

async def _graceful_shutdown(sig, app_task):
    log.warning("Received signal %s. Graceful shutdown...", sig)
    # Cancel remaining connection workers
    for ws_id, meta in list(CONNS.items()):
        w = meta.get("worker")
        if w and not w.done():
            try:
                w.cancel()
            except Exception:
                pass
    # allow tasks to finish briefly
    await asyncio.sleep(1)
    # stop the aiohttp server by cancelling the main app_task
    app_task.cancel()

# ---------- App creation ----------
def create_app():
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    return app

# ---------- Entrypoint ----------
if __name__ == "__main__":
    app = create_app()
    loop = asyncio.get_event_loop()
    # start cleanup loop
    loop.create_task(cleanup_loop())
    # run app
    app_task = asyncio.create_task(web._run_app(app, host="0.0.0.0", port=PORT))
    _setup_signals(loop, app_task)
    try:
        loop.run_until_complete(app_task)
    except asyncio.CancelledError:
        log.info("App task cancelled — exiting")
    except Exception:
        log.exception("Server crashed")
    finally:
        log.info("Server shutdown complete")
