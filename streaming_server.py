# File: streaming_server.py
"""
Twilio Media Streams Protocol-Compliant Server
Fixed for Error 31951 - Strict protocol adherence
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
import aiohttp

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming-realtime")

# --- Config ---
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
ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 5001))

# --- Directory setup ---
DATA_DIR = pathlib.Path("data")
STATIC_DIR = pathlib.Path("static")
TMP_DIR = pathlib.Path("tmp")
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)

# --- Twilio Protocol Constants ---
class TwilioProtocol:
    PROTOCOL_VERSION = "1.0.0"
    PROTOCOL_NAME = "Call"
    
    @staticmethod
    def create_connected_event():
        """Create exactly the connected event Twilio expects"""
        return {
            "event": "connected",
            "protocol": TwilioProtocol.PROTOCOL_NAME,
            "version": TwilioProtocol.PROTOCOL_VERSION
        }
    
    @staticmethod
    def create_media_event(stream_sid, payload_b64, track="outbound", chunk=1, timestamp=None):
        """Create EXACT media event structure Twilio requires"""
        if timestamp is None:
            timestamp = str(int(time.time() * 1000))
            
        return {
            "event": "media",
            "streamSid": stream_sid,
            "media": {
                "track": track,
                "chunk": str(chunk),
                "timestamp": timestamp,
                "payload": payload_b64
            }
        }
    
    @staticmethod
    def create_mark_event(stream_sid, name):
        """Create mark event for synchronization"""
        return {
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {
                "name": name
            }
        }

# --- Persona loading (unchanged) ---
PERSONA_FILES = [
    "Sara_Opening.json", "Sara_Objections.json", "Sara_KnowledgeBase.json",
    "Sara_CallFlow.json", "Sara_Playbook.json", "Sara_SystemPrompt_Production.json"
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

sp = PERSONAS.get("Sara_SystemPrompt_Production.json")
if isinstance(sp, dict) and "prompt" in sp:
    SYSTEM_PROMPT = sp["prompt"]
else:
    SYSTEM_PROMPT = sp if isinstance(sp, str) else json.dumps(sp, ensure_ascii=False)

# --- FFmpeg setup ---
def ensure_ffmpeg():
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path is None:
        log.error("❌ ffmpeg not found on PATH")
        raise SystemExit("ffmpeg required")
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        log.info("✅ ffmpeg found: %s", result.stdout.split('\n')[0])
    except Exception as e:
        log.error("❌ ffmpeg test failed: %s", e)
        raise SystemExit("ffmpeg required")

# Audio conversion functions (unchanged)
def raw_bytes_to_wav(raw_path: str, wav_path: str, sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", raw_path, "-ar", "16000", "-ac", "1", wav_path
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {proc.stderr.decode()[:2000]}")
    return wav_path

def mp3_to_twilio_raw(mp3_path: str, out_raw_path: str, out_sample_rate: int = 8000):
    cmd = [
        "ffmpeg", "-y", "-i", mp3_path,
        "-f", "s16le", "-ar", str(out_sample_rate), "-ac", "1", out_raw_path
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg mp3->raw failed: {proc.stderr.decode()[:2000]}")
    return out_raw_path

def build_twilio_media_payload_from_raw(raw_path: str, chunk_size: int = 3200):
    with open(raw_path, "rb") as fh:
        chunk_count = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            yield base64.b64encode(chunk).decode("ascii"), chunk_count
            chunk_count += 1

# --- API functions (unchanged) ---
async def transcribe_wav_with_openai(wav_path: str, model: str = "whisper-1", timeout: int = 60):
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    form = aiohttp.FormData()
    with open(wav_path, "rb") as fh:
        form.add_field("file", fh, filename=pathlib.Path(wav_path).name, content_type="audio/wav")
        form.add_field("model", model)
        async with ClientSession() as session:
            async with session.post(url, headers=headers, data=form, timeout=timeout) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error("OpenAI transcription failed %s: %s", resp.status, text[:2000])
                    return None
                j = await resp.json()
                return j.get("text")

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
        "max_tokens": 1000
    }
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.error("LLM request failed %s: %s", resp.status, text[:2000])
                return None
            j = await resp.json()
            try:
                return j["choices"][0]["message"]["content"].strip()
            except Exception:
                log.error("Unexpected LLM response: %s", j)
                return None

async def eleven_tts_to_mp3(text: str, out_mp3_path: str):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.read()
            if resp.status != 200:
                log.error("ElevenLabs TTS failed %s: %s", resp.status, (data[:1000] if data else b""))
                return None
            async with aiofiles.open(out_mp3_path, "wb") as fh:
                await fh.write(data)
            return out_mp3_path

# --- Connection Management ---
CONNS = {}

async def handle_segment_and_respond(ws_id: str):
    meta = CONNS.get(ws_id)
    if not meta:
        return
        
    lock = meta["lock"]
    if lock.locked():
        return
        
    async with lock:
        try:
            buf = bytes(meta["buffer"])
            meta["buffer"].clear()
            if len(buf) < 1600:
                return

            stream_sid = meta.get("stream_sid")
            if not stream_sid:
                log.error("No stream_sid for response in %s", ws_id)
                return

            call_sid = meta.get("call_sid") or ws_id
            sample_rate = meta.get("sample_rate", 8000)
            ts = int(time.time())
            
            raw_file = TMP_DIR / f"{ws_id}_{ts}.s16le"
            wav_file = TMP_DIR / f"{ws_id}_{ts}.wav"
            mp3_file = STATIC_DIR / f"{ws_id}_{ts}.mp3"

            raw_file.write_bytes(buf)

            # Convert and transcribe
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, raw_bytes_to_wav, str(raw_file), str(wav_file), sample_rate)

            transcript = await transcribe_wav_with_openai(str(wav_file))
            if not transcript:
                return
            transcript = transcript.strip()
            log.info("Transcript: %s", transcript[:400])

            words = transcript.split()
            if len(words) < 1:
                return

            # Get LLM response
            reply = await ask_llm(transcript)
            if not reply:
                return
            log.info("LLM reply: %s", reply[:400])

            # Generate TTS
            out_mp3 = await eleven_tts_to_mp3(reply, str(mp3_file))
            if not out_mp3:
                return

            # Convert to Twilio format
            raw_for_twilio = TMP_DIR / f"{ws_id}_{ts}_twilio.s16le"
            await loop.run_in_executor(None, mp3_to_twilio_raw, str(mp3_file), str(raw_for_twilio), sample_rate)

            # Handle playback interruption
            interrupt = meta["interrupt"]
            old_task = meta.get("playback_task")
            if old_task and not old_task.done():
                meta["interrupt"].set()
                try:
                    await asyncio.wait_for(old_task, timeout=1.0)
                except Exception:
                    pass
                meta["interrupt"].clear()

            # Playback with PROTOCOL-COMPLIANT messages
            async def playback():
                ws_obj = meta.get("ws")
                stream_sid = meta.get("stream_sid")
                if not ws_obj or ws_obj.closed or not stream_sid:
                    return
                    
                try:
                    chunk_count = 0
                    start_time = time.time()
                    
                    for payload_b64, chunk_num in build_twilio_media_payload_from_raw(str(raw_for_twilio), chunk_size=3200):
                        if interrupt.is_set() or ws_obj.closed:
                            break
                            
                        # ✅ PROTOCOL-COMPLIANT media message
                        timestamp = str(int((time.time() - start_time) * 1000))
                        media_msg = TwilioProtocol.create_media_event(
                            stream_sid=stream_sid,
                            payload_b64=payload_b64,
                            track="outbound", 
                            chunk=chunk_num,
                            timestamp=timestamp
                        )
                        
                        try:
                            await ws_obj.send_str(json.dumps(media_msg))
                            # Log first few messages to verify structure
                            if chunk_num < 3:
                                log.info("✅ Sent protocol-compliant media message: %s", 
                                        {k: v for k, v in media_msg.items() if k != 'media'})
                                log.info("Media structure: %s", media_msg['media'].keys())
                        except Exception as e:
                            log.warning("Send frame error: %s", str(e))
                            break
                        await asyncio.sleep(0.02)
                        chunk_count += 1
                        
                except Exception as e:
                    log.error("Playback error: %s", e)
                finally:
                    log.info("Playback finished for %s", ws_id)

            task = asyncio.create_task(playback())
            meta["playback_task"] = task
            
        except Exception as e:
            log.error("Error in handle_segment_and_respond: %s", e)

# --- WebSocket Handler - COMPLETELY REWRITTEN ---
@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except Exception as e:
        log.error("Unhandled exception: %s", e)
        return web.json_response({"error": "Internal server error"}, status=500)

async def ws_handler(request):
    log.info("🔍 WebSocket connection attempt received")
    
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    ws_id = f"{int(time.time()*1000)}_{id(ws)}"
    CONNS[ws_id] = {
        "buffer": bytearray(),
        "call_sid": None,
        "stream_sid": None,
        "sample_rate": 8000,
        "ws": ws,
        "lock": asyncio.Lock(),
        "last_media_ts": time.time(),
        "playback_task": None,
        "interrupt": asyncio.Event()
    }
    meta = CONNS[ws_id]
    
    log.info("🎉 New Twilio WS connected: %s", ws_id)

    try:
        # ✅ Send EXACT connected event Twilio expects
        connected_msg = TwilioProtocol.create_connected_event()
        await ws.send_str(json.dumps(connected_msg))
        log.info("📞 Sent Twilio 'connected' event: %s", connected_msg)

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    event_type = data.get("event")
                    
                    if event_type == "start":
                        start_data = data.get("start", {})
                        stream_sid = start_data.get("streamSid")
                        call_sid = start_data.get("callSid")
                        sample_rate = start_data.get("sampleRate", 8000)
                        
                        if not stream_sid:
                            log.error("❌ No streamSid in start event!")
                            continue
                            
                        meta["stream_sid"] = stream_sid
                        meta["call_sid"] = call_sid
                        meta["sample_rate"] = int(sample_rate)
                        
                        log.info("🎬 Stream START: stream_sid=%s, call_sid=%s, sample_rate=%s", 
                                stream_sid, call_sid, sample_rate)
                        
                        # ✅ Send initial protocol-compliant media
                        silence_payload = base64.b64encode(b"\x00" * 320).decode("ascii")
                        initial_media = TwilioProtocol.create_media_event(
                            stream_sid=stream_sid,
                            payload_b64=silence_payload,
                            track="outbound",
                            chunk=0,
                            timestamp="0"
                        )
                        await ws.send_str(json.dumps(initial_media))
                        log.info("🔊 Sent initial media with streamSid: %s", stream_sid)

                    elif event_type == "media":
                        media_data = data.get("media", {})
                        payload = media_data.get("payload")
                        
                        if payload and meta.get("stream_sid"):
                            try:
                                chunk = base64.b64decode(payload)
                                meta["buffer"].extend(chunk)
                                
                                if len(meta["buffer"]) >= 16000:  # ~1 second of audio
                                    asyncio.create_task(handle_segment_and_respond(ws_id))
                                    
                            except Exception as e:
                                log.error("Failed to decode media: %s", e)

                    elif event_type == "stop":
                        log.info("⏹️ Stream STOP received")
                        break
                        
                except json.JSONDecodeError as e:
                    log.error("❌ Invalid JSON received: %s", e)
                    continue
                except Exception as e:
                    log.error("❌ Error processing message: %s", e)
                    continue
                    
    except Exception as e:
        log.error("❌ WebSocket error: %s", e)
    finally:
        log.info("🔚 Closing WebSocket: %s", ws_id)
        # Cleanup tasks
        if meta.get("playback_task") and not meta["playback_task"].done():
            meta["playback_task"].cancel()
        CONNS.pop(ws_id, None)
        
    return ws

# --- Routes ---
routes = web.RouteTableDef()

@routes.get("/health")
async def handle_health(request):
    return web.json_response({"status": "ok", "time": datetime.utcnow().isoformat()})

@routes.get("/")
async def handle_root(request):
    return web.json_response({"message": "Sara Streaming Server", "status": "active"})

@routes.get("/ws")
async def websocket_route(request):
    return await ws_handler(request)

# --- App Setup ---
def init_app():
    ensure_ffmpeg()
    app = web.Application(middlewares=[error_middleware])
    app.add_routes(routes)
    app.router.add_static("/static", path=str(STATIC_DIR), show_index=False)
    return app

async def cleanup_loop():
    while True:
        try:
            now = datetime.utcnow()
            cutoff = now - timedelta(hours=24)
            for p in list(STATIC_DIR.glob("*")) + list(TMP_DIR.glob("*")):
                try:
                    if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            log.exception("Cleanup loop error")
        await asyncio.sleep(3600)

async def main():
    app = init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("✅ Streaming server listening on port %d", PORT)
    log.info("✅ Twilio protocol compliance: ACTIVE")
    asyncio.create_task(cleanup_loop())
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.error("❌ Server crashed: %s", e)
        raise