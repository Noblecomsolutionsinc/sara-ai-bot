# tasks.py
import os
import json
import base64
import logging
import tempfile
import requests
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from celery_app import celery
from gpt_client import call_gpt
from tts_client import text_to_speech

import redis

LOG = logging.getLogger("tasks")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL, decode_responses=True)

STATIC_TTS = Path("static/tts")
STATIC_TTS.mkdir(parents=True, exist_ok=True)

SERVER_URL = os.getenv("SERVER_URL")  # used by callback /internal/tts_ready
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "48"))

def audio_list_key(session_id: str) -> str:
    return f"sara:audio:{session_id}"

def meta_key(session_id: str) -> str:
    return f"sara:meta:{session_id}"

def result_key(session_id: str) -> str:
    return f"sara:result:{session_id}"

def pop_audio_chunks(session_id: str, max_chunks: int = 2000):
    key = audio_list_key(session_id)
    chunks = []
    for _ in range(max_chunks):
        raw = redis_conn.lpop(key)
        if not raw:
            break
        try:
            chunks.append(json.loads(raw))
        except Exception:
            continue
    return chunks

def write_raw_file(chunks):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".raw")
    for c in chunks:
        b64 = c.get("b64") or c.get("payload") or c.get("audio")
        try:
            tmp.write(base64.b64decode(b64))
        except Exception:
            continue
    tmp.flush()
    tmp.close()
    return tmp.name

def convert_raw_to_wav(raw_path):
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
    except Exception:
        LOG.exception("ffmpeg conversion failed")
        return None
    finally:
        try:
            os.unlink(raw_path)
        except Exception:
            pass

def call_whisper(wav_path: str) -> str:
    OPENAI_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_KEY:
        LOG.error("OpenAI key missing for ASR")
        return ""
    url = "https://api.openai.com/v1/audio/transcriptions"
    files = {"file": open(wav_path, "rb")}
    data = {"model": "whisper-1"}
    headers = {"Authorization": f"Bearer {OPENAI_KEY}"}
    try:
        r = requests.post(url, headers=headers, files=files, data=data, timeout=120)
        r.raise_for_status()
        txt = r.json().get("text", "")
        return txt
    except Exception:
        LOG.exception("Whisper call failed")
        return ""

@celery.task(bind=True, name="tasks.generate_tts")
def generate_tts(self, session_id: str, text: str, voice: str = None):
    LOG.info("generate_tts called for session=%s", session_id)
    path = text_to_speech(text, voice=voice, call_id=session_id)
    if path:
        # store result
        try:
            redis_conn.hset(result_key(session_id), mapping={"tts_path": path, "tts_generated_at": datetime.utcnow().isoformat()})
        except Exception:
            LOG.exception("Failed to store tts path in redis for %s", session_id)

        # callback to app so it can inject TwiML / Play the MP3 into active call
        try:
            if SERVER_URL:
                requests.post(f"{SERVER_URL.rstrip('/')}/internal/tts_ready", json={"call_id": session_id, "file": path}, timeout=8)
        except Exception:
            LOG.exception("Callback to app /internal/tts_ready failed")
        return {"status": "ok", "file": path}
    return {"status": "error"}

@celery.task(bind=True, name="tasks.process_audio")
def process_audio(self, session_id: str, do_asr: bool = True):
    LOG.info("process_audio started for session=%s (ASR=%s)", session_id, do_asr)
    chunks = pop_audio_chunks(session_id, max_chunks=8000)
    if not chunks:
        LOG.info("No audio chunks for session %s", session_id)
        return {"status": "empty"}

    # write raw PCM then convert to wav if ASR requested
    raw = write_raw_file(chunks)
    transcript = ""
    if do_asr:
        try:
            wav = convert_raw_to_wav(raw)
            if wav:
                transcript = call_whisper(wav) or ""
        except Exception:
            LOG.exception("ASR flow failed for session %s", session_id)
            transcript = ""
    else:
        # fast fallback summary
        transcript = "[short audio captured - no ASR]"

    # GPT processing
    try:
        sys_prompt = ""
        # try to grab safe system prompt from store if available
        try:
            if hasattr(sara_store := __import__("json_loader").json_loader.sara_store, "system_prompt"):
                sp = sara_store.system_prompt
                if isinstance(sp, dict):
                    sys_prompt = sp.get("text", "") or sp.get("realtime_short", "") or ""
                else:
                    sys_prompt = str(sp or "")
        except Exception:
            sys_prompt = ""

        prompt = f"{sys_prompt}\n\nTranscript: {transcript}\n\nReturn strict JSON with keys: intent, summary, action_text."
        gpt_out = call_gpt(prompt, max_completion_tokens=512, temperature=0.2)
        try:
            gpt_result = json.loads(gpt_out) if isinstance(gpt_out, str) else gpt_out or {}
        except Exception:
            gpt_result = {"raw": str(gpt_out)}
    except Exception:
        LOG.exception("GPT processing failed for session %s", session_id)
        gpt_result = {"error": "gpt_failed"}

    # store metadata & gpt_result
    try:
        redis_conn.hset(meta_key(session_id), mapping={"transcript": transcript, "gpt_result": json.dumps(gpt_result)})
        redis_conn.hset(result_key(session_id), mapping={"gpt_result": json.dumps(gpt_result)})
    except Exception:
        LOG.exception("Failed to store meta/result for session %s", session_id)

    # generate TTS if GPT provided text
    disposition_text = ""
    try:
        if isinstance(gpt_result, dict):
            disposition_text = gpt_result.get("summary") or gpt_result.get("action_text") or gpt_result.get("output_text") or ""
        elif isinstance(gpt_result, str):
            disposition_text = gpt_result
    except Exception:
        LOG.exception("parsing gpt_result failed")

    if disposition_text:
        generate_tts.delay(session_id, disposition_text)

    LOG.info("process_audio completed for session %s", session_id)
    return {"status": "processed", "gpt_result": gpt_result}
