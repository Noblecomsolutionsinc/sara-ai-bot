# worker.py
import os
import logging
import json
from pathlib import Path
import requests
from datetime import datetime

STATIC_TTS = Path("static/tts")
STATIC_TTS.mkdir(parents=True, exist_ok=True)

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
SERVER_URL = os.getenv("SERVER_URL")  # to callback /internal/tts_ready

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

def elevenlabs_tts(text, filename, voice_id=None):
    """
    Minimal ElevenLabs HTTP call. Replace voice_id with your voice.
    """
    if not ELEVEN_API_KEY:
        logger.error("No ElevenLabs key set")
        return False
    voice = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "alloy")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text}
    try:
        r = requests.post(url, headers=headers, json=payload, stream=True, timeout=60)
        r.raise_for_status()
        with open(filename, "wb") as fh:
            for chunk in r.iter_content(1024):
                fh.write(chunk)
        return True
    except Exception as e:
        logger.exception("ElevenLabs TTS error: %s", e)
        return False

def process_task(payload):
    """
    Called by RQ worker. Example payload:
    {"type":"generate_tts","call_id":"...","text":"..."}
    """
    task_type = payload.get("type")
    call_id = payload.get("call_id")
    logger.info("Worker processing task_type=%s call_id=%s", task_type, call_id)
    if task_type == "generate_tts":
        text = payload.get("text", "Hello from Sara AI")
        fname = STATIC_TTS / f"{call_id}_{int(datetime.utcnow().timestamp())}.mp3"
        ok = elevenlabs_tts(text, str(fname))
        if ok:
            logger.info("TTS saved: %s", fname)
            # notify Flask app that a tts file exists
            if SERVER_URL:
                try:
                    requests.post(f"{SERVER_URL}/internal/tts_ready", json={"call_id": call_id, "file": str(fname)}, timeout=10)
                except Exception:
                    logger.exception("Callback to app failed")
        else:
            logger.error("TTS generation failed for call %s", call_id)
    else:
        logger.warning("Unknown task type: %s", task_type)
