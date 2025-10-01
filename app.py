# app.py
import os
import logging
import csv
import json
from pathlib import Path
from flask import Flask, request, Response, jsonify, abort
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
import redis

from json_loader import sara_store

# Logging
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("app")

# App init
app = Flask(__name__, static_folder="static")

# Environment / config
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SERVER_URL = os.environ.get("SERVER_URL")  # public https://... for TwiML static files
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL")  # wss://...
CAMPAIGN_TRIGGER_TOKEN = os.environ.get("CAMPAIGN_TRIGGER_TOKEN", "")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", "48"))

# Redis / Twilio clients
redis_conn = redis.from_url(REDIS_URL)
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

# Redis keys
CALLMAP_KEY = "sara:callmap:"  # hash: CallSid -> session_id

def validate_twilio_request(req):
    """
    Validate Twilio request signature when token available.
    For local/manual testing you can bypass validation by:
      - adding ?skip_validation=1 to the URL, OR
      - sending header X-SKIP-TWILIO-VALIDATION: true

    IMPORTANT: do NOT leave bypass enabled in production unless you understand the risk.
    """
    # Explicit bypass for quick testing
    try:
        if req.args.get("skip_validation") == "1":
            LOG.info("Bypassing Twilio validation via query param skip_validation=1")
            return True
        if req.headers.get("X-SKIP-TWILIO-VALIDATION", "").lower() == "true":
            LOG.info("Bypassing Twilio validation via header X-SKIP-TWILIO-VALIDATION")
            return True
    except Exception:
        pass

    # If Twilio validator not configured, allow (useful for dev)
    if not validator:
        return True

    signature = req.headers.get("X-Twilio-Signature", "")
    url = req.url
    form = req.form.to_dict()
    try:
        return validator.validate(url, form, signature)
    except Exception:
        LOG.exception("Twilio request validation failed")
        return False


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "elevenlabs_configured": bool(os.getenv("ELEVENLABS_API_KEY")),
        "gpt_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "status": "ok",
        "twilio_configured": bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    })

@app.route("/outbound", methods=["POST"])
def outbound():
    # Twilio webhook when outbound call connects
    if not validate_twilio_request(request):
        LOG.warning("Invalid Twilio signature on /outbound")
        return abort(403)
    call_sid = request.form.get("CallSid") or request.form.get("CallSid".lower()) or ""
    LOG.info("outbound called by Twilio CallSid=%s", call_sid)

    vr = VoiceResponse()
    # Play greeting MP3 if present; fallback to Say
    greeting = Path("static/tts/general_greeting.mp3")
    if greeting.exists():
        # Use SERVER_URL to serve static file
        play_url = f"{SERVER_URL}/static/tts/{greeting.name}" if SERVER_URL else f"/static/tts/{greeting.name}"
        vr.play(play_url)
    else:
        vr.say(f"Hi, this is {os.getenv('SARA_NAME','Sara')} from {os.getenv('COMPANY_NAME','your company')}. One moment please.", voice="alice")

    # Start Twilio Media Stream to the streaming server
    st = Start()
    stream = Stream(url=PUBLIC_STREAMING_URL)
    # pass callSid so streaming server can correlate
    stream.parameter(name="callSid", value=call_sid)
    st.append(stream)
    vr.append(st)

    # KEEP THE CALL OPEN so worker has time to process and inject TTS
    # Tune pause length to your expected processing time (60 seconds is a reasonable default)
    vr.pause(length=60)

    # Fallback message if no TTS injected
    vr.say("Thanks — we'll follow up with an email shortly.", voice="alice")

    return Response(str(vr), mimetype="application/xml")


@app.route("/internal/tts_ready", methods=["POST"])
def tts_ready():
    """
    Worker posts here when it creates a TTS mp3.
    We attempt to find the active Twilio CallSid mapped to the worker's session id and inject TwiML to play the mp3.
    """
    data = request.get_json() or {}
    session_id = data.get("call_id")  # our session id (streaming server generated)
    mp3_path = data.get("file")
    LOG.info("tts_ready callback: session=%s file=%s", session_id, mp3_path)

    if not session_id or not mp3_path:
        return jsonify({"status": "bad_request"}), 400

    # Find CallSid -> session mapping (scan the hash; small cost)
    call_sid = None
    try:
        all_map = redis_conn.hgetall(CALLMAP_KEY)
        for k, v in all_map.items():
            try:
                k_dec = k.decode() if isinstance(k, bytes) else str(k)
                v_dec = v.decode() if isinstance(v, bytes) else str(v)
                if v_dec == session_id:
                    call_sid = k_dec
                    break
            except Exception:
                continue
    except Exception:
        LOG.exception("Failed to read callmap from Redis")

    if not call_sid:
        LOG.info("No active call found for session %s — skipping live-play", session_id)
        return jsonify({"status": "no-active-call"})

    if not twilio_client:
        LOG.error("Twilio client not configured; cannot play into active call")
        return jsonify({"status": "twilio-not-configured"}), 500

    # Build absolute URL for mp3
    if mp3_path.startswith("http://") or mp3_path.startswith("https://"):
        media_url = mp3_path
    else:
        # ensure leading slash removed
        media_url = f"{SERVER_URL.rstrip('/')}/{mp3_path.lstrip('/')}" if SERVER_URL else mp3_path

    # Create TwiML to play the mp3
    new_twiml = f"<Response><Play>{media_url}</Play></Response>"
    try:
        # Update the live call TwiML so Twilio plays the MP3 into the call
        twilio_client.calls(call_sid).update(twiml=new_twiml)
        LOG.info("Injected TTS into call %s -> %s", call_sid, media_url)
        return jsonify({"status": "ok", "call_sid": call_sid})
    except Exception:
        LOG.exception("Failed to update Twilio call with TTS")
        return jsonify({"status": "error"}), 500


@app.route("/run_campaign", methods=["POST"])
def run_campaign():
    token = request.args.get("token")
    if CAMPAIGN_TRIGGER_TOKEN and token != CAMPAIGN_TRIGGER_TOKEN:
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
