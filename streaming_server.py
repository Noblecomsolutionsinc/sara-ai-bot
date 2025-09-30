# file: streaming_server.py
"""
Streaming server for Twilio Media Streams (aiohttp).
Merged & fixed:
 - Proper handling of incoming mediaFormat (mulaw vs s16le)
 - Convert ElevenLabs output -> μ-law 8k raw and stream to Twilio
 - Send Twilio 'mark' after playback and wait for Twilio 'mark' acknowledgement
 - Fix GPT param: use max_completion_tokens
 - Add pre-silence to improve Twilio buffering
Usage: drop in place of existing streaming_server.py and restart.
Requirements: ffmpeg on PATH, aiohttp, aiofiles, requests (optional), python 3.10+
"""

import os
import json
import time
import base64
import logging
import asyncio
import pathlib
import subprocess
import tempfile
from typing import Optional

from aiohttp import web, WSMsgType, ClientSession, FormData

# -------------------- Config & logging --------------------
PORT = int(os.environ.get("PORT", 5001))
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")

TMP_DIR = pathlib.Path("tmp")
TMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sara-streaming")

# -------------------- Tunables --------------------
SILENCE_TIMEOUT = 1.0            # seconds of inactivity -> flush buffer
MIN_BYTES_TO_PROCESS = 8000      # ~1s of mu-law @8k (1 byte/sample) or ~0.5s @16k pcm (2 bytes/sample)
TTS_CHUNK_MS = 250               # outbound chunk length (ms)
PRE_SILENCE_MS = 80              # pre-roll silence for Twilio buffering (ms)
MAX_AI_HISTORY = 6
TTS_TIMEOUT = 30                 # seconds for TTS HTTP call

# -------------------- Per-connection state --------------------
class ConnState:
    def __init__(self, ws):
        self.ws = ws
        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.sample_rate: int = 8000
        self.media_format: str = ""         # e.g. "audio/x-mulaw;rate=8000"
        self.buffer = bytearray()           # incoming raw bytes (as received)
        self.last_media_ts = time.time()
        self.processing = False
        self.chunk_counter = 0
        self.conversation_history = []      # lightweight history
        self.mark_waiters = {}              # mark_name -> Future

CONNS: dict[str, ConnState] = {}

