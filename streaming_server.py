# streaming_server.py — Enterprise Twilio Media Streams gateway (aiohttp)
import os
import json
import time
import base64
import logging
import asyncio
import pathlib
import subprocess
import tempfile
import shutil
from typing import Optional
from urllib.parse import parse_qs, unquote

from aiohttp import web, WSMsgType, ClientSession, FormData

# Optional Redis for async processing & pub/sub
try:
    import redis.asyncio as aioredis
except Exception:
    aioredis = None

# -------------------- Config & logging --------------------
PORT = int(os.environ.get("PORT", 8765))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")
REDIS_URL = os.environ.get("REDIS_URL")          # enable async worker mode if set
ASYNC_MODE = os.environ.get("ASYNC_MODE", "true").lower() in ("1", "true", "yes")
TMP_DIR = pathlib.Path("tmp")
TMP_DIR.mkdir(exist_ok=True)
MAX_AI_HISTORY = int(os.environ.get("MAX_AI_HISTORY", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sara-streaming")

# Tunables
SILENCE_TIMEOUT = float(os.environ.get("SILENCE_TIMEOUT", "1.0"))
MIN_BYTES_TO_PROCESS = int(os.environ.get("MIN_BYTES_TO_PROCESS", "2400"))  # mu-law bytes threshold
TTS_CHUNK_MS = int(os.environ.get("TTS_CHUNK_MS", "250"))
PRE_SILENCE_MS = int(os.environ.get("PRE_SILENCE_MS", "200"))
TTS_TIMEOUT = int(os.environ.get("ELEVENLABS_TTS_TIMEOUT", "30"))
FFMPEG_SEMAPHORE_COUNT = int(os.environ.get("FFMPEG_SEMAPHORE_COUNT", "1"))

# Connection store
class ConnState:
    def __init__(self, ws):
        self.ws = ws
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.sample_rate: int = 8000
        self.media_format: str = ""
        self.buffer = bytearray()
        self.last_media_ts = time.time()
        self.processing = False
        self.chunk_counter = 0
        self.conversation_history = []
        self.business_name: str = "the business"
        self.business_type: str = "general"
        self.pubsub_task = None  # redis pubsub subscriber task
        self.ws_id = None

CONNS: dict[str, ConnState] = {}

# FFmpeg helpers (blocking via thread executor)
def ffmpeg_to_wav(input_bytes: bytes, input_format_hint: Optional[str], in_rate: int, out_rate: int = 16000) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if input_format_hint:
        cmd += ["-f", input_format_hint]
    cmd += ["-ar", str(in_rate), "-ac", "1", "-i", "pipe:0", "-ar", str(out_rate), "-ac", "1", "-f", "wav", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg->wav failed: {err.decode(errors='ignore')}")
    return out

def ffmpeg_any_to_mulaw(input_bytes: bytes, input_format_hint: Optional[str] = None, out_rate: int = 8000) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if input_format_hint:
        cmd += ["-f", input_format_hint]
    cmd += ["-i", "pipe:0", "-ar", str(out_rate), "-ac", "1", "-f", "mulaw", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg->mulaw failed: {err.decode(errors='ignore')}")
    return out

# Twilio JSON builders
def twilio_media_msg(stream_sid: str, b64_payload: str) -> str:
    return json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": b64_payload}})

def twilio_mark_msg(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})

# OpenAI Whisper transcription
async def transcribe_with_whisper(wav_bytes: bytes) -> Optional[str]:
    if not OPENAI_API_KEY:
        log.warning("OpenAI key missing; returning mock transcription")
        await asyncio.sleep(0.02)
        return None
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = FormData()
    data.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    data.add_field("model", "whisper-1")
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, data=data, timeout=30) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    return j.get("text", "").strip()
                else:
                    txt = await resp.text()
                    log.error("Whisper error: %s", txt[:400])
                    return None
    except Exception as e:
        log.exception("Whisper request error: %s", e)
        return None

# GPT generation
async def generate_with_gpt(history: list, user_text: str, business_name: str, business_type: str) -> str:
    if not OPENAI_API_KEY:
        await asyncio.sleep(0.01)
        return "I heard you. (OpenAI key missing.)"
    # Load persona system from brains JSON if present
    base_prompt = ""
    try:
        pfile = pathlib.Path("data") / "Sara_SystemPrompt_Production.json"
        if pfile.exists():
            persona_data = json.loads(pfile.read_text(encoding="utf-8"))
            base_prompt = persona_data.get("system_prompt", "") or ""
    except Exception:
        base_prompt = ""
    # Build messages
    messages = []
    if base_prompt:
        messages.append({"role":"system","content": base_prompt})
    # append history
    messages.extend(history or [])
    messages.append({"role":"user","content": user_text})
    payload = {"model": os.getenv("GPT_MODEL", "gpt-5-mini"), "messages": messages, "temperature": float(os.getenv("GPT_TEMPERATURE","0.8")), "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS","300"))}
    try:
        async with ClientSession() as sess:
            async with sess.post(OPENAI_API_URL, headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type":"application/json"}, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    return j["choices"][0]["message"]["content"].strip()
                else:
                    txt = await resp.text()
                    log.error("GPT error: %s", txt[:400])
                    return f"I heard you said: {user_text}"
    except Exception as e:
        log.exception("GPT request error: %s", e)
        return "I'm having trouble thinking right now — can you repeat that?"

# ElevenLabs TTS
async def synthesize_with_elevenlabs(text: str) -> Optional[bytes]:
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        log.warning("ElevenLabs missing; fallback to silence")
        return None
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, json=payload, timeout=TTS_TIMEOUT) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    txt = await resp.text()
                    log.error("ElevenLabs TTS failed: %s", txt[:400])
                    return None
    except Exception as e:
        log.exception("ElevenLabs request error: %s", e)
        return None

