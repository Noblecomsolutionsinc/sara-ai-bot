# generate_tts_and_upload.py (LOCAL VERSION - no AWS)
"""
Generate ElevenLabs TTS clips and save them locally.
Outputs:
  - brains/greetings_map.json : { general: { greeting, hold, booked } }

Usage:
  set env vars: ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
  then run: python generate_tts_and_upload.py
"""

import os
import json
import time
import requests
import pathlib

# ---------- CONFIG ---------- #
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")

OUT_DIR = "static/tts"
GREETINGS_OUT = "brains/greetings_map.json"

# Phrases Sara will use
GREETING_PHRASES = {
    "general": {
        "greeting": "Hi, this is Sara Hayes from Noblecom Solutions. How are you doing today?",
        "hold": "Thanks — one moment while I pull up a quick insight for you.",
        "booked": "Great — I’ll send a calendar invite for a short call. What email should I use?"
    }
}

# ---------- Helpers ---------- #
def ensure_out_dir():
    pathlib.Path(OUT_DIR).mkdir(parents=True, exist_ok=True)

def synthesize_elevenlabs_stream(text, out_path):
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        raise RuntimeError("ElevenLabs credentials missing (ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID)")
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Accept": "audio/mpeg", "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.6, "similarity_boost": 0.7}}
    with requests.post(url, headers=headers, json=payload, stream=True, timeout=120) as r:
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:400]}")
        with open(out_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    fh.write(chunk)
    return out_path

# ---------- Main generation ---------- #
def main():
    ensure_out_dir()
    greetings_map = {}

    for btype, phrases in GREETING_PHRASES.items():
        greetings_map[btype] = {}
        for tag, txt in phrases.items():
            filename = f"{btype}_{tag}.mp3"
            local_path = os.path.join(OUT_DIR, filename)
            print(f"[+] Generating {filename} ...")
            synthesize_elevenlabs_stream(txt, local_path)
            greetings_map[btype][tag] = local_path
            print(" ->", local_path)
            time.sleep(0.5)

    with open(GREETINGS_OUT, "w", encoding="utf-8") as f:
        json.dump(greetings_map, f, indent=2)

    print(f"[wrote] {GREETINGS_OUT}")
    print("All done. MP3 files are saved locally in static/tts/")

if __name__ == "__main__":
    main()
