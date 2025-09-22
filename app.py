# app.py — Robust Sara AI outbound: OpenAI (requests w/ timeout) -> ElevenLabs (streamed) -> Twilio
import os
import uuid
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, request, send_from_directory, jsonify, Response
from dotenv import load_dotenv
import requests
from twilio.rest import Client as TwilioClient

# Load .env
load_dotenv()

# ---------------------------
# Config / env
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))  # seconds
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
ELEVENLABS_TTS_TIMEOUT = int(os.getenv("ELEVENLABS_TTS_TIMEOUT", "120"))
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")  # e.g. https://sara-ai-bot.onrender.com
SARA_NAME = os.getenv("SARA_NAME", "Sara")
SARA_ROLE = os.getenv("SARA_ROLE", "Digital Marketing Consultant")
SARA_COMPANY = os.getenv("COMPANY_NAME", "")
CALENDLY_LINK = os.getenv("MEETING_LINK", "")

# Retry/backoff policy
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "1"))

# ---------------------------
# App setup
# ---------------------------
app = Flask(__name__)
AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sara-ai")

# Initialize Twilio client only if credentials present
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

# ---------------------------
# Utility & checks
# ---------------------------
def check_env_vars():
    required = [
        "OPENAI_API_KEY",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
        "SERVER_URL",
    ]
    missing = [k for k in required if not os.getenv(k)]
    return missing

