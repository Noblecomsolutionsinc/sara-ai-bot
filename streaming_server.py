# streaming_server.py
import os
import time
import uuid
import json
import base64
import logging
import pathlib
import subprocess
import asyncio
import aiohttp
from aiohttp import web
from twilio.rest import Client as TwilioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Required env vars (fail fast) ---
REQUIRED = [
    "OPENAI_API_KEY",
    "OPENAI_CHAT_MODEL",   # e.g. "gpt-4o-mini" or "gpt-4o"
    "ELEVENLABS_API_KEY",
    "ELEVEN_VOICE_ID",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "PUBLIC_STREAMING_URL"  # e.g. https://sara-ai-streaming.onrender.com
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

# Directories
BASE = pathlib.Path(".")
STATIC_DIR = BASE / "static"
RAW_DIR = BASE / "raw"
DATA_DIR = BASE / "data"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Twilio REST client (blocking)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

routes = web.RouteTableDef()

# in-memory map ws_id -> metadata
connections = {}

# load persona JSON files (your Sara_*.json files)
def load_persona(data_dir=str(DATA_DIR)):
    persona = {}
    try:
        for fn in os.listdir(data_dir):
            if fn.endswith(".json"):
                p = pathlib.Path(data_dir) / fn
                try:
                    persona[fn] = json.loads(p.read_text(encoding="utf-8"))
                    logging.info("Loaded persona file: %s", fn)
                except Exception as e:
                    logging.warning("Failed to load %s: %s", fn, e)
    except Exception as e:
        logging.warning("No persona files loaded: %s", e)
    return persona

SARA_PERSONA = load_persona()

def build_system_prompt(persona_dict):
    """
    Combine persona JSONs into a single system prompt string.
    Keep it concise but include important keys. You can refine this later.
    """
    try:
        # Prefer a dedicated system prompt file if present
        sysfile = "Sara_SystemPrompt_Production.json"
        if sysfile in persona_dict:
            return json.dumps(persona_dict[sysfile], indent=2)
        # else, concatenate important files
        combined = {"files": {}}
        for k, v in persona_dict.items():
            combined["files"][k] = v
        return json.dumps(combined, indent=2)
    except Exception as e:
        logging.exception("Failed to build system prompt: %s", e)
        return "You are Sara AI. Be concise and helpful."

SYSTEM_PROMPT = build_system_prompt(SARA_PERSONA)

# ffmpeg conversion helper (blocking)
def convert_raw_to_wav(raw_path, wav_path, sample_rate=8000):
    # raw s16le -> 16k wav mono for Whisper
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", str(raw_path),
        "-ar", "16000", "-ac", "1",
        str(wav_path)
    ]
    logging.info("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        logging.error("ffmpeg error: %s", proc.stderr.decode()[:400])
        raise RuntimeError("ffmpeg conversion failed")
    return wav_path

# Transcribe with OpenAI Whisper via REST
async def transcribe_with_openai(wav_path):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = aiohttp.FormData()
    # keep file open while request runs
    with open(wav_path, "rb") as fh:
        data.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        data.add_field("model", "whisper-1")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=data, timeout=120) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logging.error("Whisper failed %s: %s", resp.status, text[:400])
                    return None
                j = await resp.json()
                return j.get("text")

# Ask LLM (chat completion)
async def ask_llm(prompt_text):
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
        async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
            txt = await resp.text()
            if resp.status != 200:
                logging.error("LLM failed %s: %s", resp.status, txt[:400])
                return None
            j = await resp.json()
            try:
                return j["choices"][0]["message"]["content"].strip()
            except Exception:
                logging.error("LLM response parse error: %s", j)
                return None

# ElevenLabs TTS -> write MP3
async def eleven_synthesize(text, out_path):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json"
    }
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
            data = await resp.read()
            if resp.status != 200:
                logging.error("ElevenLabs error %s: %s", resp.status, data[:400])
                return None
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path

# Blocking Twilio call update to play audio
def play_audio_into_call_blocking(call_sid, public_url):
    logging.info("Updating Twilio call %s to play %s", call_sid, public_url)
    try:
        twilio_client.calls(call_sid).update(twiml=f"<Response><Play>{public_url}</Play></Response>")
    except Exception as e:
        logging.exception("Twilio play update failed for %s: %s", call_sid, e)
        raise

async def play_audio_into_call(call_sid, public_url):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, play_audio_into_call_blocking, call_sid, public_url)

