import csv
import time
import requests
import os
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")
CONTACTS_FILE = "contacts.csv"
LOG_FILE = "contacts_called.csv"
DELAY_BETWEEN_CALLS = 2  # seconds

# ---------------------------
# Ensure log file exists
# ---------------------------
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "phone", "audio_url", "message_text", "status"])
        writer.writeheader()

# ---------------------------
# Read contacts
# ---------------------------
contacts = []
with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        contacts.append(row)

# ---------------------------
# Run calls
# ---------------------------
with open(LOG_FILE, "a", newline="", encoding="utf-8") as log_f:
    log_writer = csv.DictWriter(log_f, fieldnames=["name", "phone", "audio_url", "message_text", "status"])
    
    for contact in contacts:
        name = contact["name"]
        phone = contact["phone"]
        
        payload = {"name": name, "phone": phone}
        try:
            response = requests.post(f"{SERVER_URL}/outbound", json=payload, timeout=60)
            if response.status_code == 200:
                data = response.json()
                log_writer.writerow({
                    "name": name,
                    "phone": phone,
                    "audio_url": data.get("audio_url"),
                    "message_text": data.get("message_text"),
                    "status": "success"
                })
                print(f"✅ Called {name} ({phone}) successfully.")
            else:
                log_writer.writerow({
                    "name": name,
                    "phone": phone,
                    "audio_url": "",
                    "message_text": "",
                    "status": f"HTTP {response.status_code}: {response.text}"
                })
                print(f"[❌] Failed for {name} ({phone}): HTTP {response.status_code}")
        except Exception as e:
            log_writer.writerow({
                "name": name,
                "phone": phone,
                "audio_url": "",
                "message_text": "",
                "status": f"Exception: {str(e)}"
            })
            print(f"[❌] Failed for {name} ({phone}): {str(e)}")
        
        time.sleep(DELAY_BETWEEN_CALLS)

print("✅ Runner finished all contacts.")
