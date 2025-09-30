# make_call.py
import os
from dotenv import load_dotenv
from twilio.rest import Client

# Load .env
load_dotenv()

# Get credentials
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_number = os.getenv("TWILIO_PHONE_NUMBER")

# CHANGE this to your real destination phone number
to_number = "+13092045365"  

# TwiML App URL (your Flask outbound endpoint)
twiml_url = "https://sara-ai-bot.onrender.com/outbound"

# Initialize client
client = Client(account_sid, auth_token)

try:
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
    )
    print("✅ Call initiated! SID:", call.sid)
except Exception as e:
    print("❌ Call failed:", str(e))
