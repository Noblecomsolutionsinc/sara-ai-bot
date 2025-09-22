# app.py — robust Sara AI outbound (OpenAI -> ElevenLabs -> Twilio)
import os
import uuid
import time
import logging
import traceback
from flask import Flask, request, send_from_directory, jsonify, Response
from dotenv import load_dotenv
import requests
from openai import OpenAI
from twilio.rest import Client as TwilioClient

load_dotenv()

# ---------- Config / env ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")  # e.g. https://sara-ai-bot.onrender.com
SARA_NAME = os.getenv("SARA_NAME", "Sara")
SARA_ROLE = os.getenv("SARA_ROLE", "Digital Marketing Consultant")
SARA_COMPANY = os.getenv("COMPANY_NAME", "")
CALENDLY_LINK = os.getenv("MEETING_LINK", "")

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2  # seconds (exponential)

# ---------- App setup ----------
app = Flask(__name__)
AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sara-ai")

# Client init (do not blow up if keys missing; we'll check later)
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None

# ---------- Helpers ----------
def check_env_vars():
    missing = []
    for k in ["OPENAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "SERVER_URL"]:
        if not os.getenv(k):
            missing.append(k)
    return missing

def generate_gpt_response(prompt, max_attempts=RETRY_ATTEMPTS):
    if not openai_client:
        raise RuntimeError("OPENAI_API_KEY not configured")

    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info("OpenAI: request attempt %d", attempt)
            # Use the explicit OpenAI client
            resp = openai_client.chat.completions.create(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": prompt}],
                # you can add max_tokens here if you want to cap
            )
            # Newer SDK returns .choices[0].message.content
            return resp.choices[0].message.content
        except Exception as e:
            logger.warning("OpenAI call failed (attempt %d): %s", attempt, str(e))
            if attempt >= max_attempts:
                logger.exception("OpenAI: final failure")
                raise
            time.sleep(RETRY_BACKOFF ** attempt)

def generate_voice_file(text, voice_id=ELEVENLABS_VOICE_ID, max_attempts=RETRY_ATTEMPTS):
    """Call ElevenLabs TTS HTTP API and stream to file (memory-safe). Returns filename."""
    if not ELEVENLABS_API_KEY or not voice_id:
        raise RuntimeError("ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID not configured")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}
    }

    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info("ElevenLabs TTS request attempt %d", attempt)
            with requests.post(url, json=payload, headers=headers, stream=True, timeout=120) as r:
                if r.status_code != 200:
                    text_resp = r.text
                    logger.warning("ElevenLabs returned %s: %s", r.status_code, text_resp)
                    raise RuntimeError(f"ElevenLabs TTS error: {r.status_code} {text_resp}")
                # stream to disk
                filename = f"{uuid.uuid4().hex}.mp3"
                filepath = os.path.join(AUDIO_DIR, filename)
                with open(filepath, "wb") as fw:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            fw.write(chunk)
                logger.info("Saved TTS to %s", filepath)
                return filename
        except Exception as e:
            logger.warning("ElevenLabs attempt %d failed: %s", attempt, str(e))
            if attempt >= max_attempts:
                logger.exception("ElevenLabs: final failure")
                raise
            time.sleep(RETRY_BACKOFF ** attempt)

def create_twilio_call(phone, twiml_url):
    if not twilio_client:
        raise RuntimeError("Twilio credentials not configured")
    try:
        logger.info("Creating Twilio call to %s (twiml %s)", phone, twiml_url)
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url,
            timeout=60
        )
        logger.info("Twilio call created: SID=%s", call.sid)
        return call.sid
    except Exception as e:
        logger.exception("Twilio call creation failed")
        raise

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def health_check():
    missing = check_env_vars()
    body = {"status": "ok", "missing_env": missing}
    return jsonify(body), (500 if missing else 200)

@app.route("/call_audio/<filename>", methods=["GET"])
def serve_audio(filename):
    # Serve mp3 file; let web server handle range requests if needed
    filepath = os.path.join(AUDIO_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "audio file not found"}), 404
    # send_from_directory will set mime type correctly
    return send_from_directory(AUDIO_DIR, filename, conditional=True)

@app.route("/twiml/<filename>", methods=["GET"])
def twiml(filename):
    # TwiML: Tell Twilio to play the hosted MP3
    audio_url = f"{SERVER_URL.rstrip('/')}/call_audio/{filename}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{audio_url}</Play>
</Response>"""
    return Response(xml, mimetype="application/xml")

@app.route("/outbound", methods=["POST"])
def outbound_call():
    # Accept flexible payload shapes (name, contact_name, phone, to, to_phone, phone_number, contact:{...})
    data = request.get_json(silent=True) or {}
    # Support legacy fields
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
        return jsonify({"error": "Missing name or phone"}), 400

    logger.info("OUTBOUND request: %s -> %s", name, phone)

    # Build the prompt (tweak to your needs)
    prompt = (
        f"You are {SARA_NAME}, a {SARA_ROLE} at {SARA_COMPANY}.\n"
        f"Call {name} and introduce yourself professionally. Create urgency, explain potential lost revenue, handle objections, and book a meeting at {CALENDLY_LINK}.\n"
    )

    # Step 1: GPT
    try:
        gpt_response = generate_gpt_response(prompt)
        logger.info("GPT produced %d chars", len(gpt_response or ""))
    except Exception as e:
        logger.error("GPT error: %s", str(e))
        traceback.print_exc()
        return jsonify({"error": "GPT generation failed", "detail": str(e)}), 500

    # Step 2: TTS
    try:
        audio_file = generate_voice_file(gpt_response)
    except Exception as e:
        logger.error("TTS error: %s", str(e))
        traceback.print_exc()
        return jsonify({"error": "TTS generation failed", "detail": str(e)}), 500

    # Step 3: Create Twilio call (use TwiML that plays the hosted mp3)
    try:
        twiml_url = f"{SERVER_URL.rstrip('/')}/twiml/{audio_file}"
        call_sid = create_twilio_call(phone, twiml_url)
    except Exception as e:
        logger.error("Twilio error: %s", str(e))
        traceback.print_exc()
        return jsonify({"error": "Twilio call failed", "detail": str(e)}), 500

    return jsonify({
        "status": "queued",
        "call_sid": call_sid,
        "audio_url": f"{SERVER_URL.rstrip('/')}/call_audio/{audio_file}",
        "message_text": gpt_response
    }), 200

# ---------- Run ----------
if __name__ == "__main__":
    # when testing locally:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
