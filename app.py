# app.py — Enterprise-ready Flask web app for Sara AI
import os
import csv
import time
import logging
import urllib.parse
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory
from twilio.rest import Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")

# --- App ---
app = Flask(__name__, static_folder="static")

# --- Env / Config ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL", "wss://sara-ai-streaming.onrender.com/ws")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")
REDIS_URL = os.environ.get("REDIS_URL")  # optional - used by worker/async flow
CAMPAIGN_TRIGGER_TOKEN = os.environ.get("CAMPAIGN_TRIGGER_TOKEN")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "24"))
CALL_PACING_SECONDS = float(os.environ.get("CALL_PACING_SECONDS", "2.0"))
DEFAULT_GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-5-mini")

# Twilio client
client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("✅ Twilio client initialized")
    except Exception as e:
        log.exception("Failed to init Twilio client: %s", e)
        client = None
else:
    log.warning("Twilio credentials missing — Twilio client disabled")

# --- Utilities ---
def detect_business_type(business_name: str) -> str:
    if not business_name:
        return "general"
    name_lower = business_name.lower()
    keywords = {
        "dermatology clinic": ['dermatology', 'dermatologist', 'derm', 'skin', 'cosmetic', 'aesthetic'],
        "law firm": ['law', 'legal', 'attorney', 'lawyer', 'firm', 'advocate'],
        "dental practice": ['dental', 'dentist', 'teeth', 'smile', 'orthodontist'],
        "medical practice": ['medical', 'clinic', 'hospital', 'health', 'doctor', 'physician'],
        "marketing agency": ['marketing', 'media', 'advertising', 'brand', 'agency'],
    }
    for industry, kws in keywords.items():
        if any(k in name_lower for k in kws):
            return industry
    return "general"

def normalize_phone(phone: str) -> str:
    if not phone:
        return ""
    p = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if p.startswith("+"):
        return p
    if p.isdigit():
        if len(p) == 11 and p.startswith("1"):
            return "+" + p
        # default to US +1 if length 10
        if len(p) == 10:
            return "+1" + p
    return p

# --- Health ---
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({
        "status": "ok",
        "twilio_configured": bool(client),
        "openai_configured": bool(OPENAI_API_KEY),
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "gpt_model": DEFAULT_GPT_MODEL
    }), 200

# --- Serve static TTS files (safe) ---
@app.route("/static/tts/<path:filename>")
def serve_tts(filename):
    tts_dir = Path(app.static_folder) / "tts"
    if not tts_dir.exists():
        return "Not Found", 404
    # send_from_directory handles range/conditional requests
    return send_from_directory(str(tts_dir.resolve()), filename, conditional=True)

# --- TwiML outbound webhook ---
@app.route("/outbound", methods=["GET", "POST"])
def outbound():
    """
    Twilio webhook used by Twilio when starting an outbound call.
    Returns TwiML which optionally <Play>s a greeting mp3 then <Start><Stream> to streaming server.
    Query params / form:
      ?business_name=...&business_type=...
    """
    try:
        # Accept args via GET or POST (Twilio REST call uses GET, Twilio webhook uses POST)
        business_name = request.args.get("business_name") or request.form.get("business_name") or request.values.get("business_name") or "Unknown Business"
        business_type = request.args.get("business_type") or request.form.get("business_type") or detect_business_type(business_name)
        # Build stream url and encode params
        encoded_business_name = urllib.parse.quote(business_name)
        encoded_business_type = urllib.parse.quote(business_type)
        stream_url = f"{PUBLIC_STREAMING_URL}?business_name={encoded_business_name}&business_type={encoded_business_type}"
        # Attempt to find a greeting mp3 under static/tts/
        tts_dir = Path(app.static_folder) / "tts"
        per_type_file = tts_dir / f"{business_type.replace(' ', '_')}_greeting.mp3"
        general_file = tts_dir / "general_greeting.mp3"
        greeting_url = None
        if per_type_file.exists():
            greeting_url = f"{SERVER_URL}/static/tts/{per_type_file.name}"
        elif general_file.exists():
            greeting_url = f"{SERVER_URL}/static/tts/{general_file.name}"

        if greeting_url:
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{greeting_url}</Play>
  <Start><Stream url="{stream_url}"/></Start>
