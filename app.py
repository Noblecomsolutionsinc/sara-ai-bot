# File: app.py - UPDATED MESSAGE
import os
import csv
import logging
import time
from flask import Flask, Response, jsonify, request
from twilio.rest import Client

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-app")

# --- Required env vars ---
REQUIRED = [
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN", 
    "TWILIO_PHONE_NUMBER",
    "SERVER_URL",
    "PUBLIC_STREAMING_URL"
]

missing = [v for v in REQUIRED if not os.environ.get(v)]
if missing:
    log.error("Missing required env vars: %s", missing)
    raise SystemExit(f"Missing required env vars: {missing}")

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
SERVER_URL = os.environ["SERVER_URL"].rstrip("/")
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")

# Ensure WebSocket URL uses wss://
if PUBLIC_STREAMING_URL.startswith('http://'):
    PUBLIC_STREAMING_URL = PUBLIC_STREAMING_URL.replace('http://', 'wss://', 1)
elif PUBLIC_STREAMING_URL.startswith('https://'):
    PUBLIC_STREAMING_URL = PUBLIC_STREAMING_URL.replace('https://', 'wss://', 1)
elif not PUBLIC_STREAMING_URL.startswith('wss://'):
    PUBLIC_STREAMING_URL = f"wss://{PUBLIC_STREAMING_URL}"

# Add WebSocket path if not present
if not PUBLIC_STREAMING_URL.endswith('/ws'):
    PUBLIC_STREAMING_URL = f"{PUBLIC_STREAMING_URL}/ws"

log.info("Using WebSocket URL: %s", PUBLIC_STREAMING_URL)

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
app = Flask(__name__)

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/outbound", methods=["POST"])
def outbound():
    try:
        twilio_params = {
            "CallSid": request.form.get("CallSid"),
            "To": request.form.get("To"),
            "From": request.form.get("From"),
        }
    except Exception:
        twilio_params = {}
    log.info("Outbound TwiML requested — params=%s", twilio_params)

    # ✅ Updated message as requested
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{PUBLIC_STREAMING_URL}"/>
  </Start>
  <Say voice="Polly.Joanna">Hi, Connecting you with Sara Hayes. Please hold. Your call is important to us.</Say>
</Response>
"""
    return Response(twiml, mimetype="text/xml")

def safe_initiate_call(to_number, name="unknown"):
    last_exc = None
    for attempt in range(1, 4):  # 3 attempts
        try:
            call = client.calls.create(
                to=to_number,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{SERVER_URL}/outbound",
                method="POST"
            )
            log.info("Started call to %s (attempt %d) SID=%s", to_number, attempt, call.sid)
            return call
        except Exception as e:
            last_exc = e
            log.warning("Attempt %d failed to start call to %s: %s", attempt, to_number, e)
            if attempt <= 2:
                time.sleep(1)
    log.exception("All attempts failed to call %s. Last error: %s", to_number, last_exc)
    return None

def run_campaign(csv_path="contacts.csv", limit=None):
    if not os.path.exists(csv_path):
        log.error("contacts.csv not found at %s", csv_path)
        raise SystemExit("contacts.csv not found")

    log.info("Starting campaign. OUTBOUND_URL=%s", f"{SERVER_URL}/outbound")
    with open(csv_path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log.error("contacts.csv has no headers")
            raise SystemExit("contacts.csv missing headers")
        count = 0
        for row in reader:
            if limit and count >= limit:
                break
            rown = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            name = rown.get("name", "unknown")
            phone = rown.get("phone") or rown.get("mobile") or rown.get("number")
            if not phone:
                log.warning("Skipping %s due to missing phone: %s", name, row)
                continue
            log.info("Dialing %s (%s)", name, phone)
            call = safe_initiate_call(phone, name)
            if call:
                count += 1
            time.sleep(1.0)
        log.info("Campaign finished — attempted calls: %d", count)
    return count

@app.route("/run_campaign", methods=["POST"])
def run_campaign_endpoint():
    token = os.environ.get("CAMPAIGN_TRIGGER_TOKEN")
    req_token = request.headers.get("X-Run-Token") or request.form.get("token")
    if token and req_token != token:
        log.warning("Unauthorized attempt to trigger campaign")
        return jsonify({"error": "unauthorized"}), 403
    limit = request.args.get("limit")
    limit = int(limit) if limit and limit.isdigit() else None
    try:
        count = run_campaign(limit=limit)
        return jsonify({"status": "started", "attempted": count}), 200
    except Exception as e:
        log.exception("Failed to start campaign")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    mode = os.environ.get("MODE", "server").lower()
    if mode == "campaign":
        run_campaign()
    else:
        port = int(os.environ.get("PORT", 5000))
        log.info("Starting Flask server on port %s", port)
        app.run(host="0.0.0.0", port=port)