# -------------------- ffmpeg helpers (blocking execs) --------------------
def ffmpeg_to_wav(input_bytes: bytes, input_format_hint: Optional[str], in_rate: int, out_rate: int = 16000) -> bytes:
    """
    Convert raw incoming audio (mulaw or s16le) to WAV pcm16 @ out_rate.
    input_format_hint: 'mulaw' or 's16le' or None.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if input_format_hint:
        cmd += ["-f", input_format_hint]
    # specify input sample rate and channel
    cmd += ["-ar", str(in_rate), "-ac", "1", "-i", "pipe:0", "-ar", str(out_rate), "-ac", "1", "-f", "wav", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg->wav failed: {err.decode(errors='ignore')}")
    return out

def ffmpeg_any_to_mulaw(input_bytes: bytes, input_hint: Optional[str] = None, out_rate: int = 8000) -> bytes:
    """
    Convert arbitrary audio (mp3/wav/pcm) to raw mu-law @ out_rate (no headers).
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if input_hint:
        cmd += ["-f", input_hint]
    cmd += ["-i", "pipe:0", "-ar", str(out_rate), "-ac", "1", "-f", "mulaw", "pipe:1"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate(input=input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg->mulaw failed: {err.decode(errors='ignore')}")
    return out

def ffmpeg_file_to_mulaw(in_path: str, out_path: str, out_rate: int = 8000) -> None:
    """
    Convert file (mp3/wav) to mu-law raw file at out_path.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", in_path, "-ar", str(out_rate), "-ac", "1", "-f", "mulaw", out_path]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg file->mulaw failed: {proc.stderr.decode(errors='ignore')}")

# -------------------- Twilio message builders --------------------
def twilio_media_msg(stream_sid: str, b64_payload: str) -> str:
    return json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": b64_payload}})

def twilio_mark_msg(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})

# -------------------- AI & TTS helpers --------------------
async def transcribe_with_whisper(wav_bytes: bytes) -> Optional[str]:
    """
    Call OpenAI Whisper transcription endpoint (multipart/form-data).
    Falls back to a test string if API key missing.
    """
    if not OPENAI_API_KEY:
        log.warning("OpenAI key missing - using mock transcription")
        await asyncio.sleep(0.05)
        return "This is a test transcription. Configure OPENAI_API_KEY to enable real transcriptions."

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
                    text = await resp.text()
                    log.error("Whisper transcription failed: %s", text[:500])
                    return None
    except Exception as e:
        log.exception("Whisper call error: %s", e)
        return None

async def generate_with_gpt(history: list, user_text: str) -> str:
    """
    Call GPT-5-mini chat completions. Uses max_completion_tokens per model requirement.
    """
    if not OPENAI_API_KEY:
        await asyncio.sleep(0.05)
        return "I heard you. (Enable OpenAI key for full responses.)"

    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": (
            "You are Sara Hayes, a friendly phone assistant. Keep responses short (1-2 sentences), warm and professional."
        )}
    ]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": "gpt-5-mini",
        "messages": messages,
        "temperature": 0.7,
        "max_completion_tokens": 150
    }

    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, json=payload, timeout=30) as resp:
                if resp.status == 200:
                    j = await resp.json()
                    return j["choices"][0]["message"]["content"].strip()
                else:
                    txt = await resp.text()
                    log.error("GPT call failed: %s", txt[:500])
                    return f"I understand you said: {user_text}. How can I help?"
    except Exception as e:
        log.exception("GPT call error: %s", e)
        return "I'm having trouble thinking right now — can you repeat that?"

async def synthesize_with_elevenlabs(text: str) -> Optional[bytes]:
    """
    Call ElevenLabs TTS streaming (returns bytes). If missing keys, return None.
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        log.warning("ElevenLabs key/voice missing - skipping real TTS")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json"
    }
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    try:
        async with ClientSession() as sess:
            async with sess.post(url, headers=headers, json=payload, timeout=TTS_TIMEOUT) as resp:
                if resp.status == 200:
                    mp3 = await resp.read()
                    return mp3
                else:
                    txt = await resp.text()
                    log.error("ElevenLabs TTS failed: %s", txt[:500])
                    return None
    except Exception as e:
        log.exception("ElevenLabs error: %s", e)
        return None

# -------------------- Core server handler --------------------
class TwilioMediaHandler:
    def __init__(self):
        pass

    async def handle_websocket(self, request):
        log.info("🔍 WebSocket connection received")
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        ws_id = f"conn_{int(time.time()*1000)}"
        state = ConnState(ws)
        CONNS[ws_id] = state
        log.info("🎉 WebSocket connected: %s", ws_id)

        # send connected event
        await ws.send_str(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        log.info("✅ Sent 'connected' event")

        # background task: monitor silence to flush small buffers
        monitor_task = asyncio.create_task(self._monitor_silence(ws_id))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        log.warning("Received non-json text message")
                        continue

                    ev = data.get("event")
                    if ev == "start":
                        start = data.get("start", {})
                        state.stream_sid = start.get("streamSid")
                        state.call_sid = start.get("callSid")
                        # Twilio sometimes sends sampleRate as int or str
                        state.sample_rate = int(start.get("sampleRate") or state.sample_rate)
                        state.media_format = (start.get("mediaFormat") or "").lower()
                        log.info("🎬 Stream started - stream_sid: %s mediaFormat: %s sampleRate: %s",
                                 state.stream_sid, state.media_format, state.sample_rate)
                        # send greeting immediately
                        asyncio.create_task(self.send_immediate_greeting(ws_id))
                    elif ev == "media":
                        await self._handle_media(ws_id, data)
                    elif ev == "mark":
                        mark = data.get("mark", {})
                        name = mark.get("name")
                        log.info("🔖 Received mark from Twilio: %s", name)
                        fut = state.mark_waiters.pop(name, None)
                        if fut and not fut.done():
                            fut.set_result(True)
                    elif ev == "stop":
                        log.info("⏹️ Stop event received")
                        break
                    else:
                        log.debug("Unhandled event: %s", ev)
                elif msg.type == WSMsgType.ERROR:
                    log.error("Websocket error: %s", msg.data)
        except Exception as e:
            log.exception("Websocket loop error: %s", e)
        finally:
            monitor_task.cancel()
            log.info("🔚 Closing connection: %s", ws_id)
            CONNS.pop(ws_id, None)
            await ws.close()
        return ws

    async def _handle_media(self, ws_id: str, data: dict):
        state = CONNS.get(ws_id)
        if not state:
            log.warning("Media for unknown connection: %s", ws_id)
            return
        payload = data.get("media", {}).get("payload")
        if not payload:
            return
        try:
            decoded = base64.b64decode(payload)
            state.buffer.extend(decoded)
            state.last_media_ts = time.time()
            # decide threshold: if media_format contains mulaw, 1 byte/sample
            if not state.processing:
                # minimum bytes depending on assumed format
                if ("mulaw" in state.media_format) and len(state.buffer) >= MIN_BYTES_TO_PROCESS:
                    state.processing = True
                    asyncio.create_task(self._process_audio(ws_id))
                elif ("mulaw" not in state.media_format) and len(state.buffer) >= MIN_BYTES_TO_PROCESS * 2:
                    # s16le is 2 bytes per sample approx, require larger buffer
                    state.processing = True
                    asyncio.create_task(self._process_audio(ws_id))
        except Exception as e:
            log.exception("Failed decoding media payload: %s", e)

    async def _monitor_silence(self, ws_id: str):
        """
        If there's residual small buffer and silence for SILENCE_TIMEOUT, flush it to process.
        """
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
        Convert incoming bytes to WAV (Whisper), transcribe, call GPT and TTS, then stream response.
        """
        state = CONNS.get(ws_id)
        if not state:
            return
        try:
            buf = bytes(state.buffer)
            state.buffer.clear()
            log.info("🔊 Processing audio buffer: %d bytes", len(buf))

            # deduce input hint
            if state.media_format and "mulaw" in state.media_format:
                hint = "mulaw"
                in_rate = state.sample_rate or 8000
            else:
                hint = "s16le"
                in_rate = state.sample_rate or 16000

            # Convert to WAV for Whisper (blocking ffmpeg)
            loop = asyncio.get_running_loop()
            wav_bytes = await loop.run_in_executor(None, ffmpeg_to_wav, buf, hint, in_rate, 16000)

            # Transcribe
            transcript = await transcribe_with_whisper(wav_bytes)
            if transcript:
                log.info("🎙️ User said: %s", transcript)
            else:
                log.info("No speech detected or transcript too short")

            if transcript and len(transcript.strip()) >= 3:
                # get AI response
                reply = await generate_with_gpt(state.conversation_history, transcript)
                state.conversation_history.append({"role": "user", "content": transcript})
                state.conversation_history.append({"role": "assistant", "content": reply})
                state.conversation_history = state.conversation_history[-MAX_AI_HISTORY:]
                log.info("🤖 Sara responds: %s", reply)
                await self._respond_with_tts(ws_id, reply)
            else:
                # prompt if no speech recognized
                if not any(m.get("role") == "assistant" for m in state.conversation_history[-1:]):
                    prompt = "I'm listening. Please tell me how I can help you today."
                    await self._respond_with_tts(ws_id, prompt)
        except Exception as e:
            log.exception("❌ Error in speech processing: %s", e)
        finally:
            state.processing = False

    async def send_immediate_greeting(self, ws_id: str):
        await self._respond_with_tts(ws_id, "Hello! I'm Sara Hayes. How can I help you today?")

    async def _respond_with_tts(self, ws_id: str, text: str):
        """
        Get TTS bytes (ElevenLabs or fallback), convert to mu-law raw, chunk and send to Twilio,
        then send a mark and wait for Twilio mark acknowledgement.
        """
        state = CONNS.get(ws_id)
        if not state:
            return
        log.info("🔊 Converting to speech: '%s'", text[:120])

        # attempt ElevenLabs
        tts_bytes = None
        input_hint = None
        if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
            tts_bytes = await synthesize_with_elevenlabs(text)
            # ElevenLabs returns mp3-like bytes (we treat as unknown; file hint None)
            input_hint = None

        # fallback: generate a short tone WAV (so we can exercise path)
        if not tts_bytes:
            log.info("🔧 Using fallback tone for TTS (debug)")
            tts_bytes = ffmpeg_generate_tone_wav(600, 700)  # returns WAV bytes
            input_hint = "wav"

        # Convert TTS bytes -> mu-law raw bytes (blocking conversion)
        loop = asyncio.get_running_loop()
        try:
            # If we have a file-like mp3/wav bytes and we don't know format, ffmpeg can detect.
            mulaw_bytes = await loop.run_in_executor(None, ffmpeg_any_to_mulaw, tts_bytes, input_hint, 8000)
        except Exception as e:
            log.exception("Failed to convert TTS to mu-law: %s", e)
            return

        # Stream mulaw bytes to Twilio
        mark_name = await self._stream_mulaw_to_twilio(state, mulaw_bytes, chunk_ms=TTS_CHUNK_MS, pre_silence_ms=PRE_SILENCE_MS)
        # Wait (timeout) for Twilio mark acknowledgement
        if mark_name:
            fut = asyncio.get_running_loop().create_future()
            state.mark_waiters[mark_name] = fut
            try:
                await asyncio.wait_for(fut, timeout=10.0)
                log.info("Playback confirmed by Twilio (mark=%s)", mark_name)
            except asyncio.TimeoutError:
                log.warning("Timed out waiting for Twilio mark: %s", mark_name)
                state.mark_waiters.pop(mark_name, None)

    async def _stream_mulaw_to_twilio(self, state: ConnState, mulaw_bytes: bytes, chunk_ms: int = 250, pre_silence_ms: int = 80) -> Optional[str]:
        """
        Chunk mu-law bytes and send Twilio media messages, then send mark.
        """
        if not state.stream_sid:
            log.error("No stream_sid for connection; cannot send audio")
            return None

        # pre-silence to assist Twilio buffering (mu-law silence = 0xFF)
        if pre_silence_ms and pre_silence_ms > 0:
            silence_samples = int(8000 * pre_silence_ms / 1000)
            mulaw_bytes = (b'\xFF' * silence_samples) + mulaw_bytes

        chunk_size = max(1, int(8000 * chunk_ms / 1000))  # bytes per chunk (1 byte/sample)
        mark_name = f"tts-{int(time.time()*1000)}-{os.urandom(3).hex()}"

        # send chunks
        for idx in range(0, len(mulaw_bytes), chunk_size):
            chunk = mulaw_bytes[idx:idx + chunk_size]
            b64 = base64.b64encode(chunk).decode("ascii")
            msg = twilio_media_msg(state.stream_sid, b64)
            try:
                await state.ws.send_str(msg)
                if idx == 0:
                    # debug prefix
                    prefix = base64.b64encode(chunk[:6]).decode("ascii")
                    log.info("Outbound chunk[0] size=%d b64_prefix=%s", len(chunk), prefix)
            except Exception as e:
                log.exception("❌ Failed to send media chunk to Twilio: %s", e)
                return None
            # pace slightly under real time for smooth playback
            await asyncio.sleep(chunk_ms / 1000.0 * 0.9)

        # send mark at end of playback
        try:
            await state.ws.send_str(twilio_mark_msg(state.stream_sid, mark_name))
            log.info("Sent mark %s for stream %s", mark_name, state.stream_sid)
        except Exception as e:
            log.exception("Failed to send mark to Twilio: %s", e)
            return None

        return mark_name

# -------------------- Utilities --------------------
def ffmpeg_generate_tone_wav(freq_hz: int, duration_ms: int) -> bytes:
    """
    Generate a WAV sine tone (blocking) to exercise path. Returns WAV bytes.
    """
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
handler = TwilioMediaHandler()
app = web.Application()
app.router.add_get('/ws', handler.handle_websocket)
app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "active": len(CONNS)}))

if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %d", PORT)
    web.run_app(app, host='0.0.0.0', port=PORT)
