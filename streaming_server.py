# File: streaming_server.py
# Minimal WebSocket server for Twilio Media Streams
# Twilio <Stream> will connect here, send audio frames, and we send back audio responses.

import os
import asyncio
import base64
import json
import uuid
import logging
import io
import requests
from dotenv import load_dotenv
from pydub import AudioSegment
import websockets

from sara_brain import SaraBrain
import memory_manager

# ---------------------------
# Setup
# ---------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sara-stream")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TWILIO_MEDIA_PORT = int(os.getenv("TWILIO_MEDIA_PORT", "8765"))

sara_brain = SaraBrain()

# ---------------------------
# Helpers
# ---------------------------

def b64_to_segment(b64_payload: str, sample_rate=8000):
    """Twilio PCM b64 → AudioSegment."""
    raw = base64.b64decode(b64_payload)
    return AudioSegment(
        data=raw,
        sample_width=2,
        frame_rate=sample_rate,
        channels=1
    )

def segment_to_pcm(seg: AudioSegment, sample_rate=8000):
    seg = seg.set_frame_rate(sample_rate).set_channels(1).set_sample_width(2)
    buf = io.BytesIO()
    seg.export(buf, format="raw")
    return buf.getvalue()

def transcribe_audio(seg: AudioSegment):
    """Very simple transcription via OpenAI Whisper (blocking)."""
    wav_io = io.BytesIO()
    seg.export(wav_io, format="wav")
    wav_io.seek(0)
    files = {"file": ("audio.wav", wav_io, "audio/wav")}
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    resp = requests.post("https://api.openai.com/v1/audio/transcriptions",
                         headers=headers, files=files, timeout=30)
    if resp.status_code == 200:
        return resp.json().get("text", "")
    logger.warning("ASR failed: %s %s", resp.status_code, resp.text)
    return ""

def gpt_reply(messages):
    """Ask GPT for structured Sara reply."""
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-5-mini",
        "messages": messages,
        "max_completion_tokens": 400,
        "temperature": 0.3,
        "response_format": {"type": "json_object"}
    }
    r = requests.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers, timeout=30)
    j = r.json()
    raw = j.get("choices", [])[0].get("message", {}).get("content", "")
    try:
        return json.loads(raw)
    except Exception:
        return {"sara_text": raw, "action": None}

def tts_bytes(text: str):
    """ElevenLabs TTS → mp3 bytes."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    r = requests.post(url, json=payload, headers=headers, timeout=30, stream=True)
    r.raise_for_status()
    return r.content

def mp3_to_pcm8k(mp3_bytes: bytes):
    seg = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    seg = seg.set_frame_rate(8000).set_channels(1).set_sample_width(2)
    buf = io.BytesIO()
    seg.export(buf, format="raw")
    return buf.getvalue()

async def send_audio(ws, pcm_bytes, chunk_ms=200):
    """Send PCM audio back to Twilio as base64 frames."""
    bytes_per_ms = (8000 * 2 * 1) // 1000
    chunk_size = bytes_per_ms * chunk_ms
    idx = 0
    while idx < len(pcm_bytes):
        chunk = pcm_bytes[idx: idx + chunk_size]
        b64 = base64.b64encode(chunk).decode("ascii")
        msg = {"event": "media", "media": {"payload": b64, "type": "audio"}}
        await ws.send(json.dumps(msg))
        idx += chunk_size
        await asyncio.sleep(chunk_ms / 1000.0)

# ---------------------------
# Connection handler
# ---------------------------
async def handle_connection(ws, path):
    logger.info("New Twilio connection")
    conv_id = uuid.uuid4().hex[:8]
    buffer = AudioSegment.silent(duration=0)

    try:
        async for raw in ws:
            data = json.loads(raw)
            event = data.get("event")
            if event == "start":
                logger.info("Call started: %s", data.get("start", {}))
            elif event == "media":
                b64 = data.get("media", {}).get("payload")
                if not b64:
                    continue
                seg = b64_to_segment(b64)
                buffer += seg

                # If buffer reaches 1 sec, process
                if len(buffer) > 1000:
                    transcript = transcribe_audio(buffer)
                    logger.info("Heard: %s", transcript)

                    # Build GPT messages with memory
                    memory_manager.append_message(conv_id, "user", transcript)
                    messages = memory_manager.build_gpt_messages_from_history(conv_id, "You are Sara, return JSON.")
                    reply = gpt_reply(messages)
                    text = reply.get("sara_text", "")
                    memory_manager.append_message(conv_id, "assistant", text)

                    # TTS
                    mp3 = tts_bytes(text)
                    pcm = mp3_to_pcm8k(mp3)
                    await send_audio(ws, pcm)

                    buffer = AudioSegment.silent(duration=0)

            elif event == "stop":
                logger.info("Call stopped")
                break
    except Exception as e:
        logger.exception("WS error: %s", e)

# ---------------------------
# Entrypoint
# ---------------------------
def start_server():
    logger.info("Starting streaming server on :%d", TWILIO_MEDIA_PORT)
    loop = asyncio.get_event_loop()
    server = websockets.serve(handle_connection, "0.0.0.0", TWILIO_MEDIA_PORT, max_size=2**24)
    loop.run_until_complete(server)
    loop.run_forever()

if __name__ == "__main__":
    start_server()
