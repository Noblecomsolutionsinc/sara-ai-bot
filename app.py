# app.py
import os
import csv
import time
import logging
from flask import Flask, Response, jsonify, request
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")
app = Flask(__name__)

# --- Required env vars (fail fast) ---
REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "SERVER_URL",
    "TWILIO_MEDIA_WS_URL"
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
SERVER_URL = os.environ["SERVER_URL"].rstrip("/")
TWILIO_MEDIA_WS_URL = os.environ["TWILIO_MEDIA_WS_URL"]

# optional
CALL_DELAY_SECONDS = float(os.environ.get("CALL_DELAY_SECONDS", "0.8"))
OUTBOUND_PATH = os.environ.get("OUTBOUND_PATH", "outbound")
OUTBOUND_URL = f"{SERVER_URL}/{OUTBOUND_PATH.lstrip('/')}"

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Health endpoint (responds to GET and HEAD — Render probes HEAD)
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"}), 200

# Twilio will fetch this TwiML when making outbound calls
@app.route(f"/{OUTBOUND_PATH}", methods=["POST"])
def outbound():
    # Optional: log minimal Twilio params for debugging
    try:
        params = {"CallSid": request.form.get("CallSid"), "To": request.form.get("To"), "From": request.form.get("From")}
    except Exception:
        params = {}
    log.info("Outbound TwiML requested — Twilio params: %s", params)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting you with Sara. Please hold.</Say>
  <Connect>
    <Stream url="{TWILIO_MEDIA_WS_URL}" />
  </Connect>
</Response>
"""
    return Response(twiml, mimetype="text/xml")

# Campaign runner: dials contacts.csv sequentially, 1 at a time
def run_campaign(csv_path="contacts.csv"):
    if not os.path.exists(csv_path):
        log.error("contacts.csv not found at %s", csv_path)
        raise SystemExit("contacts.csv not found")

    log.info("Starting campaign — OUTBOUND_URL=%s", OUTBOUND_URL)
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log.error("contacts.csv missing headers")
            raise SystemExit("contacts.csv missing headers")
        count = 0
        for row in reader:
            # normalise keys to reduce CSV header issues
            rown = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            name = rown.get("name", "unknown")
            phone = rown.get("phone") or rown.get("mobile") or rown.get("number")
            if not phone:
                log.warning("Skipping row without phone: %s", row)
                continue
            log.info("Dialing %s (%s)", name, phone)
            try:
                call = client.calls.create(
                    to=phone,
                    from_=TWILIO_PHONE_NUMBER,
                    url=OUTBOUND_URL,
                    method="POST"
                )
                log.info("Call initiated SID=%s for %s", call.sid, name)
                count += 1
            except Exception:
                log.exception("Failed to start call to %s (%s)", name, phone)
            time.sleep(CALL_DELAY_SECONDS)
    log.info("Campaign finished — attempted calls: %d", count)

if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        log.info("Starting Flask server on port %s", port)
        app.run(host="0.0.0.0", port=port)
