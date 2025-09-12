import os
import csv
import time
import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")  # e.g., https://sara-ai-bot.onrender.com
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
# Call each contact via Render server
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
