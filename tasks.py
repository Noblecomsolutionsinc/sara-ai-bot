# tasks.py
"""
Celery background tasks for Sara AI:
- process_audio: collects audio chunks from Redis, optionally runs ASR (Whisper),
  sends transcript to GPT, generates TTS via ElevenLabs, stores results in Redis,
  and notifies the Flask app (/internal/tts_ready) so Twilio can play audio into the live call.
- generate_tts: wrapper to create TTS and callback the app.
"""
from __future__ import annotations

import os
import json
import time
import base64
import logging
import tempfile
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests
import redis

from celery_app import celery
from gpt_client import generate_reply as gpt_generate  # expects list-of-messages or prompt
from tts_client import text_to_speech

LOG = logging.getLogger("tasks")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
LOG.addHandler(handler)

# Redis
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL, decode_responses=True)

# Paths
STATIC_TTS = Path("static/tts")
STATIC_TTS.mkdir(parents=True, exist_ok=True)

# Config
SERVER_URL = os.getenv("SERVER_URL", "")  # For callback to /internal/tts_ready
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "48"))
MAX_POP_CHUNKS = int(os.getenv("MAX_POP_CHUNKS", "8000"))

# Redis keys helper
def audio_list_key(session_id: str) -> str:
    return f"sara:audio:{session_id}"

def meta_key(session_id: str) -> str:
    return f"sara:meta:{session_id}"

def result_key(session_id: str) -> str:
    return f"sara:result:{session_id}"

# --- Audio helpers ---
def pop_audio_chunks(session_id: str, max_chunks: int = MAX_POP_CHUNKS) -> List[Dict[str, Any]]:
    key = audio_list_key(session_id)
    chunks = []
    for _ in range(max_chunks):
        raw = redis_conn.lpop(key)
        if not raw:
            break
        try:
            chunks.append(json.loads(raw))
        except Exception:
            # fallback if raw already a dict-like string
            try:
                chunks.append(eval(raw))
            except Exception:
                continue
    return chunks

def write_raw_file(chunks: List[Dict[str, Any]]) -> Optional[str]:
    if not chunks:
        return None
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".raw")
    try:
        for c in chunks:
            b64 = c.get("b64") or c.get("payload") or c.get("audio")
            if not b64:
                continue
            try:
                tmp.write(base64.b64decode(b64))
            except Exception:
                LOG.warning("Skipping invalid base64 chunk")
                continue
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception:
        LOG.exception("Failed writing raw file")
        try:
            tmp.close()
        except Exception:
            pass
        return None

