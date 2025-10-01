import logging
import os
from celery import Celery
from tts_client import text_to_speech
from gpt_client import generate_reply
import asyncio

# --------------------------------------------------
# Logging
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("tasks")

# --------------------------------------------------
# Celery Config
# --------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery = Celery(
    "tasks",
    broker=REDIS_URL,
    backend=REDIS_URL
)

logger.info(f"✅ Celery connected to {REDIS_URL}")


# --------------------------------------------------
# Process Audio → GPT → TTS
# --------------------------------------------------
@celery.task(bind=True, max_retries=3)
def process_audio(self, user_text: str):
    """
    Full pipeline:
    1. Get GPT reply
    2. Convert to speech
    3. Return TTS file path
    """
    try:
        logger.info(f"🎤 Processing audio input: {user_text}")

        # Call GPT
        loop = asyncio.get_event_loop()
        gpt_reply = loop.run_until_complete(generate_reply(user_text))

        # Call TTS
        audio_file = text_to_speech(gpt_reply)

        logger.info(f"✅ Pipeline success: {audio_file}")
        return {"reply": gpt_reply, "audio": audio_file}

    except Exception as e:
        logger.error(f"❌ process_audio failed: {e}")
        raise self.retry(exc=e, countdown=5)
