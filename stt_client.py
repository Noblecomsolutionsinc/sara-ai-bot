"""
stt_client.py

Speech-to-text helper for Twilio Media Streams audio payloads.

- Expects base64-encoded raw PCM frames from Twilio (default: 8000 Hz, s16le, mono).
- Writes raw bytes to a temp file, uses ffmpeg to convert to WAV, posts to OpenAI
  audio transcription endpoint (whisper-1) via requests.
- Returns the transcribed text (string) or empty string on failure.
"""

import os
import base64
import tempfile
import subprocess
import requests
import logging
from typing import Optional

logger = logging.getLogger("stt_client")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_TRANSCRIBE_URL = os.getenv("OPENAI_TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
# Twilio typically sends audio as 8kHz s16le mono PCM (but check your Twilio settings)
TWILIO_SAMPLE_RATE = int(os.getenv("TWILIO_SAMPLE_RATE", "8000"))
TWILIO_SAMPLE_FORMAT = os.getenv("TWILIO_SAMPLE_FORMAT", "s16le")  # sample format for ffmpeg input

def _raw_to_wav(raw_path: str, wav_path: str, sample_rate: int = TWILIO_SAMPLE_RATE, sample_fmt: str = TWILIO_SAMPLE_FORMAT) -> bool:
    """
    Use ffmpeg to convert raw PCM to WAV.
    raw_path: path to raw PCM file
    wav_path: desired wav output
    """
    try:
        cmd = [
            "ffmpeg",
            "-f", "s16le" if sample_fmt == "s16le" else sample_fmt,
            "-ar", str(sample_rate),
            "-ac", "1",
            "-i", raw_path,
            "-ar", "16000",   # upsample to 16k for better ASR if desired
            "-ac", "1",
            wav_path,
            "-y",
        ]
        logger.debug("Running ffmpeg: %s", " ".join(cmd))
        completed = subprocess.run(cmd, capture_output=True, check=False, timeout=15)
        if completed.returncode != 0:
            logger.error("ffmpeg failed: %s", completed.stderr.decode(errors="ignore"))
            return False
        return True
    except Exception as e:
        logger.exception("ffmpeg exception: %s", e)
        return False

def transcribe_from_base64_chunks(chunks_b64: list) -> str:
    """
    Accepts a list of base64-encoded raw PCM frames (strings).
    Concatenates them into a raw file, converts to WAV, calls OpenAI Whisper,
    and returns the transcribed text.
    """
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set; cannot transcribe")
        return ""

    if not chunks_b64:
        return ""

    try:
        # Create temporary raw file
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as raw_f:
            raw_path = raw_f.name
            for b64 in chunks_b64:
                try:
                    raw_bytes = base64.b64decode(b64)
                    raw_f.write(raw_bytes)
                except Exception as e:
                    logger.warning("Failed to decode chunk: %s", e)
            raw_f.flush()

        # Convert to WAV
        wav_fd, wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(wav_fd)
        ok = _raw_to_wav(raw_path, wav_path)
        if not ok:
            logger.error("Failed to convert raw PCM to WAV")
            try:
                os.remove(raw_path)
            except Exception:
                pass
            try:
                os.remove(wav_path)
            except Exception:
                pass
            return ""

        # Call OpenAI transcription endpoint via requests
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        # Use multipart form-data: file param must be binary file
        files = {"file": open(wav_path, "rb")}
        data = {"model": WHISPER_MODEL}
        logger.debug("Sending audio to OpenAI Whisper (local file=%s)...", wav_path)
        resp = requests.post(OPENAI_TRANSCRIBE_URL, headers=headers, files=files, data=data, timeout=30)
        try:
            files["file"].close()
        except Exception:
            pass

        if resp.status_code != 200:
            logger.error("OpenAI transcription failed: %s %s", resp.status_code, resp.text[:1000])
            try:
                os.remove(raw_path)
                os.remove(wav_path)
            except Exception:
                pass
            return ""

        payload = resp.json()
        text = payload.get("text") or ""
        text = text.strip()

        # Cleanup
        try:
            os.remove(raw_path)
            os.remove(wav_path)
        except Exception:
            pass

        return text

    except Exception as e:
        logger.exception("Transcription error: %s", e)
        return ""
