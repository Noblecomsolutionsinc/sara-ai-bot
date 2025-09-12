import os
import requests
from dotenv import load_dotenv

# -----------------------------
# Load .env
# -----------------------------
load_dotenv()

ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
SERVER_URL = os.getenv("SERVER_URL")  # for public URL
STATIC_AUDIO_DIR = "static/audio"

# Confirm environment variables
print("ELEVENLABS_KEY =", "Loaded" if ELEVENLABS_KEY else "MISSING")
print("VOICE_ID =", "Loaded" if VOICE_ID else "MISSING")
print("SERVER_URL =", SERVER_URL if SERVER_URL else "MISSING")

if not ELEVENLABS_KEY or not VOICE_ID or not SERVER_URL:
    print("ERROR: Missing required environment variables. Check your .env file.")
    exit(1)

# -----------------------------
# Ensure audio folder exists
# -----------------------------
os.makedirs(STATIC_AUDIO_DIR, exist_ok=True)

# -----------------------------
# Function to generate TTS MP3
# -----------------------------
def generate_voice_mp3(text, call_sid):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVENLABS_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "voice_settings": {"stability":0.75, "similarity_boost":0.8}
    }

    print("Sending request to ElevenLabs...")
    r = requests.post(url, headers=headers, json=payload, stream=True)
    try:
        r.raise_for_status()
    except Exception as e:
        print("ElevenLabs API error:", e)
        exit(1)

    mp3_filename = f"sara_{call_sid}.mp3"
    mp3_path = os.path.join(STATIC_AUDIO_DIR, mp3_filename)

    with open(mp3_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)

    public_url = f"{SERVER_URL}/static/audio/{mp3_filename}"
    return mp3_path, public_url

# -----------------------------
# Test script
# -----------------------------
if __name__ == "__main__":
    test_text = "Hello! This is Sara speaking from ElevenLabs. This is a test."
    test_sid = "TEST123"

    mp3_path, public_url = generate_voice_mp3(test_text, test_sid)

    print("\n✅ MP3 successfully generated!")
    print("Local path:", mp3_path)
    print("Public URL:", public_url)
    print("\nOpen the Public URL in your browser to hear Sara's voice.")
