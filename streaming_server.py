# File: streaming_server.py
"""
Near real-time Twilio Media Streams -> Whisper -> Chat -> ElevenLabs with barge-in.
Drop into your project as streaming_server.py and deploy.
Reads credentials from environment:
OPENAI_API_KEY, ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, PUBLIC_STREAMING_URL, etc.
Requires ffmpeg on PATH.
"""
import os
import json
import time
import base64
import logging
import asyncio
import aiofiles
import pathlib
import subprocess
import shutil
from datetime import datetime, timedelta
from aiohttp import web, ClientSession, WSMsgType
import aiohttp  # for FormData

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming-realtime")

# --- Config & env ---
REQUIRED = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "PUBLIC_STREAMING_URL"
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1")
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "1000"))

ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
ELEVEN_TTS_TIMEOUT = int(os.environ.get("ELEVENLABS_TTS_TIMEOUT", "120"))

PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 5001))
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))

# buffer flush threshold in bytes (approx ~1s at 8000Hz/16-bit mono => ~16000 bytes)
BUFFER_FLUSH_BYTES = int(os.environ.get("BUFFER_FLUSH_BYTES", 16000))

# dirs
DATA_DIR = pathlib.Path("data")
STATIC_DIR = pathlib.Path("static")
TMP_DIR = pathlib.Path("tmp")
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

# --- Persona JSONs load ---
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

# Determine system prompt string
sp = PERSONAS.get("Sara_SystemPrompt_Production.json")
if isinstance(sp, dict) and "prompt" in sp:
    SYSTEM_PROMPT = sp["prompt"]
else:
    SYSTEM_PROMPT = sp if isinstance(sp, str) else json.dumps(sp, ensure_ascii=False)
log.info("SYSTEM_PROMPT length: %d", len(SYSTEM_PROMPT))

# --- ffmpeg check ---
def ensure_ffmpeg():
    if shutil.which("ffmpeg") is None:
        log.error("ffmpeg not found on PATH. Install ffmpeg.")
        raise SystemExit("ffmpeg required")
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception:
        log.error("ffmpeg present but failed to run - check installation")
        raise SystemExit("ffmpeg required")

