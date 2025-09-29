# streaming_server.py
"""
Full production streaming server for Sara:
- Receives Twilio Media Stream WebSocket connections (JSON events)
- Buffers base64 audio from Twilio ("media" events)
- Converts to WAV via ffmpeg
- Sends WAV to OpenAI Whisper for transcription (REST)
- Calls OpenAI chat completions with persona/system prompt to get reply
- Generates TTS via ElevenLabs and converts to Twilio raw PCM frames
- Streams audio back to Twilio via WebSocket "media" messages
- Saves transcripts and MP3s under static/ for audit and Twilio <Play> fallback
- Cleans up old files
"""

import os
import json
import time
import base64
import logging
import asyncio
import aiofiles
import tempfile
import pathlib
import subprocess
from datetime import datetime, timedelta
from aiohttp import web, ClientSession, WSMsgType

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming-prod")

# --- Required env vars ---
REQUIRED = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "PUBLIC_STREAMING_URL"  # used for generating public MP3 URLs (if needed)
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 5001))
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))
BUFFER_FLUSH_BYTES = int(os.environ.get("BUFFER_FLUSH_BYTES", 80 * 1024))  # flush after ~80KB
TMP_DIR = pathlib.Path("tmp")
STATIC_DIR = pathlib.Path("static")
DATA_DIR = pathlib.Path("data")
TMP_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# --- Persona loading ---
PERSONA_FILES = [
    "Sara_Opening.json",
    "Sara_Objections.json",
    "Sara_KnowledgeBase.json",
    "Sara_CallFlow.json",
    "Sara_Playbook.json",
    "Sara_SystemPrompt_Production.json"
]

PERSONAS = {}
for fn in PERSONA_FILES:
    p = DATA_DIR / fn
    if not p.exists():
        log.error("Missing persona file: %s", p)
        raise SystemExit(f"Missing persona file: {fn}")
    with open(p, "r", encoding="utf-8") as fh:
        PERSONAS[fn] = json.load(fh)
        log.info("Loaded persona: %s", fn)

# pick system prompt content (supports either string or dict->{prompt})
SYSTEM_PROMPT = ""
sp = PERSONAS.get("Sara_SystemPrompt_Production.json")
if isinstance(sp, dict) and "prompt" in sp:
    SYSTEM_PROMPT = sp["prompt"]
else:
    SYSTEM_PROMPT = json.dumps(sp, ensure_ascii=False) if sp else ""

log.info("SYSTEM_PROMPT length: %d", len(SYSTEM_PROMPT))

# --- Per-connection state ---
CONNS = {}  # ws -> {buffer: bytearray, call_sid, sample_rate, processing_flag, last_media_ts}

# --- Utilities: ffmpeg conversion ---
def ensure_ffmpeg():
    try:
        rv = subprocess.run(["ffmpeg", "-version"], capture_output=True)
        if rv.returncode != 0:
            raise FileNotFoundError
    except Exception:
        log.error("ffmpeg not found on PATH. Install ffmpeg for audio conversions.")
        raise SystemExit("ffmpeg required")

def raw_bytes_to_wav(raw_path: str, wav_path: str, sample_rate: int = 8000):
    """
    Convert raw PCM (s16le) at sample_rate to a WAV file 16kHz mono.
    Twilio media payloads are typically 8kHz s16le (check your stream 'start' event).
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", raw_path,
        "-ar", "16000", "-ac", "1",
        wav_path
    ]
    log.debug("Running ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        log.error("ffmpeg convert error: %s", proc.stderr.decode(errors="ignore")[:1000])
        raise RuntimeError("ffmpeg conversion failed")
    return wav_path

def mp3_to_twilio_raw(mp3_path: str, out_raw_path: str, out_sample_rate: int = 8000):
    """
    Convert MP3 to s16le RAW at out_sample_rate for Twilio playback frames.
    We'll produce s16le PCM (signed 16-bit little-endian) at 8000 Hz mono.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", mp3_path,
        "-f", "s16le", "-ar", str(out_sample_rate), "-ac", "1",
        out_raw_path
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        log.error("ffmpeg mp3->raw error: %s", proc.stderr.decode(errors="ignore")[:1000])
        raise RuntimeError("ffmpeg mp3->raw failed")
    return out_raw_path

