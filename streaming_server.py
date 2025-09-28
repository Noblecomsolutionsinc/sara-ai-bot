# streaming_server.py
"""
Full production streaming server — Sara AI.
Requirements:
 - ffmpeg on PATH
 - Render envs set (see early message)
 - data/*.json persona files present
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
from datetime import datetime, timedelta
from aiohttp import web, WSMsgType, ClientSession
from twilio.rest import Client as TwilioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming")

# Paths
ROOT = pathlib.Path(".").resolve()
STATIC_DIR = ROOT / "static"
RAW_DIR = ROOT / "raw"
DATA_DIR = ROOT / "data"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Env (strict)
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

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")  # used to build MP3 public URLs

PORT = int(os.environ.get("PORT", 6000))
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))

# Twilio client (blocking)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ensure ffmpeg installed
def ensure_ffmpeg():
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise FileNotFoundError
    except Exception:
        log.error("ffmpeg not found. Install ffmpeg or use a Docker image that includes it.")
        raise SystemExit("ffmpeg not available")

# ffmpeg conversion (blocking)
def convert_raw_to_wav_blocking(raw_path: str, wav_path: str, sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", str(raw_path),
        "-ar", "16000", "-ac", "1",
        str(wav_path)
    ]
    log.info("Running ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        log.error("ffmpeg failed: %s", proc.stderr.decode(errors="ignore")[:1000])
        raise RuntimeError("ffmpeg conversion failed")
    return wav_path

# Load persona JSONs
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
    # fallback: aggregate trimmed persona
    out = {}
    for k, v in persona_map.items():
        try:
            out[k] = v if isinstance(v, (str, int, float)) else "<object>"
        except Exception:
            out[k] = "<error>"
    return "Sara persona summary:\n" + json.dumps(out, ensure_ascii=False)

SYSTEM_PROMPT = build_system_prompt(PERSONA)
log.info("SYSTEM_PROMPT length: %d", len(SYSTEM_PROMPT))

# OpenAI transcription (whisper) - REST
async def transcribe_with_openai(wav_path: str):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = aiohttp.FormData()
    with open(wav_path, "rb") as fh:
        data.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        data.add_field("model", WHISPER_MODEL)
        async with ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=120) as resp:
                txt = await resp.text()
                if resp.status != 200:
                    log.error("Whisper failed %s: %s", resp.status, txt[:1000])
                    return None
                j = await resp.json()
                return j.get("text")

# OpenAI chat completion (LLM)
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
        "max_tokens": 400
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=OPENAI_TIMEOUT) as resp:
            txt = await resp.text()
            if resp.status != 200:
                log.error("LLM failed %s: %s", resp.status, txt[:1000])
                return None
            j = await resp.json()
            try:
                return j["choices"][0]["message"]["content"].strip()
            except Exception:
                log.error("Unexpected LLM response: %s", j)
                return None

# ElevenLabs TTS -> save MP3
async def eleven_synthesize_mp3(text: str, out_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.read()
            if resp.status != 200:
                log.error("ElevenLabs TTS failed %s: %s", resp.status, data[:1000])
                return None
            with open(out_path, "wb") as fh:
                fh.write(data)
            return out_path

# Twilio update to play MP3 (blocking)
def twilio_play_blocking(call_sid: str, public_url: str):
    try:
        log.info("Updating Twilio call %s to play %s", call_sid, public_url)
        twilio_client.calls(call_sid).update(twiml=f"<Response><Play>{public_url}</Play></Response>")
    except Exception:
        log.exception("Twilio update failed for %s", call_sid)
        raise

async def twilio_play(call_sid: str, public_url: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, twilio_play_blocking, call_sid, public_url)

# Per-connection state
CONNS = {}  # ws_id -> {buffer, call_sid, sample_rate, processing}
BUFFER_FLUSH_BYTES = int(os.environ.get("BUFFER_FLUSH_BYTES", 100 * 1024))

# Processing pipeline
async def process_and_respond(ws_id: str):
    meta = CONNS.get(ws_id)
    if not meta:
        log.warning("No meta for %s", ws_id)
        return
    if meta.get("processing"):
        log.info("Already processing %s", ws_id)
        return
    meta["processing"] = True
    try:
        buf = meta.get("buffer", bytearray())
        if not buf:
            log.info("Empty buffer for %s", ws_id)
            return
        call_sid = meta.get("call_sid")
        sample_rate = meta.get("sample_rate", 8000)
        stamp = int(time.time())
        raw_fn = RAW_DIR / f"{ws_id}_{stamp}.s16le"
        wav_fn = RAW_DIR / f"{ws_id}_{stamp}.wav"
        mp3_fn = STATIC_DIR / f"{ws_id}_{stamp}.mp3"

        raw_fn.write_bytes(bytes(buf))
        meta["buffer"] = bytearray()

        # convert raw->wav in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, convert_raw_to_wav_blocking, str(raw_fn), str(wav_fn), sample_rate)

        # transcribe
        transcript = await transcribe_with_openai(str(wav_fn))
        if not transcript:
            log.warning("No transcript for %s", ws_id)
            return
        log.info("Transcript (call=%s): %.200s", call_sid or "unknown", transcript)

        # LLM reply (uses persona)
        reply = await ask_llm(transcript)
        if not reply:
            log.warning("LLM returned empty for %s", ws_id)
            return
        log.info("LLM reply (call=%s): %.200s", call_sid or "unknown", reply)

        # TTS
        out_mp3 = await eleven_synthesize_mp3(reply, str(mp3_fn))
        if not out_mp3:
            log.warning("TTS failed for %s", ws_id)
            return

        public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
        log.info("Generated MP3 at %s", public_url)

        if call_sid:
            await twilio_play(call_sid, public_url)
            log.info("Requested Twilio to play for call %s", call_sid)
        else:
            log.warning("No call_sid for ws %s", ws_id)

    except Exception:
        log.exception("Error in process_and_respond for %s", ws_id)
    finally:
        meta["processing"] = False

# Web handlers
routes = web.RouteTableDef()

@routes.get("/health")
async def health(request):
    # Render uses HEAD; aiohttp responds to HEAD automatically for GET handlers.
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})

@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws_id = str(uuid.uuid4())
    CONNS[ws_id] = {"buffer": bytearray(), "call_sid": None, "sample_rate": 8000, "processing": False, "last_media_ts": time.time()}
    log.info("New WS connection %s", ws_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    log.debug("Non-JSON text: %s", msg.data[:200])
                    continue
                event = j.get("event")
                if event == "start":
                    start = j.get("start", {})
                    call_sid = start.get("callSid")
                    sr = start.get("sample_rate") or start.get("sampleRate") or 8000
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
                    if len(CONNS[ws_id]["buffer"]) >= BUFFER_FLUSH_BYTES:
                        asyncio.create_task(process_and_respond(ws_id))

                elif event == "stop":
                    log.info("Stream STOP ws=%s", ws_id)
                    await process_and_respond(ws_id)

                else:
                    log.debug("Unhandled event: %s", event)

            elif msg.type == WSMsgType.BINARY:
                log.debug("Binary message (unexpected) len=%d", len(msg.data))
            elif msg.type == WSMsgType.ERROR:
                log.error("WS error %s: %s", ws_id, ws.exception())

    except Exception:
        log.exception("Exception in WS loop for %s", ws_id)
    finally:
        log.info("Closing WS %s", ws_id)
        CONNS.pop(ws_id, None)
        await ws.close()
    return ws

# cleanup loop
async def cleanup_loop():
    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=MP3_RETENTION_HOURS)
            for p in STATIC_DIR.glob("*.mp3"):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        log.info("Removing old mp3 %s", p.name)
                        p.unlink(missing_ok=True)
                except Exception:
                    log.exception("Error cleaning mp3 %s", p.name)
            for p in RAW_DIR.glob("*"):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        log.info("Removing old raw %s", p.name)
                        p.unlink(missing_ok=True)
                except Exception:
                    log.exception("Error cleaning raw %s", p.name)
        except Exception:
            log.exception("Cleanup loop failure")
        await asyncio.sleep(3600)

# Create and run app
def create_app():
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    return app

if __name__ == "__main__":
    app = create_app()
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_loop())
    log.info("Starting streaming server on port %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)