# ---------------------------
# OpenAI (HTTP) helper using requests with timeout & retries
# ---------------------------
def generate_gpt_response(prompt: str, retries: int = RETRY_ATTEMPTS, timeout_seconds: int = OPENAI_TIMEOUT) -> str:
    """Call OpenAI Chat Completions via requests with enforced timeout and retries."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": prompt}]
    }

    start = time.time()
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            logger.info("OpenAI: attempt %d start @ %s", attempt, datetime.utcnow().isoformat())
            resp = requests.post(OPENAI_API_URL, json=payload, headers=headers, timeout=timeout_seconds)
            text_resp = resp.text[:2000]
            if resp.status_code != 200:
                logger.warning("OpenAI: non-200 status=%s body=%s", resp.status_code, text_resp)
                raise RuntimeError(f"OpenAI returned status {resp.status_code}: {text_resp}")
            j = resp.json()
            # safe path to extract text
            choice = j.get("choices", [{}])[0]
            message = choice.get("message") or {}
            text = message.get("content") or choice.get("text") or ""
            duration = time.time() - start
            logger.info("OpenAI: success attempt=%d duration=%.2fs chars=%d", attempt, duration, len(text or ""))
            return text
        except Exception as e:
            last_exc = e
            logger.warning("OpenAI: attempt %d failed: %s", attempt, str(e))
            if attempt <= retries:
                sleep_for = RETRY_BACKOFF_SECONDS * attempt
                logger.info("OpenAI: sleeping %.1fs before retry", sleep_for)
                time.sleep(sleep_for)
            else:
                duration = time.time() - start
                logger.error("OpenAI: final failure after %.2fs", duration, exc_info=True)
                raise RuntimeError(f"OpenAI generation failed after {attempt} attempts: {str(e)}") from e

# ---------------------------
# ElevenLabs TTS helper (streaming)
# ---------------------------
def generate_voice_file(text: str, voice_id: str = ELEVENLABS_VOICE_ID, retries: int = RETRY_ATTEMPTS, tts_timeout: int = ELEVENLABS_TTS_TIMEOUT) -> str:
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not configured")
    if not voice_id:
        raise RuntimeError("ELEVENLABS_VOICE_ID not configured")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}

    start = time.time()
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            logger.info("TTS: attempt %d start @ %s", attempt, datetime.utcnow().isoformat())
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=tts_timeout) as r:
                if r.status_code != 200:
                    body = r.text[:1000]
                    logger.warning("TTS: non-200 status=%s body=%s", r.status_code, body)
                    raise RuntimeError(f"ElevenLabs returned status {r.status_code}: {body}")
                filename = f"{uuid.uuid4().hex}.mp3"
                filepath = os.path.join(AUDIO_DIR, filename)
                with open(filepath, "wb") as fw:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fw.write(chunk)
                duration = time.time() - start
                logger.info("TTS: success attempt=%d duration=%.2fs file=%s", attempt, duration, filename)
                return filename
        except Exception as e:
            last_exc = e
            logger.warning("TTS: attempt %d failed: %s", attempt, str(e))
            if attempt <= retries:
                sleep_for = RETRY_BACKOFF_SECONDS * attempt
                logger.info("TTS: sleeping %.1fs before retry", sleep_for)
                time.sleep(sleep_for)
            else:
                duration = time.time() - start
                logger.error("TTS: final failure after %.2fs", duration, exc_info=True)
                raise RuntimeError(f"TTS failed after {attempt} attempts: {str(e)}") from e

# ---------------------------
# Twilio helper
# ---------------------------
def create_twilio_call(phone: str, twiml_url: str) -> str:
    if not twilio_client:
        raise RuntimeError("Twilio credentials not configured (TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)")
    if not TWILIO_PHONE_NUMBER:
        raise RuntimeError("TWILIO_PHONE_NUMBER not configured")
    try:
        logger.info("Twilio: creating call to %s with twiml %s", phone, twiml_url)
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
            timeout=60
        )
        logger.info("Twilio: call created sid=%s", call.sid)
        return call.sid
    except Exception as e:
        logger.exception("Twilio: call creation failed")
        raise RuntimeError(f"Twilio call failed: {str(e)}") from e

# ---------------------------
# Routes
# ---------------------------
@app.route("/", methods=["GET"])
def health_check():
    missing = check_env_vars()
    return jsonify({"status": "ok", "missing_env": missing}), (500 if missing else 200)

@app.route("/call_audio/<filename>", methods=["GET"])
def serve_audio(filename):
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "audio file not found"}), 404
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
    request_start = time.time()
    logger.info("OUTBOUND START @ %s", datetime.utcnow().isoformat())

    data = request.get_json(silent=True) or {}

    name = (
        data.get("name")
        or data.get("contact_name")
        or data.get("full_name")
        or (data.get("contact") or {}).get("name")
        or ""
    )
    phone = (
        data.get("phone")
        or data.get("to_phone")
        or data.get("phone_number")
        or (data.get("contact") or {}).get("phone")
        or data.get("to")
        or ""
    )

    name = name.strip() if isinstance(name, str) else ""
    phone = phone.strip() if isinstance(phone, str) else ""

    if not name or not phone:
        logger.warning("OUTBOUND: missing name or phone in request: %r", data)
        return jsonify({"error": "Missing name or phone"}), 400

    logger.info("OUTBOUND REQUEST: name=%s phone=%s", name, phone)

    prompt = (
        f"You are {SARA_NAME}, a {SARA_ROLE} at {SARA_COMPANY}.\n"
        f"Call {name}. Introduce yourself professionally, create urgency, explain potential lost revenue, handle objections, and book a meeting at {CALENDLY_LINK}.\n"
    )

    try:
        gpt_start = time.time()
        gpt_response = generate_gpt_response(prompt)
        gpt_duration = time.time() - gpt_start
        logger.info("OUTBOUND: GPT done duration=%.2fs chars=%d", gpt_duration, len(gpt_response or ""))

        tts_start = time.time()
        audio_file = generate_voice_file(gpt_response)
        audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{audio_file}"
        tts_duration = time.time() - tts_start
        logger.info("OUTBOUND: TTS done duration=%.2fs file=%s", tts_duration, audio_file)

        twiml_url = f"{SERVER_URL.rstrip('/')}/twiml/{audio_file}"
        call_sid = create_twilio_call(phone, twiml_url)

        total_duration = time.time() - request_start
        logger.info("OUTBOUND COMPLETE duration=%.2fs name=%s phone=%s call_sid=%s", total_duration, name, phone, call_sid)

        return jsonify({
            "status": "queued",
            "call_sid": call_sid,
            "audio_url": audio_url,
            "message_text": gpt_response
        }), 200

    except Exception as e:
        duration = time.time() - request_start
        logger.error("OUTBOUND FAILED duration=%.2fs error=%s", duration, str(e), exc_info=True)
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Run (for local testing)
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
