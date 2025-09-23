# File: app.py
# Path: ./app.py
import os
import uuid
import time
import logging
from datetime import datetime
from flask import Flask, request, send_from_directory, jsonify, Response
from dotenv import load_dotenv
import requests
from twilio.rest import Client as TwilioClient
import glob

# Load .env
load_dotenv()

# ---------------------------
# Config / env
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))  # seconds
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "300"))

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_TTS_TIMEOUT = int(os.getenv("ELEVENLABS_TTS_TIMEOUT", "120"))

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

SERVER_URL = os.getenv("SERVER_URL")
SARA_NAME = os.getenv("SARA_NAME", "Sara")
SARA_ROLE = os.getenv("SARA_ROLE", "Digital Marketing Consultant")
SARA_COMPANY = os.getenv("COMPANY_NAME", "")
CALENDLY_LINK = os.getenv("MEETING_LINK", "")

# Retry/backoff policy
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "1"))

# Behaviour flags
SIMULATE = os.getenv("SIMULATE", "false").strip().lower() in ("1", "true", "yes")
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "24"))

# Logging config
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("sara-ai")

# ---------------------------
# App setup
# ---------------------------
app = Flask(__name__)
AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# Initialize Twilio client only if credentials present and not simulating
twilio_client = None
if not SIMULATE and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        logger.exception("Failed to initialize Twilio client")

# ---------------------------
# Helpers
# ---------------------------
def check_env_vars():
    required = ["OPENAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "SERVER_URL"]
    if not SIMULATE:
        required += ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"]
    return [k for k in required if not os.getenv(k)]

def redact_phone(phone: str) -> str:
    p = str(phone)
    return p[:5] + "*****" + p[-2:] if len(p) > 7 else "*****"

def truncate(text: str, n: int = 500) -> str:
    return text if text and len(text) <= n else (text[:n] + "…") if text else ""

def cleanup_old_audio(retention_hours: int = MP3_RETENTION_HOURS):
    cutoff = time.time() - (retention_hours * 3600)
    for path in glob.glob(os.path.join(AUDIO_DIR, "*.mp3")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info("cleanup_old_audio: removed %s", path)
        except Exception:
            logger.exception("cleanup_old_audio: failed to remove %s", path)

# ---------------------------
# OpenAI helper
# ---------------------------
def generate_gpt_response(prompt: str, retries: int = RETRY_ATTEMPTS,
                          timeout_seconds: int = OPENAI_TIMEOUT,
                          max_tokens: int = OPENAI_MAX_TOKENS) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": max_tokens
    }

    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(OPENAI_API_URL, json=payload, headers=headers, timeout=timeout_seconds)
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI returned {resp.status_code}: {truncate(resp.text)}")
            j = resp.json()
            text = ""
            if isinstance(j, dict):
                choices = j.get("choices", [])
                if choices and isinstance(choices, list):
                    msg = choices[0].get("message", {})
                    text = msg.get("content") or ""
            text = text.strip()
            if not text:
                text = "Hi, this is Sara Hayes from Noblecom Solutions. I wanted to quickly reach out about opportunities to grow your revenue. Can we find a time to connect?"
            return text
        except Exception as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError("OpenAI generation failed") from last_exc

# ---------------------------
# ElevenLabs TTS helper
# ---------------------------
def generate_voice_file(text: str, voice_id: str = ELEVENLABS_VOICE_ID,
                        retries: int = RETRY_ATTEMPTS,
                        tts_timeout: int = ELEVENLABS_TTS_TIMEOUT) -> str:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not configured")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID not configured")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}

    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=tts_timeout) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"ElevenLabs returned {r.status_code}: {truncate(r.text)}")
                filename = f"{uuid.uuid4().hex}.mp3"
                filepath = os.path.join(AUDIO_DIR, filename)
                with open(filepath, "wb") as fw:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fw.write(chunk)
                return filename
        except Exception as e:
            last_exc = e
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError("TTS generation failed") from last_exc

# ---------------------------
# Twilio helper
# ---------------------------
def create_twilio_call(phone: str, twiml_url: str) -> str:
    if SIMULATE:
        logger.info("SIMULATE: skipping call to %s", redact_phone(phone))
        return "SIMULATED"
    if not twilio_client:
        raise RuntimeError("Twilio client not configured")
    call = twilio_client.calls.create(to=phone, from_=TWILIO_PHONE_NUMBER, url=twiml_url, timeout=60)
    return call.sid

# ---------------------------
# Routes
# ---------------------------
@app.route("/", methods=["GET"])
def health_check():
    missing = check_env_vars()
    status = 200 if not missing else 500
    return jsonify({"status": "ok" if status == 200 else "misconfigured", "missing_env": missing}), status

@app.route("/call_audio/<filename>", methods=["GET"])
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, conditional=True)

@app.route("/twiml/<filename>", methods=["GET"])
def twiml(filename):
    audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{filename}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
</Response>"""
    return Response(xml, mimetype="application/xml")

@app.route("/outbound", methods=["POST"])
def outbound_call():
    cleanup_old_audio()
    data = request.get_json(silent=True) or {}
    name = data.get("name") or ""
    phone = data.get("phone") or ""

    if not name or not phone:
        return jsonify({"error": "Missing name or phone"}), 400

    prompt = f"""
You are Sara Hayes, a Senior Growth Consultant at Noblecom Solutions.

Your role: persuade business owners to book a strategy call with you. 
You must sound 100% human — warm, confident, consultative, and a little urgent. 
Avoid sounding like a robot, script, or salesperson reading lines.

Conversation rules:
- Start with a natural introduction: name, role, and a reason for calling. Keep it casual but confident.
- Quickly move into a business pain point: missed revenue, lost leads, or inefficient marketing.
- Use short, natural sentences. Occasionally use fillers like “honestly”, “you know”, “right”, or a quick pause — this makes you sound human.
- Handle objections smoothly.
- Always circle back to urgency: lost revenue, missed opportunities, competitors pulling ahead.
- End by booking a meeting: {CALENDLY_LINK}.
- Never mention AI or scripts. You are Sara, a real consultant.

Tone: confident, empathetic, persuasive, human. 
Goal: book the meeting.
"""

    try:
        gpt_response = generate_gpt_response(prompt, max_tokens=OPENAI_MAX_TOKENS, timeout_seconds=OPENAI_TIMEOUT)
        audio_file = generate_voice_file(gpt_response)
        audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{audio_file}"
        twiml_url = f"{SERVER_URL.rstrip('/')}/twiml/{audio_file}"
        call_sid = create_twilio_call(phone, twiml_url)
        return jsonify({"status": "queued", "call_sid": call_sid, "audio_url": audio_url, "message_text": gpt_response}), 200
    except Exception as e:
        logger.exception("OUTBOUND FAILED: %s", str(e))
        return jsonify({"error": "internal server error", "message": "check server logs for details"}), 500

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
