# app.py
import os
import csv
import time
import logging
from flask import Flask, Response, jsonify, request
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
app = Flask(__name__)

# --- Required env vars (fail fast) ---
REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "BOT_URL"  # e.g. https://sara-ai-bot.onrender.com
]
missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    logging.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
BOT_URL = os.environ["BOT_URL"].rstrip("/")
OUTBOUND_PATH = os.environ.get("OUTBOUND_PATH", "/outbound").lstrip("/")
OUTBOUND_URL = f"{BOT_URL}/{OUTBOUND_PATH}"

CALL_DELAY_SECONDS = float(os.environ.get("CALL_DELAY_SECONDS", "0.8"))  # keep it conservative

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

# Outbound TwiML endpoint that Twilio will request when initiating the call
@app.route("/outbound", methods=["POST"])
def outbound():
    # Optional: log Twilio params for debugging (CallSid, To, From)
    logging.info("Outbound TwiML requested by Twilio. Params: %s", dict(request.form))
    TWILIO_MEDIA_WS_URL = os.environ.get("TWILIO_MEDIA_WS_URL")
    if not TWILIO_MEDIA_WS_URL:
        logging.error("TWILIO_MEDIA_WS_URL is not set")
        return Response("<Response><Say>Server misconfiguration</Say></Response>", mimetype="text/xml", status=500)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting you with Sara...</Say>
  <Connect>
    <Stream url="{TWILIO_MEDIA_WS_URL}" />
  </Connect>
</Response>
"""
    return Response(twiml, mimetype="text/xml")

# Campaign runner
def run_campaign(csv_path="contacts.csv"):
    if not os.path.exists(csv_path):
        logging.error("contacts.csv not found at %s", csv_path)
        raise SystemExit("contacts.csv not found")

    logging.info("Starting outbound campaign — OUTBOUND_URL=%s", OUTBOUND_URL)
    with open(csv_path, newline='', encoding='utf-8') as f:
        # Normalize fieldnames so common capitalization mistakes don't break you
        reader = csv.DictReader(f)
        headers = [h.strip().lower() for h in reader.fieldnames] if reader.fieldnames else []
        count = 0
        for row in reader:
            # Lowercase keys
            row_normal = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            name = row_normal.get("name", "unknown")
            phone = row_normal.get("phone") or row_normal.get("mobile") or row_normal.get("number")
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
                logging.info("Call initiated SID=%s", call.sid)
                count += 1
            except Exception as e:
                logging.exception("Failed to start call to %s (%s): %s", name, phone, e)
            time.sleep(CALL_DELAY_SECONDS)
        logging.info("Campaign finished — attempted calls: %d", count)

if __name__ == "__main__":
    mode = os.environ.get("MODE", "server")
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        logging.info("Starting Flask server on %s", port)
        app.run(host="0.0.0.0", port=port)