def convert_raw_to_wav(raw_path: str) -> Optional[str]:
    """
    Uses ffmpeg to convert 8kHz s16le mono raw PCM -> 16kHz WAV suitable for Whisper.
    Expects ffmpeg binary available in PATH.
    """
    if not raw_path or not Path(raw_path).exists():
        return None
    wav_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    wav_tmp.close()
    cmd = [
        "ffmpeg", "-y",
        "-f", "s16le",
        "-ar", "8000",
        "-ac", "1",
        "-i", raw_path,
        "-ar", "16000",
        wav_tmp.name
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return wav_tmp.name
    except subprocess.CalledProcessError:
        LOG.exception("ffmpeg conversion failed")
        return None
    except Exception:
        LOG.exception("ffmpeg conversion unexpected error")
        return None
    finally:
        # remove raw file if conversion succeeded or not
        try:
            Path(raw_path).unlink()
        except Exception:
            pass

# --- Whisper ASR (OpenAI API) ---
def call_whisper_api(wav_path: str) -> str:
    OPENAI_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_KEY:
        LOG.error("OPENAI_API_KEY not set; skipping ASR")
        return ""
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    files = {"file": open(wav_path, "rb")}
    data = {"model": "whisper-1"}
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
        r.raise_for_status()
        return r.json().get("text", "") or ""
    except Exception:
        LOG.exception("Whisper transcription failed")
        return ""

# --- TTS callback helper ---
def callback_tts_ready(session_id: str, path: str) -> None:
    """
    Notify Flask app that a TTS file is ready so it can inject TwiML into the live call.
    """
    try:
        if not SERVER_URL:
            LOG.warning("SERVER_URL not set; cannot callback /internal/tts_ready")
            return
        url = f"{SERVER_URL.rstrip('/')}/internal/tts_ready"
        payload = {"call_id": session_id, "file": path}
        # Small timeout - this is best-effort
        r = requests.post(url, json=payload, timeout=8)
        r.raise_for_status()
        LOG.info("Callback /internal/tts_ready succeeded for %s", session_id)
    except Exception:
        LOG.exception("Callback to /internal/tts_ready failed for %s", session_id)

# --- Celery tasks ---
@celery.task(bind=True, name="tasks.generate_tts", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def generate_tts(self, session_id: str, text: str, voice: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate TTS via ElevenLabs and callback the app so Twilio can play the MP3.
    """
    LOG.info("generate_tts called for session=%s", session_id)
    try:
        safe_name = f"{session_id}_{int(datetime.utcnow().timestamp())}.mp3"
        path = text_to_speech(text=text, filename=safe_name)
        if path:
            # store result metadata
            try:
                redis_conn.hset(result_key(session_id), mapping={"tts_path": path, "tts_at": datetime.utcnow().isoformat()})
            except Exception:
                LOG.exception("Failed to write tts metadata to redis for %s", session_id)
            # callback Flask app to inject into Twilio
            callback_tts_ready(session_id, path)
            return {"status": "ok", "file": path}
        else:
            LOG.error("tts_client returned no path for session %s", session_id)
            return {"status": "error", "reason": "tts_failed"}
    except Exception as e:
        LOG.exception("generate_tts failed for %s", session_id)
        raise

@celery.task(bind=True, name="tasks.process_audio", autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def process_audio(self, session_id: str, do_asr: bool = True) -> Dict[str, Any]:
    """
    Full processing pipeline:
    - pull audio chunks from Redis
    - (optional) run ASR
    - call GPT for intent/summary/actions
    - generate TTS if required and callback app
    """
    LOG.info("process_audio started for session=%s do_asr=%s", session_id, do_asr)
    try:
        chunks = pop_audio_chunks(session_id)
        if not chunks:
            LOG.info("No audio chunks to process for %s", session_id)
            return {"status": "empty"}

        raw_path = write_raw_file(chunks)
        transcript = ""
        if do_asr and raw_path:
            wav = convert_raw_to_wav(raw_path)
            if wav:
                try:
                    transcript = call_whisper_api(wav) or ""
                except Exception:
                    LOG.exception("ASR call failed for %s", session_id)
                    transcript = ""
        else:
            # placeholder short transcript for quick flow
            transcript = "[short audio captured - no ASR]"

        # Build GPT prompt
        try:
            from json_loader import sara_store
            sys_prompt = ""
            if hasattr(sara_store, "system_prompt"):
                sp = sara_store.system_prompt
                if isinstance(sp, dict):
                    sys_prompt = sp.get("text", "") or sp.get("realtime_short", "")
                else:
                    sys_prompt = str(sp or "")
        except Exception:
            sys_prompt = ""

        prompt = f"{sys_prompt}\n\nTranscript: {transcript}\n\nReturn JSON with keys: intent, summary, action_text."
        # Use chat style: pass as messages for best results
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": transcript}]
        try:
            gpt_out_raw = gpt_generate(messages, model=os.getenv("OPENAI_MODEL", "gpt-5-mini-2025-08-07"), max_tokens=int(os.getenv("OPENAI_MAX_TOKENS", "512")))
        except TypeError:
            # fallback if generate_reply signature expects (str,..)
            gpt_out_raw = gpt_generate(prompt)
        except Exception:
            LOG.exception("GPT call failed for %s", session_id)
            gpt_out_raw = None

        # Normalize GPT output to dict
        gpt_result: Dict[str, Any] = {}
        try:
            if isinstance(gpt_out_raw, str):
                try:
                    gpt_result = json.loads(gpt_out_raw)
                except Exception:
                    # Not JSON — keep raw under 'raw'
                    gpt_result = {"raw": gpt_out_raw}
            elif isinstance(gpt_out_raw, dict):
                gpt_result = gpt_out_raw
            else:
                gpt_result = {"raw": str(gpt_out_raw)}
        except Exception:
            LOG.exception("Failed to normalize gpt result for %s", session_id)
            gpt_result = {"raw": str(gpt_out_raw)}

        # Store transcript and gpt_result
        try:
            redis_conn.hset(meta_key(session_id), mapping={"transcript": transcript or "", "gpt_result": json.dumps(gpt_result)})
            redis_conn.hset(result_key(session_id), mapping={"gpt_result": json.dumps(gpt_result)})
        except Exception:
            LOG.exception("Failed to write meta/result for %s", session_id)

        # Decide whether to TTS based on GPT result
        disposition_text = ""
        try:
            if isinstance(gpt_result, dict):
                disposition_text = gpt_result.get("summary") or gpt_result.get("action_text") or gpt_result.get("output_text") or ""
            elif isinstance(gpt_result, str):
                disposition_text = gpt_result
        except Exception:
            LOG.exception("parsing gpt_result failed")

        if disposition_text:
            # schedule TTS generation (async)
            generate_tts.delay(session_id, disposition_text)

        LOG.info("process_audio finished for session=%s", session_id)
        return {"status": "processed", "gpt_result": gpt_result}

    except Exception:
        LOG.exception("Unhandled error in process_audio for %s", session_id)
        raise

# cleanup task for old MP3s
@celery.task(bind=True, name="tasks.cleanup_mp3s")
def cleanup_mp3s(self) -> Dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(hours=MP3_RETENTION_HOURS)
    removed = 0
    for f in STATIC_TTS.glob("*.mp3"):
        try:
            mtime = datetime.utcfromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            continue
    LOG.info("cleanup_mp3s removed %d files older than %s", removed, cutoff.isoformat())
    return {"removed": removed}
