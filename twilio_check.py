# twilio_check.py
import os
from twilio.rest import Client

TW_SID = os.getenv("TWILIO_ACCOUNT_SID")
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

if not TW_SID or not TW_TOKEN:
    print("ERROR: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN not set in environment.")
    print("TWILIO_ACCOUNT_SID:", bool(TW_SID), "TWILIO_AUTH_TOKEN:", bool(TW_TOKEN))
    raise SystemExit(1)

client = Client(TW_SID, TW_TOKEN)
acct = client.api.accounts(TW_SID).fetch()
print("Twilio account:", acct.friendly_name)
