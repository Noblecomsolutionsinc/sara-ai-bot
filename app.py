# app.py
import os
import csv
import logging
import time
import urllib.parse
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory
from twilio.rest import Client
from redis import Redis
from rq import Queue
import json
import requests

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")

# --- App ---
app = Flask(__name__, static_folder="static")

# --- Environment Variables ---
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
SERVER_URL = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL", "wss://sara-ai-streaming.onrender.com/ws")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
QUEUE_NAME = os.environ.get("QUEUE_NAME", "audio")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))

# --- Redis queue ---
redis_conn = Redis.from_url(REDIS_URL)
q = Queue(QUEUE_NAME, connection=redis_conn)

# --- Twilio client ---
client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        log.info("✅ Twilio client initialized")
    except Exception as e:
        log.exception("Failed to initialize Twilio client: %s", e)
else:
    log.warning("Twilio credentials not set; client disabled")

# --- Load Sara JSONs ---
DATA_DIR = Path("data")
SARA_CALLFLOW = json.loads((DATA_DIR / "Sara_CallFlow.json").read_text(encoding="utf-8"))
SARA_KNOWLEDGE = json.loads((DATA_DIR / "Sara_KnowledgeBase.json").read_text(encoding="utf-8"))
SARA_OBJECTIONS = json.loads((DATA_DIR / "Sara_Objections.json").read_text(encoding="utf-8"))
SARA_OPENING = json.loads((DATA_DIR / "Sara_Opening.json").read_text(encoding="utf-8"))
SARA_PLAYBOOK = json.loads((DATA_DIR / "Sara_Playbook.json").read_text(encoding="utf-8"))
SARA_SYSTEM = json.loads((DATA_DIR / "Sara_SystemPrompt_Production.json").read_text(encoding="utf-8"))

# --- Utilities ---
def detect_business_type(business_name: str) -> str:
    if not business_name:
        return "general"
    name_lower = business_name.lower()
    industry_keywords = {
        "dermatology clinic": ['dermatology', 'derm', 'skin', 'cosmetic', 'aesthetic', 'laser', 'botox'],
        "law firm": ['law', 'legal', 'attorney', 'lawyer', 'firm', 'advocate', 'counsel'],
        "dental practice": ['dental', 'dentist', 'teeth', 'smile', 'orthodontist'],
        "medical practice": ['medical', 'clinic', 'hospital', 'health', 'wellness', 'doctor'],
        "accounting firm": ['accounting', 'accountant', 'tax', 'cpa', 'financial', 'audit'],
        "real estate agency": ['real estate', 'realtor', 'property', 'broker'],
        "insurance agency": ['insurance', 'policy', 'coverage'],
        "technology company": ['tech', 'software', 'digital', 'it', 'computer', 'solution'],
        "marketing agency": ['marketing', 'media', 'advertising', 'brand', 'agency'],
        "construction company": ['construction', 'contractor', 'build', 'renovation', 'remodel'],
        "restaurant": ['restaurant', 'cafe', 'bistro', 'grill', 'kitchen', 'food'],
        "retail store": ['shop', 'store', 'retail', 'boutique', 'market'],
    }
    for industry, keywords in industry_keywords.items():
        if any(k in name_lower for k in keywords):
            return industry
    return "general"

def enqueue_audio_processing(call_id: str, audio_base64: str):
    """Enqueue audio for background processing."""
    from worker import process_call_audio
    q.enqueue(process_call_audio, call_id, audio_base64)

def enqueue_tts(text: str, filename: str):
    """Enqueue TTS generation."""
    from worker import generate_tts
    q.enqueue(generate_tts, text, filename)

def safe_initiate_call(to_number: str, name: str = "Unknown Business"):
    if not client:
        log.error("Twilio client not configured")
        return None
    business_type = detect_business_type(name)
    try:
        encoded_name = urllib.parse.quote(name)
        encoded_type = urllib.parse.quote(business_type)
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound?business_name={encoded_name}&business_type={encoded_type}",
            method="GET",
            timeout=30
        )
        log.info("✅ Outbound call initiated SID=%s to=%s", call.sid, to_number)
        return call
    except Exception as e:
        log.exception("Failed to create call: %s", e)
        return None

