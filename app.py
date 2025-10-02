#!/usr/bin/env python3
"""
app.py

Flask app providing:
- /healthz
- /voice (start Twilio Media Stream)
- /status (lazy cleanup + archive)
- /internal/tts_ready (worker callback to store or push TTS playback into live call)

Important:
- Twilio signature validation is optional and controlled via SKIP_TWILIO_VALIDATION or ENABLE_TWILIO_VALIDATION env flags.
- Archived results stored at `sara:result:{call_sid}` with 30-day TTL.
"""
from __future__ import annotations

import os
import json
import logging
import time
import html
from datetime import datetime, timezone
from typing import Optional

from flask import Flask, request, jsonify, Response
import redis

# Twilio SDK for updating live calls (optional)
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator  # type: ignore
    TWILIO_VALIDATOR_AVAILABLE = True
except Exception:
    TwilioClient = None
    RequestValidator = None
    TWILIO_VALIDATOR_AVAILABLE = False

# Celery task entrypoint (light-weight)
from tasks import process_event  # tasks imports celery_app.worker internally

# ------------------------
# Config
# ------------------------
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PUBLIC_STREAMING_URL = os.getenv("PUBLIC_STREAMING_URL", "").rstrip("/")
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
SKIP_TWILIO_VALIDATION = os.getenv("SKIP_TWILIO_VALIDATION", "false").lower() in ("1", "true", "yes")
ENABLE_TWILIO_VALIDATION = os.getenv("ENABLE_TWILIO_VALIDATION", "false").lower() in ("1", "true", "yes")
ARCHIVE_TTL_SECONDS = int(os.getenv("ARCHIVE_TTL_SECONDS", str(30 * 24 * 3600)))
CLEANUP_GRACE_SECONDS = int(os.getenv("CLEANUP_GRACE_SECONDS", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ------------------------
# Logging & clients
# ------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("app")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

twilio_client: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TwilioClient is not None:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio REST client initialized.")
    except Exception:
        logger.exception("Failed to initialize Twilio client.")
        twilio_client = None

twilio_validator = None
if not SKIP_TWILIO_VALIDATION and ENABLE_TWILIO_VALIDATION and TWILIO_VALIDATOR_AVAILABLE and TWILIO_AUTH_TOKEN:
    try:
        twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
        logger.info("Twilio RequestValidator initialized.")
    except Exception:
        logger.exception("Failed to initialize Twilio RequestValidator; validation disabled.")
        twilio_validator = None
else:
    if SKIP_TWILIO_VALIDATION:
        logger.info("SKIP_TWILIO_VALIDATION is set — Twilio signature validation disabled.")
    elif not ENABLE_TWILIO_VALIDATION:
        logger.info("ENABLE_TWILIO_VALIDATION not set — Twilio signature validation disabled.")
    else:
        logger.info("Twilio RequestValidator not available.")

app = Flask(__name__, static_folder="static", static_url_path="/static")


def _epoch_to_iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _validate_twilio_request() -> bool:
    if SKIP_TWILIO_VALIDATION:
        return True
    if not twilio_validator:
        logger.warning("Twilio validator not configured; rejecting request.")
        return False

    signature = request.headers.get("X-Twilio-Signature", "")
    url = request.url
    params = {}
    if request.form:
        params = {k: v for k, v in request.form.items()}
    else:
        params = {k: v for k, v in request.args.items()}

    try:
        valid = twilio_validator.validate(url, params, signature)
        if not valid:
            logger.warning("Twilio signature validation failed for %s", url)
        return valid
    except Exception:
        logger.exception("Twilio validation exception")
        return False


@app.route("/healthz", methods=["GET"])
def healthz():
    try:
        redis_ok = False
        try:
            redis_ok = r.ping()
        except Exception:
            redis_ok = False

        celery_ok = False
        try:
            process_event.delay({"type": "ping", "ts": time.time()})
            celery_ok = True
        except Exception:
            celery_ok = False
            logger.exception("Failed to enqueue ping to Celery")

        return jsonify({"status": "ok", "redis_ok": bool(redis_ok), "celery_ok": bool(celery_ok)}), 200
    except Exception:
        logger.exception("Health check failed")
        return jsonify({"status": "error"}), 500


@app.route("/voice", methods=["GET", "POST"])
def voice():
    if not _validate_twilio_request():
        return jsonify({"error": "invalid_twilio_signature"}), 403

    # personalize
    name = request.values.get("name") or "there"
    industry = request.values.get("industry") or "your industry"
    call_sid = request.values.get("CallSid") or request.form.get("CallSid") or f"call-fallback-{os.urandom(6).hex()}"

    safe_name = html.escape(name)
    safe_industry = html.escape(industry)

    system_prompt = (
        "You are Sara Hayes, a human sales representative for Noblecom Solutions. "
        "Your goal is to engage callers naturally, qualify leads, and book meetings. "
        "You must never admit or reveal you are an AI, bot, or automated system. "
        "If directly asked whether you are AI, politely decline and redirect the conversation or end the call. "
        "Keep responses short, conversational, and human-like for phone calls."
    )

    meta_key = f"call:{call_sid}:meta"
    now = time.time()
    try:
        r.hset(meta_key, mapping={
            "system_prompt": system_prompt,
            "name": safe_name,
            "industry": safe_industry,
            "status": "incoming",
            "playback_active": "0",
            "request_playback_stop": "0",
            "started_at_epoch": now,
            "started_at_iso": _epoch_to_iso(now)
        })
    except Exception:
        logger.exception("Failed to write meta for %s", call_sid)

    if not PUBLIC_STREAMING_URL:
        logger.error("PUBLIC_STREAMING_URL not set; cannot start Media Stream.")
        return Response("<?xml version='1.0' encoding='UTF-8'?><Response><Say>Server misconfigured.</Say></Response>", mimetype="application/xml")

    stream_url = PUBLIC_STREAMING_URL.rstrip("/")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{stream_url}">
      <Parameter name="callSid" value="{html.escape(call_sid)}"/>
      <Parameter name="name" value="{html.escape(name)}"/>
      <Parameter name="industry" value="{html.escape(industry)}"/>
    </Stream>
  </Start>
  <Say voice="alice">Please hold while I connect you.</Say>
</Response>"""
    logger.info("Returning TwiML Start Stream for CallSid=%s", call_sid)
    return Response(twiml, mimetype="application/xml")


@app.route("/status", methods=["POST"])
def status():
    if not _validate_twilio_request():
        return jsonify({"error": "invalid_twilio_signature"}), 403

    payload = request.form or request.get_json(silent=True) or {}
    call_sid = payload.get("CallSid") or request.values.get("CallSid")
    call_status = payload.get("CallStatus") or request.values.get("CallStatus")

    if not call_sid:
        logger.warning("status webhook missing CallSid")
        return jsonify({"error": "missing_callSid"}), 400

    meta_key = f"call:{call_sid}:meta"
    hist_key = f"call:{call_sid}:history"
    audio_key = f"call:{call_sid}:audio_buf"
    archive_key = f"sara:result:{call_sid}"

    # Already archived?
    if r.exists(archive_key):
        logger.info("Call %s already archived", call_sid)
        return jsonify({"status": "already_archived"}), 200

    meta = r.hgetall(meta_key) or {}
    stopped_at_epoch = float(meta.get("stopped_at_epoch", "0") or 0)
    cleanup_at_epoch = float(meta.get("cleanup_at_epoch", "0") or 0)
    now = time.time()

    if not stopped_at_epoch:
        # first stop event -> schedule cleanup
        stopped_at_epoch = now
        stopped_at_iso = _epoch_to_iso(stopped_at_epoch)
        cleanup_at_epoch = now + CLEANUP_GRACE_SECONDS
        cleanup_at_iso = _epoch_to_iso(cleanup_at_epoch)
        r.hset(meta_key, mapping={
            "status": call_status or "stopped",
            "stopped_at_epoch": stopped_at_epoch,
            "stopped_at_iso": stopped_at_iso,
            "cleanup_at_epoch": cleanup_at_epoch,
            "cleanup_at_iso": cleanup_at_iso
        })
        logger.info("Call %s stopping, cleanup scheduled at %s", call_sid, cleanup_at_iso)
        return jsonify({"status": "stopping", "cleanup_at": cleanup_at_iso}), 200

    if now < cleanup_at_epoch:
        logger.info("Call %s still in grace period until %s", call_sid, _epoch_to_iso(cleanup_at_epoch))
        return jsonify({"status": "waiting", "cleanup_at": _epoch_to_iso(cleanup_at_epoch)}), 200

    # Time to archive
    history_json_list = r.lrange(hist_key, 0, -1) or []
    history_objs = []
    for item in history_json_list:
        try:
            history_objs.append(json.loads(item))
        except Exception:
            history_objs.append({"raw": item})

    audio_buf = r.lrange(audio_key, 0, -1) or []
    meta_map = r.hgetall(meta_key) or {}
    now_epoch = time.time()
    archive_obj = {
        "call_sid": call_sid,
        "meta": meta_map,
        "history": history_objs,
        "audio_buf": audio_buf,
        "archived_at_epoch": now_epoch,
        "archived_at_iso": _epoch_to_iso(now_epoch)
    }

    r.setex(archive_key, ARCHIVE_TTL_SECONDS, json.dumps(archive_obj))
    logger.info("Archived and cleaned up call %s into %s", call_sid, archive_key)

    # Delete live keys
    r.delete(meta_key, hist_key, audio_key)

    return jsonify({"status": "archived"}), 200


@app.route("/internal/tts_ready", methods=["POST"])
def internal_tts_ready():
    """
    Called by workers to indicate TTS mp3 is ready.
    Accepts JSON: {"call_id": "<CallSid>", "file": "<mp3_url>", "hangup": true|false}
    If Twilio client available and call exists, update call TwiML; otherwise store last_tts_url in Redis meta.
    """
    if not _validate_twilio_request():
        if not SKIP_TWILIO_VALIDATION:
            return jsonify({"error": "invalid_twilio_signature"}), 403

    try:
        payload = request.get_json(force=True)
    except Exception:
        payload = request.form or {}
    call_id = payload.get("call_id") or payload.get("callSid") or payload.get("call")
    file_url = payload.get("file")
    hangup = bool(payload.get("hangup", False))

    if not call_id or not file_url:
        return jsonify({"error": "missing_call_or_file"}), 400

    # If Twilio client available, update call TwiML
    if twilio_client:
        try:
            twiml_parts = [f"<Play>{html.escape(file_url)}</Play>"]
            if hangup:
                twiml_parts.append("<Hangup/>")
            twiml = "<?xml version='1.0' encoding='UTF-8'?><Response>" + "".join(twiml_parts) + "</Response>"
            updated = twilio_client.calls(call_id).update(twiml=twiml)
            logger.info("Updated live call %s with TTS (hangup=%s)", call_id, hangup)
            return jsonify({"status": "updated", "sid": getattr(updated, "sid", None)}), 200
        except Exception:
            logger.exception("Failed to update Twilio call %s with TTS", call_id)
            # Fall back to saving in Redis meta
    try:
        meta_key = f"call:{call_id}:meta"
        r.hset(meta_key, mapping={"last_tts_url": file_url})
        logger.info("Stored last_tts_url in Redis meta for call %s -> %s", call_id, file_url)
        return jsonify({"status": "stored"}), 200
    except Exception:
        logger.exception("Failed to store last_tts_url for %s", call_id)
        return jsonify({"error": "internal"}), 500


if __name__ == "__main__":
    logger.info("Starting Flask app on 0.0.0.0:%s", WEB_PORT)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
