# app.py
import os
import logging
import csv
from pathlib import Path
from flask import Flask, request, Response, jsonify, abort
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
import redis
import json

from json_loader import sara_store

LOG = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder="static")

# Env
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SERVER_URL = os.environ.get("SERVER_URL")  # public URL of this Flask app
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL")  # wss://streaming-host
CAMPAIGN_TRIGGER_TOKEN = os.environ.get("CAMPAIGN_TRIGGER_TOKEN", "")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "48"))

redis_conn = redis.from_url(REDIS_URL)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

# Redis keys
CALLMAP_KEY = "sara:callmap:"  # a hash mapping CallSid -> session_id and state

def map_call(call_sid, session_id):
    redis_conn.hset(CALLMAP_KEY, call_sid, session_id)

def get_session_for_call(call_sid):
    return redis_conn.hget(CALLMAP_KEY, call_sid)

def respond_403():
    return abort(403)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "elevenlabs_configured": bool(os.getenv("ELEVENLABS_API_KEY")),
        "gpt_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "status": "ok",
        "twilio_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    })

def validate_twilio_request(req):
    # Validate signature if possible
    if not validator:
        return True
    url = request.url
    post_vars = request.form.to_dict()
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(url, post_vars, signature)

@app.route("/outbound", methods=["POST"])
def outbound():
    # Twilio webhook when call connects
    if not validate_twilio_request(request):
        LOG.warning("Invalid Twilio signature")
        return respond_403()
    call_sid = request.form.get("CallSid")
    LOG.info("outbound called by Twilio CallSid=%s", call_sid)
    vr = VoiceResponse()
    # play greeting if exists
    greeting = Path("static/tts/general_greeting.mp3")
    if greeting.exists():
        vr.play(f"{SERVER_URL}/static/tts/{greeting.name}")
    else:
        vr.say(f"Hi, this is {os.getenv('SARA_NAME','Sara')} from {os.getenv('COMPANY_NAME')}. One moment please.", voice="alice")
    # Start Twilio Media Stream
    st = Start()
    stream = Stream(url=PUBLIC_STREAMING_URL)
    # pass callSid to streaming service via parameter
    stream.parameter(name="callSid", value=call_sid)
    st.append(stream)
    vr.append(st)
    return Response(str(vr), mimetype="application/xml")

@app.route("/internal/tts_ready", methods=["POST"])
def tts_ready():
    """
    Worker -> POST here when MP3 generated.
    If the call is still active, instruct Twilio to play the MP3 into the call
    by updating the call's TwiML to a small TwiML that plays the file (via Call.fetch + update).
    """
    data = request.get_json() or {}
    call_id = data.get("call_id")  # in our system this is session_id
    mp3_path = data.get("file")
    LOG.info("tts_ready callback: session=%s file=%s", call_id, mp3_path)
    # find Twilio CallSid for the session
    # we stored callSid->session mapping in Redis at streaming_server start
    # reverse lookup: scan CALLMAP_KEY
    call_sid = None
    # Simple approach: iterate hash (small)
    for k, v in redis_conn.hgetall(CALLMAP_KEY).items():
        if v.decode() == call_id:
            call_sid = k.decode()
            break
    if not call_sid:
        LOG.info("No active call found for session %s; skipping live-play", call_id)
        return jsonify({"status": "no-active-call"})
    # create a tiny TwiML to play the MP3 and append hold/return TwiML
    if not twilio_client:
        LOG.error("Twilio client not configured; cannot play into active call")
        return jsonify({"status": "no-twilio"})
    try:
        media_url = mp3_path if mp3_path.startswith("http") else f"{SERVER_URL}/{mp3_path.lstrip('/')}"
        # Update call to fetch new TwiML that plays the file
        new_twiml = f"<Response><Play>{media_url}</Play></Response>"
        twilio_client.calls(call_sid).update(twiml=new_twiml)
        LOG.info("Injected TTS into call %s -> %s", call_sid, media_url)
        return jsonify({"status": "ok", "call_sid": call_sid})
    except Exception:
        LOG.exception("Failed to instruct Twilio to play mp3")
        return jsonify({"status": "error"}), 500

@app.route("/run_campaign", methods=["POST"])
def run_campaign():
    token = request.args.get("token")
    secret = CAMPAIGN_TRIGGER_TOKEN
    if secret and token != secret:
        return jsonify({"error": "unauthorized"}), 401
    contacts_file = Path("contacts.csv")
    if not contacts_file.exists():
        return jsonify({"error": "contacts.csv missing"}), 400
    out = []
    for row in csv.DictReader(open(contacts_file, newline="", encoding="utf-8")):
        phone = row.get("phone")
        try:
            call = twilio_client.calls.create(
                to=phone,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{SERVER_URL}/outbound"
            )
            LOG.info("Initiated call to %s Sid=%s", phone, call.sid)
            out.append({"phone": phone, "sid": call.sid})
        except Exception as e:
            LOG.exception("Call failed for %s", phone)
            out.append({"phone": phone, "error": str(e)})
    return jsonify(out)

@app.route("/test_call/<phone>", methods=["GET"])
def test_call(phone):
    if not twilio_client:
        return jsonify({"error": "twilio not configured"}), 500
    try:
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound"
        )
        LOG.info("Test call created %s -> %s", phone, call.sid)
        return jsonify({"call_sid": call.sid})
    except Exception as e:
        LOG.exception("test_call failed")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
