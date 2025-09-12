# call_handler.py
import os
import csv
import urllib.parse
from twilio.rest import Client

def call_next_contact():
    """
    Reads the first contact from contacts.csv and triggers a Twilio outbound call.
    Returns a string describing the result.
    """
    csv_path = "contacts.csv"
    if not os.path.exists(csv_path):
        return f"contacts.csv not found at {os.path.abspath(csv_path)}"

    # Read first row
    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            first = None
            for row in reader:
                first = row
                break
    except Exception as e:
        return f"Error reading contacts.csv: {e}"

    if not first:
        return "contacts.csv is empty"

    name = (first.get("name") or "").strip()
    phone = (first.get("phone") or "").strip()
    business_type = (first.get("type") or "").strip()

    # Validate phone format quickly
    if not phone or not phone.startswith("+"):
        return f"Phone number for {name} looks invalid: '{phone}' (must include +countrycode)"

    # Load Twilio & server config from env
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
    SERVER_URL = os.environ.get("SERVER_URL")

    missing = []
    for k, v in [
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
        ("SERVER_URL", SERVER_URL),
    ]:
        if not v:
            missing.append(k)
    if missing:
        return f"Missing env vars: {', '.join(missing)}"

    # Build TwiML URL (URL-encode parameters)
    query = f"name={urllib.parse.quote(name)}&type={urllib.parse.quote(business_type)}"
    twiml_url = f"{SERVER_URL.rstrip('/')}/voice?{query}"

    # Create Twilio client and place the call
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url
        )
        return f"Initiated call {call.sid} to {name} ({phone}) — TwiML: {twiml_url}"
    except Exception as e:
        # Twilio raises exceptions with useful messages; return for logs
        return f"Twilio error: {e}"
