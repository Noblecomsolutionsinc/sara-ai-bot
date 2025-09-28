# streaming_server.py
import os
import io
import json
import uuid
import time
import shutil
import pathlib
import base64
import logging
import asyncio
import aiohttp
import subprocess
from datetime import datetime, timedelta
from aiohttp import web, WSMsgType
from twilio.rest import Client as TwilioClient

# -----------------------
# Logging & paths
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ROOT = pathlib.Path(".").resolve()
STATIC_DIR = ROOT / "static"
RAW_DIR = ROOT / "raw"
DATA_DIR = ROOT / "data"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------
# Env / config (fail fast)
# -----------------------
REQUIRED = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVEN_VOICE_ID",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "PUBLIC_STREAMING_URL"
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    logging.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVEN_VOICE_ID"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "24"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")
OPENAI_CHAT_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))

# Twilio client (blocking)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# -----------------------
# Load persona JSON files
# -----------------------
def load_persona_files(data_dir=DATA_DIR):
    persona = {}
    if not data_dir.exists():
        logging.warning("Data directory %s not found; continuing without persona files.", data_dir)
        return persona
    for file in sorted(data_dir.glob("*.json")):
        try:
            persona[file.name] = json.loads(file.read_text(encoding="utf-8"))
            logging.info("Loaded persona file: %s", file.name)
        except Exception as e:
            logging.exception("Failed to load %s: %s", file.name, e)
    return persona

PERSONA = load_persona_files()

def build_system_prompt(persona):
    """
    Build a single system prompt text combining persona files.
    Prefers Sara_SystemPrompt_Production.json if present.
    """
    if "Sara_SystemPrompt_Production.json" in persona:
        obj = persona["Sara_SystemPrompt_Production.json"]
        # if the system prompt is stored as object with a 'prompt' key
        if isinstance(obj, dict) and "prompt" in obj:
            return obj["prompt"]
        return json.dumps(obj, indent=2)
    # Otherwise aggregate main files but keep it succinct
    aggregated = {}
    for k, v in persona.items():
        aggregated[k] = v
    # Truncate heavy fields if needed (avoid blowing token budgets)
    return "Persona files:\n" + json.dumps(aggregated, indent=2)

SYSTEM_PROMPT = build_system_prompt(PERSONA)
logging.info("System prompt size: %d chars", len(SYSTEM_PROMPT))

# -----------------------
# Helpers: ffmpeg conversion
# -----------------------
def ensure_ffmpeg():
    """Raise if ffmpeg not available."""
    try:
        proc = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise FileNotFoundError("ffmpeg not available")
    except FileNotFoundError:
        logging.error("ffmpeg not found on PATH. Install it on your host. Deployment cannot proceed.")
        raise

def convert_raw_to_wav_blocking(raw_path: str, wav_path: str, sample_rate: int = 8000):
    """
    Convert Twilio raw s16le PCM (sample_rate, mono) to 16k WAV accepted by Whisper.
    Blocking call (wrap in executor).
    """
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", str(raw_path),
        "-ar", "16000", "-ac", "1",
        str(wav_path)
    ]
    logging.info("Running ffmpeg conversion: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        logging.error("ffmpeg failed: %s", proc.stderr.decode()[:1000])
        raise RuntimeError("ffmpeg conversion failed")
    return wav_path

# -----------------------
# OpenAI Whisper transcription (async wrapper)
# -----------------------
async def transcribe_with_openai(wav_path: str):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = aiohttp.FormData()
    # Open file and attach
    with open(wav_path, "rb") as fh:
        data.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        data.add_field("model", WHISPER_MODEL)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=120) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logging.error("Whisper transcription failed (%s): %s", resp.status, text[:1000])
                    return None
                j = await resp.json()
                return j.get("text")

