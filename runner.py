import os
import csv
import time
import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")
CONTACTS_FILE = "contacts.csv"
LOG_FILE = "contacts_called.csv"
DELAY_BETWEEN_CALLS = 5  # seconds

# ---------------------------
# Prepare log file
# ---------------------------
log_exists = os.path.exists(LOG_FILE)
log_f = open(LOG_FILE, "a", newline="", encoding="utf-8")
log_writer = csv.DictWriter(log_f, fieldnames=["name", "phone", "audio_url", "message_text", "status"])
if not log_exists:
    log_writer.writeheader()

# ---------------------------
# Load contacts
# ---------------------------
with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    contacts = [row for row in reader]

# ---------------------------
# Call each contact
# ---------------------------
for contact in contacts:
    name = contact.get("name")
    phone = contact.get("phone")

    if not name or not phone:
        print(f"[SKIP] Invalid contact: {contact}")
        continue

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
            print(f"[✅] Call triggered for {name} ({phone})")
        else:
            try:
                data = response.json()
                error_msg = data.get("error", response.text)
            except Exception:
                error_msg = response.text
            log_writer.writerow({
                "name": name,
                "phone": phone,
                "audio_url": "",
                "message_text": "",
                "status": f"failed: {error_msg}"
            })
            print(f"[❌] Failed for {name} ({phone}): {error_msg}")
    except requests.exceptions.Timeout:
        log_writer.writerow({
            "name": name,
            "phone": phone,
            "audio_url": "",
            "message_text": "",
            "status": "error: timeout"
        })
        print(f"[❌] Timeout error for {name} ({phone})")
    except requests.exceptions.RequestException as e:
        log_writer.writerow({
            "name": name,
            "phone": phone,
            "audio_url": "",
            "message_text": "",
            "status": f"error: {str(e)}"
        })
        print(f"[❌] Request exception for {name} ({phone}): {e}")
    except Exception as e:
        log_writer.writerow({
            "name": name,
            "phone": phone,
            "audio_url": "",
            "message_text": "",
            "status": f"error: {str(e)}"
        })
        print(f"[❌] Unexpected error for {name} ({phone}): {e}")

    log_f.flush()
    time.sleep(DELAY_BETWEEN_CALLS)

log_f.close()
print("✅ Runner finished all contacts.")
