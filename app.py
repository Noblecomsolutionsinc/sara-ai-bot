# app.py
import os
import csv
import time
import logging
from flask import Flask, Response, jsonify, request, abort
from twilio.rest import Client
# from twilio.request_validator import RequestValidator  # optional: validate Twilio requests

# -------------------------
# Basic logging + Flask app
# -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)

# -------------------------
# Required env vars (fail fast)
# -------------------------
REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "SERVER_URL"   # e.g. https://sara-ai-bot.onrender.com
]

missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    logging.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
SERVER_URL = os.environ["SERVER_URL"].rstrip("/")

# Optional tuning env vars
CALL_DELAY_SECONDS = float(os.environ.get("CALL_DELAY_SECONDS", "0.8"))  # gap between outbound dials
OUTBOUND_PATH = os.environ.get("OUTBOUND_PATH", "/outbound").lstrip("/")
OUTBOUND_URL = f"{SERVER_URL}/{OUTBOUND_PATH}"

# If present, use this; otherwise fall back to the hard-coded streaming server (your spec)
TWILIO_MEDIA_WS_URL = os.environ.get("TWILIO_MEDIA_WS_URL", "wss://sara-ai-streaming.onrender.com/ws")

# Twilio client (blocking)
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Optional Twilio request validation (recommended in production)
# If you want to enable validation, uncomment and set TWILIO_VALIDATE=true in your env.
# validator = RequestValidator(TWILIO_AUTH_TOKEN)
# def validate_twilio_request(request):
#     signature = request.headers.get("X-Twilio-Signature", "")
#     url = request.url
#     post_vars = request.form.to_dict()
#     return validator.validate(url, post_vars, signature)

# -------------------------
# Health endpoint (used by Render)
# -------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# -------------------------
# Outbound TwiML endpoint
# -------------------------
@app.route(f"/{OUTBOUND_PATH}", methods=["POST"])
def outbound():
    # Optional validation:
    if os.environ.get("TWILIO_VALIDATE", "false").lower() == "true":
        # Uncomment import and validator above to enable
        # if not validate_twilio_request(request):
        #     logging.warning("Twilio request validation failed")
        #     abort(403)
        pass

    # Log Twilio payload for debugging
    try:
        params = dict(request.form)
    except Exception:
        params = {}
    logging.info("Outbound TwiML requested. Twilio params: %s", {k: params.get(k) for k in ["CallSid","To","From"]})

    # Return TwiML that opens a MediaStream to your streaming server
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting you with Sara. Please hold.</Say>
  <Connect>
    <Stream url="{TWILIO_MEDIA_WS_URL}" />
  </Connect>
</Response>
"""
    return Response(twiml, mimetype="text/xml")

# -------------------------
# Campaign runner
# -------------------------
def run_campaign(csv_path="contacts.csv"):
    if not os.path.exists(csv_path):
        logging.error("contacts.csv not found at %s", csv_path)
        raise SystemExit("contacts.csv not found")

    logging.info("Starting outbound campaign — OUTBOUND_URL=%s", OUTBOUND_URL)
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            logging.error("contacts.csv has no header row")
            raise SystemExit("contacts.csv missing headers")

        # Normalise header names to lower-case so CSV variants don't break things
        for row in reader:
            # lower-case keys, strip whitespace on values
            row_norm = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            name = row_norm.get("name", "unknown")
            phone = row_norm.get("phone") or row_norm.get("mobile") or row_norm.get("number")
            if not phone:
                logging.warning("Skipping row without phone: %s", row)
                continue

            logging.info("Dialing %s (%s)", name, phone)
            try:
                call = client.calls.create(
                    to=phone,
                    from_=TWILIO_PHONE_NUMBER,
                    url=OUTBOUND_URL,
                    method="POST"
                )
                logging.info("Call initiated SID=%s for %s", call.sid, name)
            except Exception as e:
                logging.exception("Failed to start call to %s (%s): %s", name, phone, e)

            time.sleep(CALL_DELAY_SECONDS)

    logging.info("Campaign finished")

# -------------------------
# Entry point
# -------------------------
if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        logging.info("Starting Flask server on port %s (OUTBOUND_PATH=%s)", port, OUTBOUND_PATH)
        # For Render, use the plain python start; you can also use gunicorn:
        # gunicorn -w 4 -b 0.0.0.0:$PORT app:app
        app.run(host="0.0.0.0", port=port)
