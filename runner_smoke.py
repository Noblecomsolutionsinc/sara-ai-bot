# File: runner_smoke.py
# Path: ./runner_smoke.py
"""
Smoke runner: calls first N contacts sequentially (default 5).
Stops if any call fails.
"""

import os
import csv
import time
import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "https://sara-ai-bot.onrender.com").rstrip("/")
CONTACTS_FILE = os.getenv("CONTACTS_FILE", "contacts.csv")
SMOKE_CALLS = int(os.getenv("SMOKE_CALLS", "5"))
PACE_SECONDS = float(os.getenv("SMOKE_PACE_SECONDS", "8"))
TIMEOUT = int(os.getenv("OUTBOUND_REQUEST_TIMEOUT", "180"))

def call_one(name, phone):
    url = f"{SERVER_URL}/outbound"
    data = {"name": name, "phone": phone}
    try:
        r = requests.post(url, json=data, timeout=TIMEOUT)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)

def main():
    if not os.path.exists(CONTACTS_FILE):
        print(f"Contacts file not found: {CONTACTS_FILE}")
        return
    with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            if count >= SMOKE_CALLS:
                break
            name = row.get("name")
            phone = row.get("phone")
            print(f"Smoke call #{count+1}: {name} -> {phone}")
            status, body = call_one(name, phone)
            print(f"  HTTP: {status}")
            print(f"  Body (truncated): {body[:2000]}\n")
            if status != 200:
                print("Smoke runner stopping due to non-200 response.")
                return
            count += 1
            time.sleep(PACE_SECONDS)
    print("Smoke runner completed successfully.")

if __name__ == "__main__":
    main()
