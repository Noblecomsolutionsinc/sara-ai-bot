# File: runner.py
# Path: ./runner.py
"""
Sequential runner for outbound calls.
Pace control and optional early stop on error.
"""

import os
import csv
import time
import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
CONTACTS_FILE = os.getenv("CONTACTS_FILE", "contacts.csv")
PACE_SECONDS = float(os.getenv("CALL_PACE_SECONDS", "8"))  # seconds between calls
STOP_ON_ERROR = os.getenv("STOP_ON_ERROR", "true").strip().lower() in ("1", "true", "yes")
TIMEOUT = int(os.getenv("OUTBOUND_REQUEST_TIMEOUT", "180"))

def call_sara(name, phone):
    url = f"{SERVER_URL}/outbound"
    data = {"name": name, "phone": phone}
    try:
        response = requests.post(url, json=data, timeout=TIMEOUT)
        if response.status_code == 200:
            result = response.json()
            print(f"[✅] Called {name} ({phone})")
            print(f"  Audio URL: {result.get('audio_url')}")
            print(f"  Call SID: {result.get('call_sid')}\n")
            return True
        else:
            print(f"[❌] Failed for {name} ({phone}): {response.status_code} {response.text}")
            return False
    except Exception as e:
        print(f"[❌] Failed for {name} ({phone}): {str(e)}")
        return False

def main():
    if not os.path.exists(CONTACTS_FILE):
        print(f"Contacts file not found: {CONTACTS_FILE}")
        return
    with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name")
            phone = row.get("phone")
            ok = call_sara(name, phone)
            if not ok and STOP_ON_ERROR:
                print("Stopping runner due to error (STOP_ON_ERROR=true).")
                break
            time.sleep(PACE_SECONDS)

if __name__ == "__main__":
    main()
