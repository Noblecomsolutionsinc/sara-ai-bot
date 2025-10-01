# tts_client.py
import os
import requests
from pathlib import Path

ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")
ELEVEN_TIMEOUT = int(os.environ.get("ELEVENLABS_TTS_TIMEOUT", 120))

# Where generated mp3s will be stored
TTS_OUT_DIR = Path("static/tts")
TTS_OUT_DIR.mkdir(parents=True, exist_ok=True)


def text_to_speech(text, filename="output.mp3"):
    """
    Convert text to speech using ElevenLabs API.
    :param text: Text input
    :param filename: Output filename
    :return: Path to generated mp3
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID:
        raise RuntimeError("Missing ElevenLabs API credentials")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"

    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Content-Type": "application/json"
    }

    payload = {
        "text": text,
        "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}
    }

    out_path = TTS_OUT_DIR / filename

    response = requests.post(url, headers=headers, json=payload, stream=True, timeout=ELEVEN_TIMEOUT)
    response.raise_for_status()

    with open(out_path, "wb") as f:
        for chunk in response.iter_content(8192):
            if chunk:
                f.write(chunk)

    return str(out_path)