# --- OpenAI Whisper transcription (async) ---
async def transcribe_wav_with_openai(wav_path: str, model: str = "whisper-1", timeout: int = 60):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    # Use multipart/form-data
    data = aiohttp.FormData()
    data.add_field("file", open(wav_path, "rb"), filename=pathlib.Path(wav_path).name, content_type="audio/wav")
    data.add_field("model", model)
    async with ClientSession() as session:
        async with session.post(url, headers=headers, data=data, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                log.error("OpenAI transcription failed %s: %s", resp.status, text[:1000])
                return None
            j = await resp.json()
            return j.get("text")

# --- OpenAI Chat (async) ---
async def ask_llm(user_text: str, system_prompt: str = SYSTEM_PROMPT, model: str = "gpt-4o-mini", timeout: int = 60):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.2,
        "max_tokens": 400
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            txt = await resp.text()
            if resp.status != 200:
                log.error("LLM request failed %s: %s", resp.status, txt[:1000])
                return None
            j = await resp.json()
            try:
                return j["choices"][0]["message"]["content"].strip()
            except Exception:
                log.error("Unexpected LLM response: %s", j)
                return None

# --- ElevenLabs TTS (async) ---
async def eleven_tts_to_mp3(text: str, out_mp3_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.read()
            if resp.status != 200:
                log.error("ElevenLabs TTS failed %s: %s", resp.status, data[:1000])
                return None
            async with aiofiles.open(out_mp3_path, "wb") as fh:
                await fh.write(data)
            return out_mp3_path

# --- Twilio-playable media payload builder ---
def build_twilio_media_payload_from_raw(raw_path: str, chunk_size: int = 3200):
    """
    Read raw s16le file and yield base64 frames sized to chunk_size bytes.
    Twilio expects base64 payloads in 'media' events.
    """
    with open(raw_path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield base64.b64encode(chunk).decode("ascii")

# --- Processing pipeline for one connection ---
async def process_buffer_and_respond(ws_id: str):
    """
    For a particular ws_id, take its buffer, write to raw file,
    convert to wav, transcribe, ask LLM, tts, convert and send back to Twilio
    """
    meta = CONNS.get(ws_id)
    if not meta:
        log.warning("process called with missing meta %s", ws_id)
        return
    if meta.get("processing"):
        log.info("Already processing %s", ws_id)
        return
    meta["processing"] = True
    try:
        buf = meta.get("buffer", bytearray())
        if not buf:
            log.info("No audio buffer to process for %s", ws_id)
            return
        call_sid = meta.get("call_sid")
        sample_rate = meta.get("sample_rate", 8000)
        now_ts = int(time.time())
        raw_file = TMP_DIR / f"{ws_id}_{now_ts}.s16le"
        wav_file = TMP_DIR / f"{ws_id}_{now_ts}.wav"
        mp3_file = STATIC_DIR / f"{ws_id}_{now_ts}.mp3"
        raw_file.write_bytes(bytes(buf))
        meta["buffer"] = bytearray()

        # Convert raw PCM to WAV (blocking operation) in thread pool
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, raw_bytes_to_wav, str(raw_file), str(wav_file), sample_rate)

        # Transcribe
        transcript = await transcribe_wav_with_openai(str(wav_file))
        if not transcript:
            log.warning("No transcript produced for %s", ws_id)
            return
        log.info("Transcript for call %s: %s", call_sid or ws_id, transcript[:400])

        # Ask LLM
        reply = await ask_llm(transcript)
        if not reply:
            log.warning("LLM returned empty response for %s", ws_id)
            return
        log.info("LLM reply for %s: %s", ws_id, reply[:500])

        # TTS -> mp3
        out_mp3 = await eleven_tts_to_mp3(reply, str(mp3_file))
        if not out_mp3:
            log.warning("TTS failed for %s", ws_id)
            return

        # Convert mp3 to raw s16le frames for Twilio
        raw_for_twilio = TMP_DIR / f"{ws_id}_{now_ts}_twilio.s16le"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, mp3_to_twilio_raw, str(mp3_file), str(raw_for_twilio), sample_rate)

        # Stream audio back to Twilio by sending media events from server -> Twilio
        # Need to find websocket object for ws_id
        ws_obj = meta.get("ws")
        if not ws_obj:
            log.warning("No websocket to send response for %s", ws_id)
            return

        # Send each chunk as a media event
        for payload_b64 in build_twilio_media_payload_from_raw(str(raw_for_twilio), chunk_size=3200):
            msg = {"event": "media", "media": {"payload": payload_b64}}
            try:
                await ws_obj.send_str(json.dumps(msg))
                # small sleep to avoid overwhelming
                await asyncio.sleep(0.02)
            except Exception:
                log.exception("Failed to send media frame back to Twilio for %s", ws_id)
                break

        log.info("Finished sending audio back to Twilio for %s", ws_id)

    except Exception:
        log.exception("Error in process_buffer_and_respond for %s", ws_id)
    finally:
        meta["processing"] = False

# --- Web handlers ---
routes = web.RouteTableDef()

@routes.get("/health")
async def handle_health(request):
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})

