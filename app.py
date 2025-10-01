# app.py
import os
import logging
from flask import Flask, request, Response, jsonify
import redis
import html

# ------ Logging ------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sara_ai_bot")

# ------ Config ------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PUBLIC_STREAMING_URL = os.getenv("PUBLIC_STREAMING_URL")  # e.g. "wss://sara-ai-streaming.onrender.com/ws"
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
SARA_NAME = os.getenv("SARA_NAME", "Sara Hayes")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Noblecom Solutions")

if not PUBLIC_STREAMING_URL:
    logger.warning("PUBLIC_STREAMING_URL is not set; make sure Twilio can reach your streaming server.")

# ------ Redis (sync client for the Flask app) ------
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# ------ Flask app ------
app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "sara-ai-bot"}), 200


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """
    Twilio webhook to start a call's Media Stream.
    Expects:
      - Query params (from call_runner.py): name, industry
      - Twilio POST form field: CallSid (Twilio will supply on initial request)
    Behavior:
      - Store system_prompt in Redis under call:{call_sid}:meta so streaming_server picks it up.
      - Return TwiML that instructs Twilio to <Start><Stream url="PUBLIC_STREAMING_URL">...
    """
    # Quick dev bypass for Twilio validation if needed (do NOT use in prod)
    skip_validation = request.args.get("skip_validation", request.form.get("skip_validation", "0")) == "1"

    # Extract personalization from query params (call_runner.py sets them on the URL)
    name = request.args.get("name") or request.form.get("name") or "there"
    industry = request.args.get("industry") or request.form.get("industry") or "your industry"

    # Twilio CallSid — Twilio usually sends this as POST form field CallSid
    call_sid = request.values.get("CallSid") or request.args.get("CallSid") or request.form.get("callSid") or None
    if not call_sid:
        # It's possible Twilio hits this URL before CallSid is populated (rare for outbound), but we still proceed.
        # We'll generate a fallback id to keep things traceable; streaming server will supply a streamSid later.
        call_sid = f"call-fallback-{os.urandom(6).hex()}"
        logger.debug("No CallSid provided in webhook; generated fallback: %s", call_sid)

    # Build a dynamic system prompt for this call
    # Keep the prompt concise and instructive (avoid leaking secrets)
    # Escape user-provided fields to avoid accidental injection into TTS/GPT contexts
    safe_name = html.escape(name)
    safe_industry = html.escape(industry)
    system_prompt = (
        f"You are {SARA_NAME}, a friendly and professional outbound AI caller representing {COMPANY_NAME}. "
        f"You are calling {safe_name}, who works in the {safe_industry} industry. "
        "Your goal is to quickly build rapport and book a meeting. Be human-like, concise, and when interrupted stop speaking immediately and listen. "
        "If the prospect asks for pricing or technical detail, offer to set a follow-up meeting with a specialist. "
        "Do not ask for sensitive personal data."
    )

    # Persist system prompt and metadata in Redis so the streaming server can use it when the stream 'start' arrives
    meta_key = f"call:{call_sid}:meta"
    try:
        r.hset(meta_key, mapping={
            "system_prompt": system_prompt,
            "name": safe_name,
            "industry": safe_industry,
            "status": "incoming",
            "playback_active": "0",
            "request_playback_stop": "0"
        })
        # Optionally set a mapping for sara:callmap to find streamSid by CallSid if needed
        # r.hset("sara:callmap", call_sid, "")  # streaming_server will populate streamSid later if necessary
        logger.info("Saved system_prompt for call %s (name=%s, industry=%s)", call_sid, safe_name, safe_industry)
    except Exception as e:
        logger.exception("Failed to write system_prompt into Redis for call %s: %s", call_sid, e)

    # Build TwiML to start a Media Stream pointing to our PUBLIC_STREAMING_URL/ws (Twilio expects a wss endpoint)
    # Twilio Media Streams expects <Start><Stream url="wss://..."><Parameter name="callSid" value="..."/></Stream></Start>
    if not PUBLIC_STREAMING_URL:
        logger.error("PUBLIC_STREAMING_URL missing — cannot start Media Stream. Returning empty TwiML.")
        resp_xml = "<?xml version='1.0' encoding='UTF-8'?><Response><Say>Server misconfigured. No streaming URL set.</Say></Response>"
        return Response(resp_xml, mimetype="application/xml")

    # Ensure the stream URL includes /ws or the correct path; allow admins to set PUBLIC_STREAMING_URL accordingly
    stream_url = PUBLIC_STREAMING_URL.rstrip("/")

    # TwiML for starting the stream
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{stream_url}">
      <Parameter name="callSid" value="{call_sid}"/>
      <Parameter name="name" value="{html.escape(name)}"/>
      <Parameter name="industry" value="{html.escape(industry)}"/>
    </Stream>
  </Start>
  <!-- Optionally play a short hold or greeting until the AI responds -->
  <Say voice="alice">Please hold while I connect you.</Say>
</Response>"""

    logger.info("Returning TwiML Start Stream for CallSid=%s -> %s", call_sid, stream_url)
    return Response(twiml, mimetype="application/xml")


# Optional endpoint to inspect call metadata (debug)
@app.route("/call_meta/<call_sid>", methods=["GET"])
def call_meta(call_sid):
    try:
        meta = r.hgetall(f"call:{call_sid}:meta") or {}
        history = r.get(f"call:{call_sid}:history") or "[]"
        return jsonify({"meta": meta, "history": history})
    except Exception as e:
        logger.exception("Failed to fetch meta for %s: %s", call_sid, e)
        return jsonify({"error": "failed to fetch"}), 500


if __name__ == "__main__":
    # Only used for local testing. In production Render runs via gunicorn app:app
    port = int(os.getenv("PORT", 5000))
    logger.info("Starting sara-ai-bot Flask on 0.0.0.0:%d (development)", port)
    app.run(host="0.0.0.0", port=port, debug=False)
