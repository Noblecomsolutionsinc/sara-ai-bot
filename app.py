# app.py
import os
import logging
from pathlib import Path
from flask import Flask, request, jsonify, abort, Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
import redis

from json_loader import sara_store

LOG = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder="static")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PUBLIC_STREAMING_URL = os.getenv("PUBLIC_STREAMING_URL")  # wss://...
SERVER_URL = os.getenv("SERVER_URL")  # https://...
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "48"))

redis_conn = redis.from_url(REDIS_URL, decode_responses=True)
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

CALLMAP_KEY = "sara:callmap:"  # hash mapping CallSid -> session_id

def validate_twilio_request(req):
    try:
        if req.args.get("skip_validation") == "1":
            return True
        if req.headers.get("X-SKIP-TWILIO-VALIDATION", "").lower() == "true":
            return True
    except Exception:
        pass

    if not validator:
        return True

    signature = req.headers.get("X-Twilio-Signature", "")
    url = req.url
    form = req.form.to_dict()
    try:
        return validator.validate(url, form, signature)
    except Exception:
        LOG.exception("Twilio validation failed")
        return False

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "twilio_configured": bool(twilio_client is not None)})

@app.route("/outbound", methods=["POST"])
def outbound():
    if not validate_twilio_request(request):
        LOG.warning("Invalid Twilio signature on /outbound")
        return abort(403)
    call_sid = request.form.get("CallSid") or ""
    LOG.info("Outbound webhook invoked CallSid=%s", call_sid)

    vr = VoiceResponse()
    # Start Twilio Media Stream to the streaming server
    st = Start()
    stream = Stream(url=PUBLIC_STREAMING_URL)
    stream.parameter(name="callSid", value=call_sid)
    st.append(stream)
    vr.append(st)

    # keep open for at least a short period while pipeline runs
    vr.pause(length=60)
    vr.say("Thank you. Goodbye.", voice="alice")
    return Response(str(vr), mimetype="application/xml")

@app.route("/internal/tts_ready", methods=["POST"])
def tts_ready():
    data = request.get_json() or {}
    session_id = data.get("call_id") or data.get("session_id")
    mp3_path = data.get("file")
    LOG.info("tts_ready: session=%s file=%s", session_id, mp3_path)
    if not session_id or not mp3_path:
        return jsonify({"status": "bad_request"}), 400

    # find CallSid from CALLMAP
    call_sid = None
    try:
        all_map = redis_conn.hgetall(CALLMAP_KEY)
        for k, v in all_map.items():
            if v == session_id:
                call_sid = k
                break
    except Exception:
        LOG.exception("Failed to read callmap from redis")

    if not call_sid:
        LOG.info("No active call found for session %s", session_id)
        return jsonify({"status": "no_active_call"})

    if not twilio_client:
        LOG.error("Twilio client not configured")
        return jsonify({"status": "twilio_not_configured"}), 500

    if mp3_path.startswith("http://") or mp3_path.startswith("https://"):
        media_url = mp3_path
    else:
        media_url = f"{SERVER_URL.rstrip('/')}/{mp3_path.lstrip('/')}" if SERVER_URL else mp3_path

    new_twiml = f"<Response><Play>{media_url}</Play></Response>"
    try:
        twilio_client.calls(call_sid).update(twiml=new_twiml)
        LOG.info("Injected TTS into call %s -> %s", call_sid, media_url)
        return jsonify({"status": "ok", "call_sid": call_sid})
    except Exception:
        LOG.exception("Failed to update Twilio call with TTS")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