# PROCESS buffer and respond
async def process_buffer_and_respond(ws_id):
    meta = connections.get(ws_id)
    if not meta:
        logging.warning("No meta for ws_id %s", ws_id)
        return
    # simple lock
    if meta.get("processing"):
        logging.info("Already processing ws_id %s — skipping", ws_id)
        return
    meta["processing"] = True
    try:
        buffer = meta.get("buffer", bytearray())
        if not buffer:
            logging.info("Empty buffer for %s", ws_id)
            return
        call_sid = meta.get("call_sid")
        sample_rate = meta.get("sample_rate", 8000)
        stamp = int(time.time())
        raw_fn = RAW_DIR / f"{ws_id}_{stamp}.s16le"
        wav_fn = RAW_DIR / f"{ws_id}_{stamp}.wav"
        mp3_fn = STATIC_DIR / f"{ws_id}_{stamp}.mp3"

        # write raw
        raw_fn.write_bytes(bytes(buffer))
        # reset buffer
        meta["buffer"] = bytearray()

        # convert
        try:
            convert_raw_to_wav(raw_fn, wav_fn, sample_rate=sample_rate)
        except Exception as e:
            logging.exception("Conversion failed: %s", e)
            return

        # transcribe
        transcript = await transcribe_with_openai(str(wav_fn))
        if not transcript:
            logging.warning("No transcript")
            return
        logging.info("Transcript: %s", transcript)

        # ask LLM
        reply = await ask_llm(transcript)
        if not reply:
            logging.warning("LLM returned empty reply")
            return
        logging.info("LLM reply: %s", reply)

        # synthesize
        out_mp3 = await eleven_synthesize(reply, str(mp3_fn))
        if not out_mp3:
            logging.warning("TTS failed")
            return

        public_url = f"{PUBLIC_STREAMING_URL}/static/{mp3_fn.name}"
        logging.info("Audio saved and public at %s", public_url)

        if call_sid:
            await play_audio_into_call(call_sid, public_url)
            logging.info("Play requested for call %s", call_sid)
        else:
            logging.warning("No call_sid for ws_id %s", ws_id)

    finally:
        meta["processing"] = False
        # optional cleanup policy: keep raw & mp3 for debugging. Implement rotation externally.

# Twilio media websocket endpoint
@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws_id = str(uuid.uuid4())
    connections[ws_id] = {"buffer": bytearray(), "call_sid": None, "sample_rate": 8000, "last_media_ts": time.time(), "processing": False}
    logging.info("WS connected %s", ws_id)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    j = json.loads(msg.data)
                except Exception:
                    logging.debug("Non-JSON text: %s", msg.data[:200])
                    continue
                event = j.get("event")
                if event == "start":
                    start = j.get("start", {})
                    call_sid = start.get("callSid")
                    sr = start.get("sample_rate") or start.get("sampleRate") or 8000
                    connections[ws_id]["call_sid"] = call_sid
                    connections[ws_id]["sample_rate"] = int(sr)
                    logging.info("Stream start ws=%s call_sid=%s sample_rate=%s", ws_id, call_sid, sr)
                elif event == "media":
                    payload = j.get("media", {}).get("payload")
                    if payload:
                        chunk = base64.b64decode(payload)
                        connections[ws_id]["buffer"].extend(chunk)
                        connections[ws_id]["last_media_ts"] = time.time()
                        # heuristics: process when buffer big enough
                        if len(connections[ws_id]["buffer"]) > 80 * 1024:
                            asyncio.create_task(process_buffer_and_respond(ws_id))
                    else:
                        logging.debug("Media event with no payload")
                elif event == "stop":
                    logging.info("Stream stop ws=%s", ws_id)
                    # process final buffer
                    await process_buffer_and_respond(ws_id)
                else:
                    logging.debug("Unhandled event: %s", event)

            elif msg.type == web.WSMsgType.ERROR:
                logging.error("WS error %s: %s", ws_id, ws.exception())

    except Exception as e:
        logging.exception("WS loop exception %s: %s", ws_id, e)
    finally:
        logging.info("WS closing %s", ws_id)
        connections.pop(ws_id, None)
        await ws.close()
    return ws

# App setup
app = web.Application()
app.add_routes(routes)
app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 6000))
    logging.info("Starting streaming server on port %s", port)
    web.run_app(app, port=port)