# -----------------------
# OpenAI Chat (LLM)
# -----------------------
async def ask_llm(prompt_text: str):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text}
        ],
        "temperature": 0.2,
        "max_tokens": 400
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=OPENAI_CHAT_TIMEOUT) as resp:
            text = await resp.text()
            if resp.status != 200:
                logging.error("LLM request failed (%s): %s", resp.status, text[:1000])
                return None
            j = await resp.json()
            try:
                return j["choices"][0]["message"]["content"].strip()
            except Exception:
                logging.error("Unexpected LLM response shape: %s", j)
                return None

# -----------------------
# ElevenLabs TTS
# -----------------------
async def eleven_synthesize_mp3(text: str, out_path: str):
    """
    Call ElevenLabs streaming TTS endpoint and save MP3 bytes to out_path.
    """
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json"
    }
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.read()
            if resp.status != 200:
                logging.error("ElevenLabs TTS failed (%s): %s", resp.status, data[:1000])
                return None
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path

# -----------------------
# Twilio call update (blocking) - play the MP3 into call
# -----------------------
def play_audio_into_call_blocking(call_sid: str, public_url: str):
    """
    Update Twilio call TwiML to play the provided public_url (MP3).
    Blocking; run in executor.
    """
    try:
        logging.info("Updating Call SID %s to play %s", call_sid, public_url)
        twilio_client.calls(call_sid).update(twiml=f"<Response><Play>{public_url}</Play></Response>")
    except Exception as e:
        logging.exception("Twilio call update failed for SID %s: %s", call_sid, e)
        raise

async def play_audio_into_call(call_sid: str, public_url: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, play_audio_into_call_blocking, call_sid, public_url)

# -----------------------
# Per-connection buffer & processing
# -----------------------
connections = {}  # ws_id -> meta dict

async def process_buffer_and_respond(ws_id: str):
    """
    Convert buffer -> wav -> transcript -> LLM -> TTS -> save -> instruct Twilio to play
    """
    meta = connections.get(ws_id)
    if not meta:
        logging.warning("No connection meta for %s", ws_id)
        return
    if meta.get("processing"):
        logging.info("Already processing %s, skipping", ws_id)
        return
    meta["processing"] = True
    try:
        buffer = meta.get("buffer", bytearray())
        if not buffer:
            logging.info("Empty buffer for %s", ws_id)
            return

        call_sid = meta.get("call_sid")
        sample_rate = int(meta.get("sample_rate", 8000))
        stamp = int(time.time())
        raw_file = RAW_DIR / f"{ws_id}_{stamp}.s16le"
        wav_file = RAW_DIR / f"{ws_id}_{stamp}.wav"
        mp3_file = STATIC_DIR / f"{ws_id}_{stamp}.mp3"

        # write raw PCM
        raw_file.write_bytes(bytes(buffer))
        # reset buffer
        meta["buffer"] = bytearray()

        # convert using ffmpeg in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, convert_raw_to_wav_blocking, str(raw_file), str(wav_file), sample_rate)

        # transcribe via OpenAI Whisper
        transcript = await transcribe_with_openai(str(wav_file))
        if not transcript:
            logging.warning("No transcript produced for %s", ws_id)
            return
        logging.info("Transcript (call=%s): %s", call_sid, transcript[:400])

        # call LLM with persona context
        llm_reply = await ask_llm(transcript)
        if not llm_reply:
            logging.warning("LLM returned empty reply for %s", ws_id)
            return
        logging.info("LLM reply (call=%s): %s", call_sid, llm_reply[:400])

        # synthesize to MP3 via ElevenLabs
        out_mp3 = await eleven_synthesize_mp3(llm_reply, str(mp3_file))
        if not out_mp3:
            logging.warning("TTS failed for %s", ws_id)
            return

        # public URL for Twilio to play
        public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_file.name}"
        logging.info("Generated audio for call %s at %s", call_sid, public_url)

        # instruct Twilio to play (if we have call_sid)
        if call_sid:
            await play_audio_into_call(call_sid, public_url)
            logging.info("Requested Twilio to play audio into call %s", call_sid)
        else:
            logging.warning("No call_sid for ws %s; cannot play", ws_id)

    except Exception as e:
        logging.exception("process_buffer_and_respond error for %s: %s", ws_id, e)
    finally:
        meta["processing"] = False
        # optional: keep raw & wav & mp3 for debugging; external rotation will remove old files

