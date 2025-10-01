import os
import logging
from pathlib import Path
import requests
from dotenv import load_dotenv

# --------------------------------------------------
# Load env
# --------------------------------------------------
load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "default")

# --------------------------------------------------
# Logging
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("tts_client")

# --------------------------------------------------
# Output dir
# --------------------------------------------------
TTS_OUT_DIR = Path("static/tts")
TTS_OUT_DIR.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------
# ElevenLabs API
# --------------------------------------------------
def text_to_speech(text: str) -> str:
    """
    Convert text into speech via ElevenLabs API
    Returns the saved MP3 file path
    """
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("❌ ELEVENLABS_API_KEY not set")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"

    headers = {
        "Accept": "audio/mpeg",
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "text": text,
        "voice_settings": {"stability": 0.6, "similarity_boost": 0.8}
    }

    try:
        logger.info(f"🔊 Sending TTS request: {text[:60]}...")
        response = requests.post(url, headers=headers, json=payload, timeout=60)

        if response.status_code != 200:
            logger.error(f"❌ TTS API error {response.status_code}: {response.text}")
            raise RuntimeError(f"TTS API error {response.status_code}")

        # Save file
        file_path = TTS_OUT_DIR / f"tts_{hash(text)}.mp3"
        with open(file_path, "wb") as f:
            f.write(response.content)

        logger.info(f"✅ TTS generated: {file_path}")
        return str(file_path)

    except requests.exceptions.Timeout:
        logger.error("⏱️ TTS request timed out")
        return "[System Error: TTS timeout]"
    except Exception as e:
        logger.error(f"❌ TTS generation failed: {e}")
        return "[System Error: TTS failure]"
