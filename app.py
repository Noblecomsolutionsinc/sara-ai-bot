# streaming_server.py
import os
import asyncio
import logging
import base64
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
import json
import websockets
import aiofiles
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- Load environment ---
REQUIRED = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "PUBLIC_STREAMING_URL",
    "MP3_RETENTION_HOURS"
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    logging.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVENLABS_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"]

# Temporary folder for audio
TMP_DIR = Path("tmp_audio")
TMP_DIR.mkdir(exist_ok=True)

# --- Helpers ---
async def cleanup_old_files():
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=MP3_RETENTION_HOURS)
    for f in TMP_DIR.glob("*"):
        if datetime.utcfromtimestamp(f.stat().st_mtime) < cutoff:
            try:
                f.unlink()
                logging.info("Deleted old temp file: %s", f)
            except Exception as e:
                logging.warning("Failed to delete temp file %s: %s", f, e)

async def call_openai(messages):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "gpt-4o-mini",  # can be changed via env if needed
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 500
    }
    resp = requests.post(url, headers=headers, json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

async def call_elevenlabs_tts(text, filename):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.75, "similarity_boost": 0.75}}
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    async with aiofiles.open(filename, "wb") as f:
        await f.write(resp.content)
    logging.info("TTS audio written to %s", filename)

# --- WebSocket handler ---
async def handle_call(websocket, path):
    logging.info("New call connection from Twilio")
    messages = [{"role": "system", "content": "You are Sara, a friendly outbound consultant."}]
    tmp_wav = TMP_DIR / f"sara_{int(time.time())}.wav"

    try:
        async for msg in websocket:
            data = json.loads(msg)
            event_type = data.get("event")
            if event_type == "start":
                logging.info("Call started: %s", data)
            elif event_type == "media":
                payload = data.get("media", {}).get("payload")
                if payload:
                    audio_bytes = base64.b64decode(payload)
                    async with aiofiles.open(tmp_wav, "ab") as f:
                        await f.write(audio_bytes)
            elif event_type == "stop":
                logging.info("Call ended")
                # Process accumulated audio with OpenAI / generate response
                user_text = "Simulated transcript"  # Replace with STT if needed
                messages.append({"role": "user", "content": user_text})
                reply_text = await asyncio.to_thread(call_openai, messages)
                messages.append({"role": "assistant", "content": reply_text})
                tts_file = TMP_DIR / f"sara_reply_{int(time.time())}.mp3"
                await call_elevenlabs_tts(reply_text, tts_file)
                logging.info("Call finished, response TTS ready: %s", tts_file)
    except websockets.exceptions.ConnectionClosedOK:
        logging.info("Call connection closed normally")
    except Exception as e:
        logging.exception("Error during call handling: %s", e)
    finally:
        if tmp_wav.exists():
            tmp_wav.unlink(missing_ok=True)

# --- Start server ---
async def main():
    await cleanup_old_files()
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8765))
    logging.info("Starting WebSocket streaming server on %s:%s", host, port)
    async with websockets.serve(handle_call, host, port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