# -------------------- Core Twilio handler --------------------
class TwilioMediaHandler:
    def __init__(self, redis_client=None):
        self.redis = redis_client

    async def handle_websocket(self, request):
        log.info("🔍 WebSocket connection received")
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        ws_id = f"ws_{int(time.time()*1000)}_{os.urandom(3).hex()}"
        state = ConnState(ws)
        state.ws_id = ws_id

        # Parse business context from URL parameters
        try:
            query_params = parse_qs(request.rel_url.query_string)
            state.business_name = unquote(query_params.get('business_name', ['the business'])[0])
            state.business_type = unquote(query_params.get('business_type', ['general'])[0])
            log.info("🏢 Business context: %s (%s)", state.business_name, state.business_type)
        except Exception as e:
            log.warning("Could not parse business context: %s", e)

        CONNS[ws_id] = state
        log.info("🎉 WebSocket connected: %s", ws_id)

        # If async mode and redis available, subscribe to pubsub channel for TTS from worker
        if ASYNC_MODE and self.redis:
            state.pubsub_task = asyncio.create_task(self._redis_tts_subscriber(ws_id, state))

        # send initial connected event to Twilio (optional)
        await ws.send_str(json.dumps({"event":"connected","protocol":"Call","version":"1.0.0"}))
        monitor_task = asyncio.create_task(self._monitor_silence(ws_id))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = None
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        log.warning("Non-json text payload")
                        continue
                    ev = data.get("event")
                    if ev == "start":
                        start = data.get("start",{})
                        state.stream_sid = start.get("streamSid")
                        state.call_sid = start.get("callSid")
                        try:
                            state.sample_rate = int(start.get("sampleRate") or state.sample_rate)
                        except Exception:
                            state.sample_rate = state.sample_rate
                        # mediaFormat may vary
                        media_raw = start.get("mediaFormat")
                        if isinstance(media_raw, str):
                            state.media_format = media_raw.lower()
                        elif isinstance(media_raw, dict):
                            state.media_format = (media_raw.get("type") or media_raw.get("name") or "").lower()
                        log.info("🎬 Stream started: streamSid=%s sampleRate=%s mediaFormat=%s", state.stream_sid, state.sample_rate, state.media_format)
                        # Immediately play greeting (non-blocking)
                        asyncio.create_task(self.send_greeting(ws_id))
                    elif ev == "media":
                        await self._handle_media(ws_id, data)
                    elif ev == "mark":
                        log.info("🔖 mark received: %s", data.get("mark"))
                    elif ev == "stop":
                        log.info("⏹ stop received")
                        break
                    else:
                        log.debug("Unhandled event: %s", ev)
                elif msg.type == WSMsgType.ERROR:
                    log.error("WebSocket error: %s", msg.data)
        except Exception as e:
            log.exception("Websocket loop error: %s", e)
        finally:
            monitor_task.cancel()
            if state.pubsub_task:
                state.pubsub_task.cancel()
            CONNS.pop(ws_id, None)
            await ws.close()
            log.info("🔚 Connection closed: %s", ws_id)
        return ws

    async def _handle_media(self, ws_id: str, data: dict):
        state = CONNS.get(ws_id)
        if not state:
            log.warning("Media for unknown ws: %s", ws_id)
            return
        payload = data.get("media", {}).get("payload")
        if not payload:
            return
        try:
            decoded = base64.b64decode(payload)
            state.buffer.extend(decoded)
            state.last_media_ts = time.time()
            # threshold
            if "mulaw" in (state.media_format or ""):
                if len(state.buffer) >= MIN_BYTES_TO_PROCESS and not state.processing:
                    state.processing = True
                    asyncio.create_task(self._process_audio(ws_id))
            else:
                if len(state.buffer) >= MIN_BYTES_TO_PROCESS * 2 and not state.processing:
                    state.processing = True
                    asyncio.create_task(self._process_audio(ws_id))
        except Exception as e:
            log.exception("Failed decoding media payload: %s", e)

    async def _monitor_silence(self, ws_id: str):
        try:
            while True:
                await asyncio.sleep(0.25)
                state = CONNS.get(ws_id)
                if not state:
                    return
                if state.buffer and (time.time() - state.last_media_ts) > SILENCE_TIMEOUT and not state.processing:
                    state.processing = True
                    asyncio.create_task(self._process_audio(ws_id))
        except asyncio.CancelledError:
            return

    async def _process_audio(self, ws_id: str):
        """
        Core processing:
          - convert buffered audio to WAV (16k)
          - transcribe (Whisper)
          - either inline generate GPT + TTS and stream back
            OR enqueue job to Redis for worker to process and publish tts chunks via pubsub
        """
        state = CONNS.get(ws_id)
        if not state:
            return
        try:
            buf = bytes(state.buffer)
            state.buffer.clear()
            log.info("🔊 Processing buffer bytes=%d for ws=%s", len(buf), ws_id)

            # choose ffmpeg hint
            if state.media_format and "mulaw" in state.media_format:
                hint = "mulaw"
                in_rate = state.sample_rate or 8000
            else:
                hint = "s16le"
                in_rate = state.sample_rate or 16000

            loop = asyncio.get_event_loop()
            wav_bytes = await loop.run_in_executor(None, ffmpeg_to_wav, buf, hint, in_rate, 16000)

            transcript = await transcribe_with_whisper(wav_bytes)
            if transcript:
                log.info("🎙 Transcribed: %s", transcript)
            else:
                log.info("No meaningful transcript")

            if transcript and len(transcript.strip()) >= 2:
                # append to history
                state.conversation_history.append({"role":"user","content": transcript})
                state.conversation_history = state.conversation_history[-MAX_AI_HISTORY:]
                if ASYNC_MODE and self.redis:
                    # enqueue job to redis queue for worker
                    job_payload = {
                        "ws_id": state.ws_id,
                        "job_id": f"job_{int(time.time()*1000)}",
                        "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
                        "sample_rate": in_rate,
                        "media_format": hint,
                        "business_name": state.business_name,
                        "business_type": state.business_type,
                        "history": state.conversation_history
                    }
                    # push JSON string onto Redis list (worker will pop)
                    try:
                        await self.redis.rpush("audio_queue", json.dumps(job_payload))
                        log.info("Enqueued audio job for ws=%s", ws_id)
                    except Exception as e:
                        log.exception("Failed to push job to redis: %s", e)
                else:
                    # Inline processing: GPT -> TTS -> stream back
                    reply = await generate_with_gpt(state.conversation_history, transcript, state.business_name, state.business_type)
                    state.conversation_history.append({"role":"assistant","content": reply})
                    state.conversation_history = state.conversation_history[-MAX_AI_HISTORY:]
                    await self._respond_with_tts(state, reply)
            else:
                # If no transcript: play cold call opener
                opener = self._get_cold_call_opener(state.business_name, state.business_type)
                await self._respond_with_tts(state, opener)
        except Exception as e:
            log.exception("Error processing audio: %s", e)
        finally:
            state.processing = False

    def _get_cold_call_opener(self, business_name: str, business_type: str) -> str:
        # Simple generic opener
        return f"Hi, this is Sara Hayes from Noblecom Solutions. I help businesses like {business_name} stop losing customers and book better leads. Do you have a minute?"

    async def send_greeting(self, ws_id: str):
        state = CONNS.get(ws_id)
        if not state:
            return
        # if greetings_map exists, look for file
        try:
            gfile = pathlib.Path("brains") / "greetings_map.json"
            if gfile.exists():
                gm = json.loads(gfile.read_text(encoding="utf-8"))
                entry = gm.get(state.business_type) or gm.get("general") or gm.get("default")
                if entry:
                    # If entry is a local path (static/tts/...) read and stream
                    mp3path = entry.get("greeting") if isinstance(entry, dict) else entry
                    if mp3path and pathlib.Path(mp3path).exists():
                        # convert mp3 to mu-law and stream
                        with open(mp3path, "rb") as fh:
                            mp3_bytes = fh.read()
                        loop = asyncio.get_event_loop()
                        mulaw_bytes = await loop.run_in_executor(None, ffmpeg_any_to_mulaw, mp3_bytes, "mp3", 8000)
                        await self._stream_mulaw_to_twilio(state, mulaw_bytes, chunk_ms=TTS_CHUNK_MS, pre_silence_ms=PRE_SILENCE_MS)
                        return
            # fallback: inline opener TTS
            opener = self._get_cold_call_opener(state.business_name, state.business_type)
            await self._respond_with_tts(state, opener)
        except Exception as e:
            log.exception("send_greeting error: %s", e)

    async def _respond_with_tts(self, state: ConnState, text: str):
        if not state or not state.stream_sid:
            return
        log.info("🔊 Generating TTS (len=%d)", len(text))
        tts_bytes = None
        if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
            try:
                tts_bytes = await synthesize_with_elevenlabs(text)
            except Exception as e:
                log.exception("TTS generation failed inline: %s", e)
                tts_bytes = None

        if not tts_bytes:
            # fallback generate a short tone (wav)
            tts_bytes = ffmpeg_generate_tone_wav(600, 300)
            input_hint = "wav"
        else:
            input_hint = "mp3"

        loop = asyncio.get_event_loop()
        try:
            mulaw_bytes = await loop.run_in_executor(None, ffmpeg_any_to_mulaw, tts_bytes, input_hint, 8000)
            log.info("Converted TTS to mu-law bytes=%d", len(mulaw_bytes))
        except Exception as e:
            log.exception("Failed convert TTS to mu-law: %s", e)
            return

        await self._stream_mulaw_to_twilio(state, mulaw_bytes, chunk_ms=TTS_CHUNK_MS, pre_silence_ms=PRE_SILENCE_MS)

    async def _stream_mulaw_to_twilio(self, state: ConnState, mulaw_bytes: bytes, chunk_ms: int = 250, pre_silence_ms: int = 200) -> Optional[str]:
        if not state or not state.stream_sid:
            log.error("No stream_sid; cannot send audio")
            return None
        if pre_silence_ms and pre_silence_ms > 0:
            silence_samples = int(8000 * pre_silence_ms / 1000)
            mulaw_bytes = (b'\xFF' * silence_samples) + mulaw_bytes

        chunk_size = max(1, int(8000 * chunk_ms / 1000))
        mark_name = f"tts-{int(time.time()*1000)}-{os.urandom(3).hex()}"
        total_chunks = len(mulaw_bytes) // chunk_size
        log.info("📦 Streaming %d chunks (size=%d)", total_chunks, chunk_size)

        for i in range(0, len(mulaw_bytes), chunk_size):
            chunk = mulaw_bytes[i:i+chunk_size]
            b64 = base64.b64encode(chunk).decode("ascii")
            msg = twilio_media_msg(state.stream_sid, b64)
            try:
                await state.ws.send_str(msg)
            except Exception as e:
                log.exception("Failed sending media to Twilio: %s", e)
                return None
            await asyncio.sleep(chunk_ms / 1000.0 * 0.9)

        # send mark
        try:
            await state.ws.send_str(twilio_mark_msg(state.stream_sid, mark_name))
            log.info("Sent mark %s for stream %s", mark_name, state.stream_sid)
        except Exception as e:
            log.exception("Failed send mark: %s", e)
            return None
        return mark_name

    async def _redis_tts_subscriber(self, ws_id: str, state: ConnState):
        """
        Subscribe to Redis pubsub channel 'tts_{ws_id}' and relay chunks published by worker to Twilio.
        Worker should publish JSON messages like:
          {"type":"chunk","b64": "...", "index": 0, "is_last": false}
          {"type":"mark","name":"tts-..."}
        """
        try:
            if not self.redis:
                return
            sub = self.redis.pubsub()
            await sub.subscribe(f"tts_{ws_id}")
            log.info("Subscribed to tts_%s", ws_id)
            async for msg in sub.listen():
                if msg is None:
                    await asyncio.sleep(0.01)
                    continue
                if isinstance(msg, dict) and msg.get("type") == "message":
                    try:
                        payload = json.loads(msg.get("data", "{}"))
                    except Exception:
                        continue
                    if payload.get("type") == "chunk":
                        b64 = payload.get("b64")
                        # stream directly as Twilio 'media'
                        try:
                            await state.ws.send_str(twilio_media_msg(state.stream_sid, b64))
                        except Exception as e:
                            log.exception("Failed to forward chunk to Twilio: %s", e)
                    elif payload.get("type") == "mark":
                        mark_name = payload.get("name")
                        try:
                            await state.ws.send_str(twilio_mark_msg(state.stream_sid, mark_name))
                        except Exception as e:
                            log.exception("Failed to forward mark: %s", e)
                    elif payload.get("type") == "error":
                        log.error("Worker error for ws %s: %s", ws_id, payload.get("msg"))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("Redis subscriber error: %s", e)