@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_id = str(int(time.time()*1000)) + "_" + str(id(ws))  # quick unique id
    CONNS[ws_id] = {"buffer": bytearray(), "call_sid": None, "sample_rate": 8000, "processing": False, "ws": ws, "last_media_ts": time.time()}
    log.info("New Twilio WS connected: %s", ws_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    log.debug("Non-JSON text message")
                    continue
                event = j.get("event")
                if event == "start":
                    start = j.get("start", {})
                    call_sid = start.get("callSid")
                    sr = start.get("sample_rate") or start.get("sampleRate") or start.get("sampleRateHz") or 8000
                    CONNS[ws_id]["call_sid"] = call_sid
                    CONNS[ws_id]["sample_rate"] = int(sr)
                    CONNS[ws_id]["last_media_ts"] = time.time()
                    log.info("Stream START ws=%s call_sid=%s sample_rate=%s", ws_id, call_sid, sr)

                elif event == "media":
                    media = j.get("media", {})
                    payload = media.get("payload")
                    if not payload:
                        log.debug("Media event missing payload")
                        continue
                    try:
                        chunk = base64.b64decode(payload)
                    except Exception:
                        log.exception("Failed to decode payload")
                        continue
                    CONNS[ws_id]["buffer"].extend(chunk)
                    CONNS[ws_id]["last_media_ts"] = time.time()
                    # If buffer large enough, schedule processing
                    if len(CONNS[ws_id]["buffer"]) >= BUFFER_FLUSH_BYTES:
                        # schedule background processing
                        asyncio.create_task(process_buffer_and_respond(ws_id))
                elif event == "stop":
                    log.info("Stream STOP ws=%s", ws_id)
                    # final flush
                    await process_buffer_and_respond(ws_id)
                    # twilio will close after
                elif event == "mark":
                    log.debug("Mark: %s", j.get("timestamp"))
                else:
                    log.debug("Unhandled event: %s", event)

            elif msg.type == WSMsgType.ERROR:
                log.error("WS error %s: %s", ws_id, ws.exception())
            elif msg.type == WSMsgType.BINARY:
                log.debug("Binary message received (unexpected) len=%d", len(msg.data))
    except Exception:
        log.exception("Exception in WS loop for %s", ws_id)
    finally:
        log.info("Closing WS %s", ws_id)
        try:
            await ws.close()
        except Exception:
            pass
        CONNS.pop(ws_id, None)
    return ws

# --- Cleanup loop for old files ---
async def cleanup_loop():
    while True:
        try:
            now = datetime.utcnow()
            cutoff = now - timedelta(hours=MP3_RETENTION_HOURS)
            for p in STATIC_DIR.glob("*"):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        p.unlink(missing_ok=True)
                        log.info("Removed old static file %s", p.name)
                except Exception:
                    log.exception("Cleanup error static %s", p.name)
            for p in TMP_DIR.glob("*"):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        p.unlink(missing_ok=True)
                        log.info("Removed old tmp file %s", p.name)
                except Exception:
                    log.exception("Cleanup error tmp %s", p.name)
        except Exception:
            log.exception("Cleanup loop error")
        await asyncio.sleep(3600)

# --- App factory ---
def init_app():
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    return app

# --- Start server safely with asyncio.run ---
async def main():
    app = init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Streaming server listening on port %d", PORT)
    # background cleanup
    asyncio.create_task(cleanup_loop())
    # keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Server crashed on startup")
        raise