</Response>'''
        else:
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Please wait while we connect your call.</Say>
  <Start><Stream url="{stream_url}"/></Start>
  <Pause length="1"/>
</Response>'''
        log.info("TwiML outbound for business=%s type=%s play=%s", business_name, business_type, bool(greeting_url))
        return Response(twiml, mimetype="text/xml")
    except Exception as e:
        log.exception("Error building TwiML: %s", e)
        return Response("<Response></Response>", mimetype="text/xml")

# --- Test call endpoint (initiates call via Twilio) ---
@app.route("/test_call/<phone_number>", methods=["GET", "POST"])
def test_call(phone_number):
    if not phone_number.startswith("+"):
        return jsonify({"error": "Phone number must include country code (+1...)" }), 400
    business_name = request.args.get("business_name", "Test Business")
    # normalize phone and call
    phone = normalize_phone(phone_number)
    if not client:
        log.error("Twilio client not configured")
        return jsonify({"error": "twilio not configured"}), 500
    try:
        encoded_name = urllib.parse.quote(business_name)
        encoded_type = urllib.parse.quote(detect_business_type(business_name))
        call = client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound?business_name={encoded_name}&business_type={encoded_type}",
            method="GET",
            timeout=30
        )
        return jsonify({"status": "initiated", "call_sid": getattr(call, "sid", None)}), 200
    except Exception as e:
        log.exception("Failed to initiate call: %s", e)
        return jsonify({"error": str(e)}), 500

# --- Campaign runner (reads contacts.csv and triggers calls) ---
CSV_PATH = Path("contacts.csv")
CALLED_PATH = Path("contacts_called.csv")

def mark_called_row(row):
    # Append row to contacts_called.csv (safe append)
    headers = ["name", "phone", "industry", "timestamp", "call_sid"]
    file_exists = CALLED_PATH.exists()
    with open(CALLED_PATH, "a", newline='', encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

def run_campaign(limit=None):
    if not client:
        log.error("Twilio client not configured; aborting campaign")
        return 0
    if not CSV_PATH.exists():
        log.error("contacts.csv missing")
        return 0
    count = 0
    with open(CSV_PATH, newline='', encoding='utf-8') as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if limit and count >= limit:
                break
            name = row.get("name") or "Unknown"
            phone = normalize_phone(row.get("phone") or "")
            industry = row.get("industry") or detect_business_type(name)
            if not phone:
                log.warning("Skipping %s: no phone", name)
                continue
            try:
                encoded_name = urllib.parse.quote(name)
                encoded_type = urllib.parse.quote(industry)
                call = client.calls.create(
                    to=phone,
                    from_=TWILIO_PHONE_NUMBER,
                    url=f"{SERVER_URL}/outbound?business_name={encoded_name}&business_type={encoded_type}",
                    method="GET",
                    timeout=30
                )
                count += 1
                log.info("Placed call to %s (sid=%s)", name, getattr(call, "sid", None))
                # mark as called locally
                mark_called_row({
                    "name": name, "phone": phone, "industry": industry,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "call_sid": getattr(call, "sid", "")
                })
                time.sleep(CALL_PACING_SECONDS)
            except Exception as e:
                log.exception("Failed to call %s: %s", name, e)
    log.info("Campaign finished: attempted=%d", count)
    return count

@app.route("/run_campaign", methods=["POST"])
def run_campaign_endpoint():
    # Optional token gate
    token_required = bool(CAMPAIGN_TRIGGER_TOKEN)
    if token_required:
        req_token = request.headers.get("X-Run-Token") or request.form.get("token")
        if req_token != CAMPAIGN_TRIGGER_TOKEN:
            log.warning("Unauthorized campaign trigger")
            return jsonify({"error": "unauthorized"}), 403
    limit = request.args.get("limit")
    limit = int(limit) if limit and limit.isdigit() else None
    count = run_campaign(limit=limit)
    return jsonify({"status":"done","attempted":count}), 200

# --- Detect business route ---
@app.route("/detect_business/<business_name>", methods=["GET"])
def detect_business_route(business_name):
    return jsonify({"business_name": business_name, "detected_type": detect_business_type(business_name)}), 200

# --- CLI entrypoint ---
if __name__ == "__main__":
    mode = os.environ.get("MODE","server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        log.info("Starting Sara AI Bot on port %s", port)
        app.run(host="0.0.0.0", port=port)
