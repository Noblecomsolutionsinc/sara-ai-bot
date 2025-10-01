# generate_tts_and_upload.py
import os
import json
import time
import requests
from pathlib import Path

OUT_DIR = Path("static/tts")
OUT_DIR.mkdir(parents=True, exist_ok=True)
GREETINGS_OUT = Path("brains/greetings_map.json")

ELEVEN_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")

GREETING_PHRASES = {
    "general": {
        "greeting": "Hi, this is Sara Hayes from Noblecom Solutions. How are you doing today?",
        "hold": "Thanks — one moment while I pull up a quick insight for you.",
        "booked": "Great — I’ll send a calendar invite for a short call. What email should I use?"
    }
}

def synthesize(text, out_path):
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID:
        raise RuntimeError("ElevenLabs credentials missing")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
    headers = {"xi-api-key": ELEVEN_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.6}}
    r = requests.post(url, headers=headers, json=payload, stream=True, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as fh:
        for chunk in r.iter_content(8192):
            if chunk:
                fh.write(chunk)
    return str(out_path)

def main():
    mapping = {}
    for btype, phrases in GREETING_PHRASES.items():
        mapping[btype] = {}
        for tag, txt in phrases.items():
            fname = f"{btype}_{tag}.mp3"
            out = OUT_DIR / fname
            print("Generating", fname)
            synthesize(txt, out)
            mapping[btype][tag] = str(out)
            time.sleep(0.5)
    GREETINGS_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(GREETINGS_OUT, "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2)
    print("Wrote", GREETINGS_OUT)

if __name__ == "__main__":
    main()