# Convert raw s16le -> wav (16k) using ffmpeg (blocking; run in executor)
def raw_bytes_to_wav(raw_path: str, wav_path: str, sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", raw_path,
        "-ar", "16000", "-ac", "1",
        wav_path
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {proc.stderr.decode(errors='ignore')[:2000]}")
    return wav_path

# Convert mp3 -> s16le @ out_sample_rate for Twilio playback
def mp3_to_twilio_raw(mp3_path: str, out_raw_path: str, out_sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y",
        "-i", mp3_path,
        "-f", "s16le", "-ar", str(out_sample_rate), "-ac", "1",
        out_raw_path
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mp3->raw failed: {proc.stderr.decode(errors='ignore')[:2000]}")
    return out_raw_path

# Build base64 payloads from raw file in small chunks
def build_twilio_media_payload_from_raw(raw_path: str, chunk_size: int = 3200):
    with open(raw_path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield base64.b64encode(chunk).decode("ascii")

# --- OpenAI Whisper transcription (async) ---
async def transcribe_wav_with_openai(wav_path: str, model: str = "whisper-1", timeout: int = OPENAI_TIMEOUT):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    form = aiohttp.FormData()
    with open(wav_path, "rb") as fh:
        form.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        form.add_field("model", model)
        async with ClientSession() as session:
            async with session.post(url, headers=headers, data=form, timeout=timeout) as resp:
                text = await resp.text()
                if resp.status != 200:
                    log.error("OpenAI transcription failed %s: %s", resp.status, text[:2000])
                    return None
                j = await resp.json()
                return j.get("text")

# --- OpenAI Chat (async) ---
async def ask_llm(user_text: str, system_prompt: str = SYSTEM_PROMPT, model: str = "gpt-4o-mini", timeout: int = OPENAI_TIMEOUT):
    url = f"{OPENAI_API_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.2,
        "max_tokens": OPENAI_MAX_TOKENS
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                log.error("LLM request failed %s: %s", resp.status, text[:2000])
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
        async with session.post(url, headers=headers, json=payload, timeout=ELEVEN_TTS_TIMEOUT) as resp:
            data = await resp.read()
            if resp.status != 200:
                log.error("ElevenLabs TTS failed %s: %s", resp.status, (data[:1000] if data else b""))
                return None
            async with aiofiles.open(out_mp3_path, "wb") as fh:
                await fh.write(data)
            return out_mp3_path

# --- Per-connection state ---
# CONNS keyed by ws_id -> meta dict:
# { "buffer": bytearray(), "call_sid": str, "sample_rate": int, "ws": ws, "lock": asyncio.Lock(),
#   "last_media_ts": float, "playback_task": Task|None, "interrupt": asyncio.Event() }
CONNS = {}

# Helper: process small buffer -> transcribe -> llm -> tts -> stream back
async def handle_segment_and_respond(ws_id: str):
    meta = CONNS.get(ws_id)
    if not meta:
        return
    # short-circuit if playback is currently being interrupted
    lock = meta["lock"]
    if lock.locked():
        return
    async with lock:
        try:
            buf = bytes(meta["buffer"])
            meta["buffer"].clear()
            if len(buf) < 1600:  # too small (less than ~0.1s) -> ignore
                log.debug("Segment too small (%d bytes) for %s", len(buf), ws_id)
                return

            call_sid = meta.get("call_sid") or ws_id
            sample_rate = meta.get("sample_rate", 8000)
            ts = int(time.time())
            raw_file = TMP_DIR / f"{ws_id}_{ts}.s16le"
            wav_file = TMP_DIR / f"{ws_id}_{ts}.wav"
            mp3_file = STATIC_DIR / f"{ws_id}_{ts}.mp3"

            raw_file.write_bytes(buf)

            # convert raw to wav (executor)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, raw_bytes_to_wav, str(raw_file), str(wav_file), sample_rate)

            # transcribe
            transcript = await transcribe_wav_with_openai(str(wav_file))
            if not transcript:
                log.info("No transcript for %s (len=%d)", ws_id, len(buf))
                return
            transcript = transcript.strip()
            log.info("Transcript for call %s: %s", call_sid, transcript[:400])

            # filter too-short/noisy transcripts
            words = transcript.split()
            if len(words) < 1:
                log.info("Ignoring short transcript for %s", ws_id)
                return

            # Ask LLM
            reply = await ask_llm(transcript)
            if not reply:
                log.warning("LLM returned empty for %s", ws_id)
                return
            log.info("LLM reply for %s: %s", ws_id, reply[:400])

            # generate TTS mp3
            out_mp3 = await eleven_tts_to_mp3(reply, str(mp3_file))
            if not out_mp3:
                log.warning("TTS failed for %s", ws_id)
                return

            # convert mp3 -> raw s16le for Twilio
            raw_for_twilio = TMP_DIR / f"{ws_id}_{ts}_twilio.s16le"
            await loop.run_in_executor(None, mp3_to_twilio_raw, str(mp3_file), str(raw_for_twilio), sample_rate)

            # create playback task and stream frames (supports interrupt via interrupt Event)
            interrupt = meta["interrupt"]
            # cancel any running playback task first
            old_task = meta.get("playback_task")
            if old_task and not old_task.done():
                # signal interrupt and wait shortly for it to stop
                meta["interrupt"].set()
                try:
                    await asyncio.wait_for(old_task, timeout=1.0)
                except Exception:
                    # if it didn't stop, we proceed; playback task checks interrupt periodically
                    pass
                meta["interrupt"].clear()

            # playback coroutine
            async def playback():
                ws_obj = meta.get("ws")
                if not ws_obj or ws_obj.closed:
                    log.warning("WS closed before playback for %s", ws_id)
                    return
                try:
                    for payload_b64 in build_twilio_media_payload_from_raw(str(raw_for_twilio), chunk_size=3200):
                        if interrupt.is_set() or ws_obj.closed:
                            log.info("Playback interrupted for %s", ws_id)
                            break
                        msg = {"event": "media", "media": {"payload": payload_b64}}
                        try:
                            await ws_obj.send_str(json.dumps(msg))
                        except Exception as e:
                            log.warning("Send frame error for %s: %s", ws_id, str(e))
                            break
                        await asyncio.sleep(0.02)
                finally:
                    # Try send event "stop" or nothing; Twilio will manage call lifecycle
                    log.info("Playback finished/stopped for %s", ws_id)

            task = asyncio.create_task(playback())
            meta["playback_task"] = task
            # do not await; allow playback to run concurrently while server continues to accept media
            # save transcript & reply
            try:
                tfn = STATIC_DIR / f"{ws_id}_{ts}.txt"
                async with aiofiles.open(tfn, "w", encoding="utf-8") as tfh:
                    await tfh.write(f"CALL_SID: {call_sid}\n\nTRANSCRIPT:\n{transcript}\n\nLLM_REPLY:\n{reply}\n")
            except Exception:
                log.exception("Failed to write transcript for %s", ws_id)
            log.info("Started playback task for %s", ws_id)

        except Exception:
            log.exception("Error in handle_segment_and_respond for %s", ws_id)

# --- Web handlers ---
routes = web.RouteTableDef()

@routes.get("/health")
async def handle_health(request):
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})

@routes.get("/ws")
async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws_id = str(int(time.time()*1000)) + "_" + str(id(ws))
    CONNS[ws_id] = {
        "buffer": bytearray(),
        "call_sid": None,
        "sample_rate": 8000,
        "ws": ws,
        "lock": asyncio.Lock(),
        "last_media_ts": time.time(),
        "playback_task": None,
        "interrupt": asyncio.Event()
    }
    meta = CONNS[ws_id]
    log.info("New Twilio WS connected: %s", ws_id)

    # Send simple connected ack (helps Twilio clients if needed)
    try:
        await ws.send_str(json.dumps({"event": "connected"}))
    except Exception:
        # non-fatal; continue
        pass

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
                    meta["call_sid"] = call_sid
                    meta["sample_rate"] = int(sr)
                    meta["last_media_ts"] = time.time()
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

                    # Append to buffer
                    meta["buffer"].extend(chunk)
                    meta["last_media_ts"] = time.time()

                    # If we are currently playing back, signal interrupt (barge-in)
                    if meta["playback_task"] and not meta["playback_task"].done():
                        meta["interrupt"].set()
                        # playback will see interrupt flag and stop quickly

                    # If buffer large enough, schedule processing quickly
                    if len(meta["buffer"]) >= BUFFER_FLUSH_BYTES:
                        asyncio.create_task(handle_segment_and_respond(ws_id))

                elif event == "stop":
                    log.info("Stream STOP ws=%s", ws_id)
                    # final flush if any
                    if meta["buffer"]:
                        await handle_segment_and_respond(ws_id)
                    # let Twilio close the connection
                    # ensure playback tasks are cancelled/finished
                    if meta.get("playback_task"):
                        meta["interrupt"].set()
                        try:
                            await asyncio.wait_for(meta["playback_task"], timeout=2.0)
                        except Exception:
                            pass

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
        # cleanup
        log.info("Closing WS %s", ws_id)
        try:
            # interrupt playback if running
            meta = CONNS.get(ws_id)
            if meta:
                if meta.get("playback_task") and not meta["playback_task"].done():
                    meta["interrupt"].set()
                    try:
                        await asyncio.wait_for(meta["playback_task"], timeout=1.0)
                    except Exception:
                        pass
                # close ws if not closed
                try:
                    if not ws.closed:
                        await ws.close()
                except Exception:
                    pass
        except Exception:
            log.exception("Error during WS final cleanup for %s", ws_id)
        CONNS.pop(ws_id, None)
    return ws

# Cleanup loop (remove old files)
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

# App factory & main
def init_app():
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    return app

async def main():
    app = init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Streaming server listening on port %d", PORT)
    asyncio.create_task(cleanup_loop())
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Server crashed on startup")
        raise
