# tasks.py
import os
import json
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timedelta
import base64
import subprocess
import requests

from celery_app import celery
from gpt_client import call_gpt
from json_loader import sara_store
import redis

LOG = logging.getLogger("tasks")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
redis_conn = redis.from_url(REDIS_URL)

STATIC_TTS = Path("static/tts")
STATIC_TTS.mkdir(parents=True, exist_ok=True)

ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "alloy")
SERVER_URL = os.environ.get("SERVER_URL")  # callback target
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "48"))

# Redis key helpers
def audio_list_key(session_id):
    return f"sara:audio:{session_id}"

def meta_key(session_id):
    return f"sara:meta:{session_id}"

def push_meta(session_id, k, v):
    redis_conn.hset(meta_key(session_id), k, v)

def pop_audio_chunks(session_id, max_chunks=2000):
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

def call_whisper(wav_path):
    OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
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

def eleven_generate(text, call_id, voice=None, retries=2):
    if not ELEVEN_API_KEY:
        LOG.error("ElevenLabs API key not set")
        return None
    voice = voice or ELEVEN_VOICE_ID
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.6}}
    backoff = 1.0
    out_name = f"{call_id}_{int(datetime.utcnow().timestamp())}.mp3"
    out_path = STATIC_TTS / out_name
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
            r.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            LOG.info("Generated TTS: %s", out_path)
            return str(out_path)
        except Exception as e:
            LOG.warning("ElevenLabs attempt %d failed: %s", attempt + 1, e)
            if attempt < retries:
                import time; time.sleep(backoff); backoff *= 2
            else:
                LOG.exception("ElevenLabs final failure")
                return None

@celery.task(bind=True, name="tasks.generate_tts")
def generate_tts(self, call_id, text, voice=None):
    LOG.info("generate_tts called for %s", call_id)
    path = eleven_generate(text, call_id, voice=voice)
    if path:
        # log and optionally callback
        try:
            if SERVER_URL:
                requests.post(f"{SERVER_URL}/internal/tts_ready", json={"call_id": call_id, "file": path}, timeout=8)
        except Exception:
            LOG.exception("Callback to app failed")
        # schedule cleanup meta
        push_meta(call_id, "last_tts", path)
        return {"status": "ok", "file": path}
    return {"status": "error"}

@celery.task(bind=True, name="tasks.process_audio")
def process_audio(self, session_id, do_asr=False):
    LOG.info("process_audio session %s asr=%s", session_id, do_asr)
    chunks = pop_audio_chunks(session_id, max_chunks=8000)
    if not chunks:
        LOG.info("No audio data for %s", session_id)
        return {"status": "empty"}
    raw = write_raw_file(chunks)
    wav = None
    transcript = "[no-asr]"
    try:
        if do_asr:
            wav = convert_raw_to_wav(raw)
            if wav:
                transcript = call_whisper(wav) or transcript
    except Exception:
        LOG.exception("ASR flow failed; defaulting transcript")
    # Compose GPT prompt
    sys_prompt = ""
    if isinstance(sara_store.system_prompt, dict):
        sys_prompt = sara_store.system_prompt.get("text", "")
    elif sara_store.system_prompt:
        sys_prompt = str(sara_store.system_prompt)
    kb = sara_store.knowledge if sara_store.knowledge else {}
    prompt = f"{sys_prompt}\n\nKnowledge excerpt: {json.dumps(kb)[:4000]}\n\nTranscript: {transcript}\n\nReturn strict JSON with keys: outcome, confidence, disposition_text, followup_email."
    gpt_out = call_gpt(prompt, max_tokens=512, temperature=0.2)
    push_meta(session_id, "last_processed", json.dumps(gpt_out or {}))
    # If GPT suggests speaking, create TTS
    disposition_text = ""
    try:
        if isinstance(gpt_out, dict):
            # try to find text in common fields
            if "output_text" in gpt_out:
                disposition_text = gpt_out["output_text"]
            elif "output" in gpt_out and isinstance(gpt_out["output"], list):
                # join textual content
                parts = []
                for o in gpt_out["output"]:
                    if isinstance(o, dict):
                        parts.append(str(o.get("content", "")))
                    else:
                        parts.append(str(o))
                disposition_text = " ".join(parts)
            else:
                disposition_text = json.dumps(gpt_out)[:1000]
    except Exception:
        LOG.exception("parsing gpt_out failed")
    if disposition_text:
        generate_tts.delay(session_id, disposition_text)
    return {"status": "processed", "gpt": gpt_out}

@celery.task(bind=True, name="tasks.cleanup_mp3s")
def cleanup_mp3s(self):
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