# --- Routes ---
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({
        "status": "ok",
        "twilio_configured": bool(client),
        "openai_configured": bool(OPENAI_API_KEY),
        "elevenlabs_configured": bool(ELEVENLABS_API_KEY),
        "gpt_model": "gpt-5-mini"
    })

@app.route("/static/tts/<path:filename>")
def serve_tts(filename):
    tts_dir = Path(app.static_folder) / "tts"
    if not tts_dir.exists():
        return "Not Found", 404
    return send_from_directory(str(tts_dir.resolve()), filename, conditional=True)

@app.route("/outbound", methods=["GET", "POST"])
def outbound():
    """Twilio webhook: play greeting and start streaming."""
    try:
        business_name = request.args.get("business_name") or request.form.get("business_name") or "Unknown Business"
        business_type = request.args.get("business_type") or detect_business_type(business_name)
        encoded_business_name = urllib.parse.quote(business_name)
        encoded_business_type = urllib.parse.quote(business_type)
        stream_url = f"{PUBLIC_STREAMING_URL}?business_name={encoded_business_name}&business_type={encoded_business_type}"

        tts_path = Path(app.static_folder) / "tts"
        per_type_file = tts_path / f"{business_type.replace(' ', '_')}_greeting.mp3"
        general_file = tts_path / "general_greeting.mp3"
        if per_type_file.exists():
            greeting_url = f"{SERVER_URL}/static/tts/{per_type_file.name}"
        elif general_file.exists():
            greeting_url = f"{SERVER_URL}/static/tts/{general_file.name}"
        else:
            greeting_url = None

        if greeting_url:
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{greeting_url}</Play>
    <Start><Stream url="{stream_url}"/></Start>
</Response>'''
        else:
            twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start><Stream url="{stream_url}"/></Start>
    <Say>Please wait while we connect you.</Say>
</Response>'''

        return Response(twiml, mimetype="text/xml")
    except Exception as e:
        log.exception("Error building TwiML: %s", e)
        return Response("<Response></Response>", mimetype="text/xml")

@app.route("/test_call/<phone_number>", methods=["POST", "GET"])
def test_call(phone_number):
    if not phone_number.startswith("+"):
        return jsonify({"error": "phone number must include country code (+1...)" }), 400
    business_name = request.args.get("business_name", "Test Business")
    call = safe_initiate_call(phone_number, business_name)
    if call:
        return jsonify({"status": "initiated", "call_sid": getattr(call, "sid", None)})
    return jsonify({"error": "failed to initiate call"}), 500

# --- Run campaign from CSV ---
def run_campaign(csv_path="contacts.csv", limit=None):
    if not client:
        log.error("Twilio client not configured; cannot run campaign")
        return 0
    csv_path = Path(csv_path)
    if not csv_path.exists():
        log.error("contacts.csv missing at %s", csv_path)
        return 0
    count = 0
    with open(csv_path, newline='', encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if limit and count >= limit:
                break
            name = row.get("name", "Unknown Business").strip()
            phone = row.get("phone", None)
            if not phone:
                log.warning("Skipping %s - no phone", name)
                continue
            phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
            if not phone.startswith("+"):
                phone = "+1" + phone if len(phone) == 10 else "+" + phone
            safe_initiate_call(phone, name)
            count += 1
            time.sleep(2.0)
    log.info("Campaign finished. Calls placed: %d", count)
    return count

@app.route("/run_campaign", methods=["POST"])
def run_campaign_endpoint():
    token = os.environ.get("CAMPAIGN_TRIGGER_TOKEN")
    req_token = request.headers.get("X-Run-Token") or request.form.get("token")
    if token and req_token != token:
        return jsonify({"error": "unauthorized"}), 403
    limit = request.args.get("limit")
    limit = int(limit) if limit and limit.isdigit() else None
    count = run_campaign(limit=limit)
    return jsonify({"status": "started", "attempted": count})

# --- CLI entry ---
if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
