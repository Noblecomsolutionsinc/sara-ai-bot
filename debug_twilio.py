import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Test the actual TwiML that Twilio receives
response = requests.post(
    f"{os.environ['SERVER_URL']}/outbound",
    data={"CallSid": "test123", "To": "+1234567890", "From": "+0987654321"}
)

print("=== TwiML Response ===")
print(response.text)
print("======================")

# Check if WebSocket URL is correct
if "wss://sara-ai-streaming.onrender.com/ws" in response.text:
    print("✅ WebSocket URL found in TwiML")
else:
    print("❌ WebSocket URL NOT found in TwiML")