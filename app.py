# File: app.py - FIXED ORDER
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

    # ✅ FIXED: Say FIRST, then Start the stream
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Hi, Connecting you with Sara Hayes. Please hold. Your call is important to us.</Say>
  <Start>
    <Stream url="{PUBLIC_STREAMING_URL}"/>
  </Start>
</Response>
"""
    return Response(twiml, mimetype="text/xml")

# ... rest of your app.py code remains the same ...