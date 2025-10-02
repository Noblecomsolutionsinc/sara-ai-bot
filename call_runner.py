import csv
import os
import time
from datetime import datetime
from urllib.parse import urlencode
from twilio.rest import Client

# Twilio credentials
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
client = Client(account_sid, auth_token)

# Config
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")   # e.g. https://sara-ai-bot.onrender.com
VOICE_ENDPOINT = "/voice"
DELAY_BETWEEN_CALLS = 3  # seconds

def run_campaign(csv_file="contacts.csv", log_file="calls_log.csv"):
    with open(csv_file, newline="") as infile, open(log_file, "a", newline="") as logfile:
        reader = csv.DictReader(infile)
        logwriter = csv.writer(logfile)

        # Write header if new file
        if logfile.tell() == 0:
            logwriter.writerow(["timestamp", "name", "phone", "industry", "sid", "status"])

        for row in reader:
            name = row["name"]
            phone = row["phone"]
            industry = row.get("industry", "")

            # Encode personalization into webhook URL
            params = urlencode({"name": name, "industry": industry})
            webhook_url = f"{SERVER_URL}{VOICE_ENDPOINT}?{params}"

            try:
                print(f"📞 Calling {name} at {phone} ({industry})...")
                call = client.calls.create(
                    to=phone,
                    from_=TWILIO_NUMBER,
                    url=webhook_url
                )
                logwriter.writerow([
                    datetime.utcnow().isoformat(),
                    name,
                    phone,
                    industry,
                    call.sid,
                    call.status
                ])
                print(f"✅ Call started: SID {call.sid}")
            except Exception as e:
                print(f"❌ Failed to call {phone}: {e}")
                logwriter.writerow([
                    datetime.utcnow().isoformat(),
                    name,
                    phone,
                    industry,
                    "ERROR",
                    str(e)
                ])

            time.sleep(DELAY_BETWEEN_CALLS)

if __name__ == "__main__":
    run_campaign()
