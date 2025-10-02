#!/usr/bin/env python3
"""
stt_client.py

Speech-to-text helper:
- Accepts a list of base64-encoded PCM chunks (s16le @ 8000Hz mono from Twilio Media Streams).
- Concatenates and writes to a temporary raw file, converts to WAV via ffmpeg, then calls
  OpenAI transcription endpoint (whisper-1) via HTTP multipart upload.
- Returns transcript string ('' on silence or failure).

Usage:
    transcript = transcribe_from_base64_chunks(list_of_b64_strings)
"""
from __future__ import annotations

import os
import base64
import tempfile
import uuid
import subprocess
import logging
import json
import requests
from typing import List

# Config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRANSCRIPTION_URL = os.getenv("OPENAI_TRANSCRIPTION_URL", "https://api.openai.com/v1/audio/transcriptions")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "whisper-1")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))
FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")  # allow override

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("stt_client")


def _write_raw_pcm(payloads: List[bytes], raw_path: str) -> None:
    with open(raw_path, "wb") as f:
        for chunk in payloads:
            f.write(chunk)


def _convert_raw_to_wav(raw_path: str, wav_path: str) -> bool:
    """
    Convert s16le 8000 Hz mono raw PCM to WAV 16000 Hz mono using ffmpeg.
    Returns True on success.
    """
    cmd = [
        FFMPEG_BINARY,
        "-f", "s16le",
        "-ar", "8000",
        "-ac", "1",
        "-i", raw_path,
        "-ar", "16000",
        "-ac", "1",
        wav_path,
        "-y",
        "-loglevel", "error",
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.exception("ffmpeg conversion failed: %s", e)
        return False
    except FileNotFoundError:
        logger.exception("ffmpeg binary not found, ensure ffmpeg is installed and on PATH.")
        return False


def transcribe_from_base64_chunks(b64_chunks: List[str]) -> str:
    """
    Accepts list of base64 strings (Twilio Media Stream frames).
    Returns transcript text or empty string on failure or silence.
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not configured; cannot transcribe.")
        return ""

    if not b64_chunks:
        logger.debug("No audio chunks provided to STT.")
        return ""

    # Decode chunks
    decoded_chunks: List[bytes] = []
    for idx, b64 in enumerate(b64_chunks):
        try:
            decoded_chunks.append(base64.b64decode(b64))
        except Exception:
            logger.exception("Failed to decode base64 chunk #%s; skipping", idx)

    if not decoded_chunks:
        logger.debug("No valid audio after decoding base64 chunks.")
        return ""

    tmpdir = tempfile.mkdtemp(prefix="sara_stt_")
    raw_path = os.path.join(tmpdir, f"{uuid.uuid4().hex}.raw")
    wav_path = os.path.join(tmpdir, f"{uuid.uuid4().hex}.wav")
    try:
        _write_raw_pcm(decoded_chunks, raw_path)
        ok = _convert_raw_to_wav(raw_path, wav_path)
        if not ok:
            logger.error("Failed to convert raw audio to WAV; aborting STT.")
            return ""

        # Call OpenAI transcription endpoint using requests (multipart)
        with open(wav_path, "rb") as f:
            files = {"file": ("audio.wav", f, "audio/wav")}
            data = {"model": OPENAI_STT_MODEL}
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
            try:
                resp = requests.post(
                    OPENAI_TRANSCRIPTION_URL,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=OPENAI_TIMEOUT,
                )
                if resp.status_code == 200:
                    j = resp.json()
                    text = j.get("text") or j.get("transcript") or ""
                    if isinstance(text, str):
                        text = text.strip()
                        logger.info("STT transcript: %s", text[:200])
                        return text
                    else:
                        logger.warning("OpenAI transcription response missing text field.")
                        return ""
                else:
                    logger.warning("OpenAI transcription failed status=%s body=%s", resp.status_code, resp.text)
                    return ""
            except requests.RequestException:
                logger.exception("HTTP request to OpenAI transcription endpoint failed.")
                return ""
    finally:
        # Cleanup
        try:
            if os.path.exists(raw_path):
                os.remove(raw_path)
            if os.path.exists(wav_path):
                os.remove(wav_path)
            if os.path.isdir(tmpdir):
                os.rmdir(tmpdir)
        except Exception:
            pass
