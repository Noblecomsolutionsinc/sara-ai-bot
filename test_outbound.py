import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Simulate what Twilio sends to your /outbound endpoint
response = requests.post(
    f"{os.environ['SERVER_URL']}/outbound",
    data={
        "CallSid": "CA1234567890",
        "AccountSid": os.environ["TWILIO_ACCOUNT_SID"],
        "To": "+13092045365", 
        "From": os.environ["TWILIO_PHONE_NUMBER"],
        "CallStatus": "ringing"
    }
)

print("Status:", response.status_code)
print("TwiML Response:")
print(response.text)