# Utilities: generate tone
def ffmpeg_generate_tone_wav(freq_hz: int, duration_ms: int) -> bytes:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    dur = max(0.05, duration_ms / 1000.0)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"sine=frequency={freq_hz}:duration={dur}",
           "-ar", "22050", "-ac", "1", tmp.name]
    subprocess.run(cmd, capture_output=True)
    with open(tmp.name, "rb") as f:
        data = f.read()
    try:
        os.unlink(tmp.name)
    except Exception:
        pass
    return data

# -------------------- Web app setup --------------------
app_web = web.Application()

# create Redis client if available and requested
redis_client = None
if REDIS_URL and aioredis:
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        log.info("✅ Redis async client configured")
    except Exception as e:
        log.exception("Failed to init redis client: %s", e)
        redis_client = None
else:
    if ASYNC_MODE:
        log.warning("ASYNC_MODE enabled but redis.asyncio not available or REDIS_URL missing; falling back to inline processing")
        redis_client = None

handler = TwilioMediaHandler(redis_client)
app_web.router.add_get('/ws', handler.handle_websocket)
app_web.router.add_get('/health', lambda r: web.json_response({"status":"ok","active": len(CONNS)}))

# Startup logs
if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %s", PORT)
    log.info("Config: OpenAI=%s ElevenLabs=%s Voice=%s FFmpeg=%s AsyncMode=%s",
             bool(OPENAI_API_KEY), bool(ELEVENLABS_API_KEY), ELEVENLABS_VOICE_ID or "MISSING", shutil.which("ffmpeg") is not None, ASYNC_MODE)
    web.run_app(app_web, host='0.0.0.0', port=PORT)
