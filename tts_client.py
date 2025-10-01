# tts_client.py
import os
import requests
import logging
from pathlib import Path
from datetime import datetime

LOG = logging.getLogger("tts_client")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
TT S_OUT_DIR = Path("static/tts")
TTS_OUT_DIR = Path("static/tts")
TTS_OUT_DIR.mkdir(parents=True, exist_ok=True)

def text_to_speech(text: str, voice: Optional[str] = None, call_id: Optional[str] = None, retries: int = 2) -> Optional[str]:
    """
    Generate TTS via ElevenLabs. Returns path to saved mp3 or None.
    """
    if not ELEVEN_API_KEY:
        LOG.error("ELEVENLABS_API_KEY not configured")
        return None
    voice = voice or ELEVEN_VOICE_ID
    if not voice:
        LOG.error("No ElevenLabs voice id provided")
        return None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.6}}
    out_name = f"{call_id or 'tts'}_{int(datetime.utcnow().timestamp())}.mp3"
    out_path = TTS_OUT_DIR / out_name

    backoff = 1.0
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
            r.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            LOG.info("TTS saved to %s", out_path)
            return str(out_path)
        except Exception as e:
            LOG.warning("ElevenLabs attempt %d failed: %s", attempt + 1, e)
            if attempt < retries:
                import time; time.sleep(backoff); backoff *= 2
            else:
                LOG.exception("All ElevenLabs attempts failed")
                return None
