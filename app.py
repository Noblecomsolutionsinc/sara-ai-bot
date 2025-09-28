import os
import csv
import logging
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Start, Stream
from twilio.rest import Client
from dotenv import load_dotenv

# Load .env for local dev (Render ignores this)
load_dotenv()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sara-app")

# --- Env Vars ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", 60))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", 1000))

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

SERVER_URL = os.getenv("SERVER_URL", "https://sara-ai-bot.onrender.com")
PUBLIC_STREAMING_URL = f"{SERVER_URL.replace('https', 'wss').replace('http', 'wss')}/ws"

SARA_NAME = os.getenv("SARA_NAME", "Sara Hayes")
SARA_ROLE = os.getenv("SARA_ROLE", "Senior Growth Consultant at Noblecom Solutions")
COMPANY_NAME = os.getenv("COMPANY_NAME", "Noblecom Solutions")
MEETING_LINK = os.getenv("MEETING_LINK", "https://calendly.com/adm-fintech1/30min")

SIMULATE = os.getenv("SIMULATE", "false").lower() == "true"
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", 24))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", 2))
RETRY_BACKOFF_SECONDS = int(os.getenv("RETRY_BACKOFF_SECONDS", 1))

# --- Twilio client ---
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- Flask app ---
app = Flask(__name__)

# --- Outbound Dialer ---
def dial_number(phone_number: str):
    """Dial a number via Twilio and connect it to Sara’s streaming pipeline."""
    logger.info(f"Dialing {phone_number} from {TWILIO_PHONE_NUMBER}")
    try:
        call = twilio_client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{SERVER_URL}/outbound_twiml"
        )
        logger.info(f"Call initiated: SID {call.sid}")
        return call.sid
    except Exception as e:
        logger.error(f"Error dialing {phone_number}: {e}")
        return None

# --- Outbound TwiML ---
@app.route("/outbound_twiml", methods=["POST", "GET"])
def outbound_twiml():
    """Return TwiML for outbound calls, including the streaming connection."""
    logger.info("Serving outbound TwiML")

    response = VoiceResponse()
    start = Start()
    start.stream(url=PUBLIC_STREAMING_URL)
    response.append(start)

    response.say(f"Hello, this is {SARA_NAME} from {COMPANY_NAME}.")
    return Response(str(response), mimetype="text/xml")

# --- Campaign Runner ---
@app.route("/run_campaign", methods=["POST"])
def run_campaign():
    """Run outbound campaign from contacts.csv (one number at a time for now)."""
    try:
        with open("contacts.csv", "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                phone = row.get("phone")
                if phone:
                    dial_number(phone)
                    break  # one number at a time
        return {"status": "campaign started"}, 200
    except Exception as e:
        logger.error(f"Error running campaign: {e}")
        return {"error": str(e)}, 500

# --- Health Check ---
@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

# --- Entrypoint ---
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
