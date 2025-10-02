#!/usr/bin/env python3
"""
tts_client.py

ElevenLabs TTS client wrapper:
- Calls ElevenLabs HTTP API to synthesize text-to-speech.
- Saves MP3 to static/tts/{session_id}.mp3 (ensures dir exists).
- Returns fully-qualified URL SERVER_URL + /static/tts/{filename}.

WARNING: Twilio requires HTTPS-accessible URLs. Ensure SERVER_URL is a public HTTPS endpoint (Render or S3).
"""
from __future__ import annotations

import os
import time
import logging
from pathlib import Path
from typing import Optional

import requests

# Config
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_TTS_ENDPOINT = os.getenv("ELEVENLABS_TTS_ENDPOINT", "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}")
ELEVENLABS_TTS_TIMEOUT = int(os.getenv("ELEVENLABS_TTS_TIMEOUT", "30"))
ELEVENLABS_RETRY_ATTEMPTS = int(os.getenv("ELEVENLABS_RETRY_ATTEMPTS", "3"))
ELEVENLABS_RETRY_BACKOFF = float(os.getenv("ELEVENLABS_RETRY_BACKOFF", "1.5"))

SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
TTS_DIR = Path(os.getenv("TTS_OUTPUT_DIR", "static/tts"))

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("tts_client")

# Ensure directory exists
try:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.exception("Failed to create TTS output directory: %s", TTS_DIR)


def synthesize_speech(text: str, session_id: Optional[str] = None) -> Optional[str]:
    """
    Generate TTS using ElevenLabs HTTP API.
    Save to static/tts/<session_id>.mp3 if session_id provided, else uses timestamp-based name.
    Returns public URL or None on failure.

    Note: SERVER_URL must be HTTPS and reachable by Twilio for <Play>.
    """
    if not ELEVENLABS_API_KEY:
        logger.error("ELEVENLABS_API_KEY missing; cannot generate TTS.")
        return None
    if not ELEVENLABS_VOICE_ID:
        logger.error("ELEVENLABS_VOICE_ID missing; cannot generate TTS.")
        return None
    if not SERVER_URL:
        logger.error("SERVER_URL not configured; returning local path is unsafe for Twilio.")
        # still proceed to write file locally for testing, but warn caller
    filename = f"{session_id or int(time.time())}.mp3"
    out_path = TTS_DIR / filename

    endpoint = ELEVENLABS_TTS_ENDPOINT.format(voice_id=ELEVENLABS_VOICE_ID)
    headers = {
        "Accept": "audio/mpeg",
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {"text": text}

    for attempt in range(1, ELEVENLABS_RETRY_ATTEMPTS + 1):
        try:
            resp = requests.post(endpoint, headers=headers, json=payload, timeout=ELEVENLABS_TTS_TIMEOUT, stream=True)
            if resp.status_code == 200:
                # Save MP3 binary
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=4096):
                        if chunk:
                            f.write(chunk)
                public_url = f"{SERVER_URL}/static/tts/{filename}" if SERVER_URL else str(out_path.resolve())
                logger.info("TTS saved to %s (public URL: %s)", out_path, public_url)
                return public_url
            elif 500 <= resp.status_code < 600:
                logger.warning("ElevenLabs server error %s (attempt %s), retrying after backoff", resp.status_code, attempt)
                time.sleep(ELEVENLABS_RETRY_BACKOFF * attempt)
                continue
            else:
                logger.warning("ElevenLabs returned non-success %s: %s", resp.status_code, resp.text)
                return None
        except requests.RequestException:
            logger.exception("ElevenLabs request failed on attempt %s", attempt)
            time.sleep(ELEVENLABS_RETRY_BACKOFF * attempt)
            continue

    logger.error("All ElevenLabs attempts failed.")
    return None