# -----------------------
# WebSocket handler for Twilio Media Streams
# -----------------------
routes = web.RouteTableDef()

@routes.get("/health")
async def health(request):
    return web.json_response({"status": "ok"})

@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_id = str(uuid.uuid4())
    connections[ws_id] = {"buffer": bytearray(), "call_sid": None, "sample_rate": 8000, "last_media_ts": time.time(), "processing": False}
    logging.info("New WS connection %s", ws_id)

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    logging.debug("Non-JSON text from Twilio: %s", msg.data[:200])
                    continue

                event = j.get("event")
                if event == "start":
                    start = j.get("start", {})
                    call_sid = start.get("callSid")
                    sample_rate = start.get("sample_rate") or start.get("sampleRate") or 8000
                    connections[ws_id]["call_sid"] = call_sid
                    connections[ws_id]["sample_rate"] = int(sample_rate)
                    connections[ws_id]["last_media_ts"] = time.time()
                    logging.info("Stream START ws=%s call_sid=%s sample_rate=%s", ws_id, call_sid, sample_rate)

                elif event == "media":
                    media = j.get("media", {})
                    payload = media.get("payload")
                    if payload:
                        chunk = base64.b64decode(payload)
                        connections[ws_id]["buffer"].extend(chunk)
                        connections[ws_id]["last_media_ts"] = time.time()
                        # flush heuristic: if buffer > ~80KB, process
                        if len(connections[ws_id]["buffer"]) > 80 * 1024:
                            asyncio.create_task(process_buffer_and_respond(ws_id))
                    else:
                        logging.debug("Media event without payload")
                elif event == "stop":
                    logging.info("Stream STOP ws=%s", ws_id)
                    # process final buffer
                    await process_buffer_and_respond(ws_id)
                else:
                    logging.debug("Unhandled event type: %s", event)

            elif msg.type == WSMsgType.BINARY:
                logging.debug("Binary message received (unexpected). Len=%s", len(msg.data))
            elif msg.type == WSMsgType.ERROR:
                logging.error("WebSocket error for ws=%s: %s", ws_id, ws.exception())

    except Exception as e:
        logging.exception("Exception in ws loop for %s: %s", ws_id, e)
    finally:
        logging.info("Closing WS %s", ws_id)
        connections.pop(ws_id, None)
        await ws.close()
    return ws

# -----------------------
# Background cleanup: rotate old MP3s & raw files
# -----------------------
async def cleanup_loop():
    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=MP3_RETENTION_HOURS)
            for p in STATIC_DIR.glob("*.mp3"):
                try:
                    mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                    if mtime < cutoff:
                        logging.info("Removing old mp3: %s", p.name)
                        p.unlink(missing_ok=True)
                except Exception:
                    logging.exception("Error cleaning mp3 %s", p)
            for p in RAW_DIR.glob("*"):
                try:
                    mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                    if mtime < cutoff:
                        logging.info("Removing old raw/wav: %s", p.name)
                        p.unlink(missing_ok=True)
                except Exception:
                    logging.exception("Error cleaning raw %s", p)
        except Exception:
            logging.exception("Cleanup loop failed")
        await asyncio.sleep(3600)  # run hourly

# -----------------------
# App startup: ensure ffmpeg + start
# -----------------------
def start_app():
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_loop())
    port = int(os.environ.get("PORT", 5000))
    logging.info("Starting streaming server (ws) on port %s", port)
    web.run_app(app, port=port)

if __name__ == "__main__":
    start_app()
