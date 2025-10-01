import os
import requests

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # default voice
PUBLIC_STREAMING_URL = os.getenv("PUBLIC_STREAMING_URL")


def synthesize_speech(text: str, call_sid: str) -> str:
    """
    Convert text to speech via ElevenLabs, save to static/tts, return public URL.
    """
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.7},
        }

        resp = requests.post(url, headers=headers, json=payload)
        resp.raise_for_status()

        mp3_data = resp.content
        path = f"static/tts/{call_sid}.mp3"

        with open(path, "wb") as f:
            f.write(mp3_data)

        return f"{PUBLIC_STREAMING_URL}/static/tts/{call_sid}.mp3"

    except Exception as e:
        print(f"[TTS Error] {e}")
        return ""
