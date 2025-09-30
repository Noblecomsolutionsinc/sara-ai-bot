# check_twilio_env.py
import os
from dotenv import load_dotenv

# Force load .env from the current project root
dotenv_path = os.path.join(os.getcwd(), ".env")
print(f"Looking for .env at: {dotenv_path}")
load_dotenv(dotenv_path)

# Read vars
sid = os.getenv("TWILIO_ACCOUNT_SID")
token = os.getenv("TWILIO_AUTH_TOKEN")
phone = os.getenv("TWILIO_PHONE_NUMBER")

# Show results (mask sensitive parts)
def mask(val, keep=4):
    if not val:
        return "MISSING"
    return val[:keep] + "..." + val[-keep:]

print("SID:", mask(sid, keep=6))
print("Token:", mask(token, keep=4))
print("Phone:", phone if phone else "MISSING")
