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
import json

# Load .env
load_dotenv()

# ---------------------------
# Config / env
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "600"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.0"))

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

# For streaming
STREAMING_WS_URL = os.getenv("TWILIO_MEDIA_WS_URL", "wss://sara-ai-streaming.onrender.com")

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "1"))

SIMULATE = os.getenv("SIMULATE", "false").strip().lower() in ("1", "true", "yes")
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "24"))

# Logging
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

# Twilio client
twilio_client = None
if not SIMULATE and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        logger.exception("Failed to initialize Twilio client")

# SaraBrain
brain = None
try:
    from sara_brain import SaraBrain
    brain = SaraBrain()
    logger.info("SaraBrain loaded successfully.")
except Exception as e:
    logger.warning("SaraBrain not loaded or failed: %s", str(e))

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
# GPT helper
# ---------------------------
def generate_gpt_response(messages, retries: int = RETRY_ATTEMPTS,
                          timeout_seconds: int = OPENAI_TIMEOUT,
                          max_tokens: int = OPENAI_MAX_TOKENS,
                          expect_json: bool = True):
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    system_json_prompt = """You are Sara, an AI sales consultant.
Always respond with ONLY a valid JSON object:

{
  "sara_text": "string with Sara's spoken reply",
  "action": "optional string or null"
}"""

    full_messages = [{"role": "system", "content": system_json_prompt}] + messages
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": full_messages,
        "max_completion_tokens": max_tokens,
        "temperature": OPENAI_TEMPERATURE,
        "response_format": {"type": "json_object"}
    }

    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(OPENAI_API_URL, json=payload, headers=headers, timeout=timeout_seconds)
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI API error {resp.status_code}: {truncate(resp.text)}")
            j = resp.json()
            raw_text = j.get("choices", [])[0].get("message", {}).get("content", "")
            if not raw_text.strip():
                return {"sara_text": "[Error: empty reply]", "action": None}
            try:
                parsed = json.loads(raw_text)
                return {"sara_text": parsed.get("sara_text", ""), "action": parsed.get("action")}
            except Exception:
                return {"sara_text": raw_text, "action": None}
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
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    for attempt in range(1, retries + 2):
        try:
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=tts_timeout) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"ElevenLabs error {r.status_code}: {truncate(r.text)}")
                filename = f"{uuid.uuid4().hex}.mp3"
                filepath = os.path.join(AUDIO_DIR, filename)
                with open(filepath, "wb") as fw:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fw.write(chunk)
                return filename
        except Exception:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError("TTS generation failed")

# ---------------------------
# Twilio helper
# ---------------------------
def create_twilio_call(phone: str, twiml_url: str) -> str:
    if SIMULATE:
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
    return jsonify({"status": "ok" if not missing else "misconfigured", "missing_env": missing}), 200

@app.route("/call_audio/<filename>", methods=["GET"])
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, conditional=True)

@app.route("/twiml/<filename>", methods=["GET", "POST"])
def twiml(filename):
    audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{filename}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
</Response>"""
    return Response(xml, mimetype="application/xml")

# 🔹 NEW STREAMING ROUTE
@app.route("/twiml/stream", methods=["POST", "GET"])
def twiml_stream():
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="{STREAMING_WS_URL}" />
    </Start>
    <Say voice="alice">Hi, this is Sara from Noblecom Solutions. One moment while I connect.</Say>
</Response>"""
    return Response(xml, mimetype="application/xml")

@app.route("/outbound", methods=["POST"])
def outbound_call():
    cleanup_old_audio()
    data = request.get_json(silent=True) or {}
    name = data.get("name", "")
    phone = data.get("phone", "")
    if not name or not phone:
        return jsonify({"error": "Missing name or phone"}), 400
    prompt = f"You are {SARA_NAME}, {SARA_ROLE} at {SARA_COMPANY}. Persuade {name} to book a call. Meeting link: {CALENDLY_LINK}."
    try:
        gpt_response = generate_gpt_response([{"role": "user", "content": prompt}], expect_json=True)
        gpt_text = gpt_response.get("sara_text", "")
        audio_file = generate_voice_file(gpt_text)
        twiml_url = f"{SERVER_URL.rstrip('/')}/twiml/{audio_file}"
        call_sid = create_twilio_call(phone, twiml_url)
        return jsonify({"status": "queued", "call_sid": call_sid, "message_text": gpt_text}), 200
    except Exception as e:
        return jsonify({"error": "internal server error"}), 500

@app.route("/chat", methods=["POST"])
def chat_with_sara():
    cleanup_old_audio()
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("user_input", "")
    do_tts = bool(payload.get("tts", False))
    if not brain:
        return jsonify({"error": "SaraBrain not available"}), 500
    sara_resp = brain.get_response(user_input)
    audio_file, audio_url = None, None
    if do_tts and sara_resp.get("sara_text"):
        audio_file = generate_voice_file(sara_resp["sara_text"])
        audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{audio_file}"
    return jsonify({"response": sara_resp, "audio_file": audio_file, "audio_url": audio_url}), 200

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
