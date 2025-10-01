# worker.py
import os
import logging
import time
import json
from rq import Queue, Worker
from redis import Redis
from pathlib import Path
from elevenlabs import generate, set_api_key

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-worker")

# --- Environment ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))
QUEUE_NAME = os.environ.get("QUEUE_NAME", "audio")

if not ELEVENLABS_API_KEY:
    log.warning("ELEVENLABS_API_KEY not set; TTS tasks will fail")
else:
    set_api_key(ELEVENLABS_API_KEY)

# --- Redis queue setup ---
redis_conn = Redis.from_url(REDIS_URL)
q = Queue(QUEUE_NAME, connection=redis_conn)

# --- Load Sara JSONs for context ---
DATA_DIR = Path("data")
SARA_CALLFLOW = json.loads((DATA_DIR / "Sara_CallFlow.json").read_text(encoding="utf-8"))
SARA_KNOWLEDGE = json.loads((DATA_DIR / "Sara_KnowledgeBase.json").read_text(encoding="utf-8"))
SARA_OBJECTIONS = json.loads((DATA_DIR / "Sara_Objections.json").read_text(encoding="utf-8"))
SARA_OPENING = json.loads((DATA_DIR / "Sara_Opening.json").read_text(encoding="utf-8"))
SARA_PLAYBOOK = json.loads((DATA_DIR / "Sara_Playbook.json").read_text(encoding="utf-8"))
SARA_SYSTEM = json.loads((DATA_DIR / "Sara_SystemPrompt_Production.json").read_text(encoding="utf-8"))

# --- Tasks ---
def generate_tts(text: str, filename: str):
    """
    Generate TTS using ElevenLabs and save locally under static/tts
    """
    from pathlib import Path
    output_dir = Path("static/tts")
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"{filename}.mp3"
    log.info("Generating TTS for: %s -> %s", text[:60], filepath)
    try:
        audio = generate(text=text, voice="alloy", model="eleven_monolingual_v1")
        with open(filepath, "wb") as f:
            f.write(audio)
        log.info("TTS saved: %s", filepath)
        return str(filepath)
    except Exception as e:
        log.exception("Failed to generate TTS: %s", e)
        return None

def process_call_audio(call_id: str, audio_base64: str):
    """
    Background task to process an incoming audio chunk
    """
    log.info("Processing audio for call_id=%s, bytes=%d", call_id, len(audio_base64 or ""))
    # TODO: Send to ChatGPT or other NLP pipeline
    time.sleep(0.2)  # simulate processing

def enqueue_tts(text: str, filename: str):
    """
    Enqueue TTS generation task
    """
    log.info("Enqueue TTS task for %s", filename)
    q.enqueue(generate_tts, text, filename)

def enqueue_audio_processing(call_id: str, audio_base64: str):
    """
    Enqueue audio processing task
    """
    q.enqueue(process_call_audio, call_id, audio_base64)

# --- Worker runner ---
if __name__ == "__main__":
    log.info("Starting RQ worker on queue '%s' with Redis %s", QUEUE_NAME, REDIS_URL)
    worker = Worker([q], connection=redis_conn)
    worker.work()
