import csv
import os
import requests
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")
if not SERVER_URL:
    raise Exception("SERVER_URL missing in .env")

CONTACTS_FILE = "contacts.csv"
CALLED_FILE = "contacts_called.csv"

def call_sara(phone_number):
    url = f"{SERVER_URL}/outbound"
    data = {"phone": phone_number}
    try:
        r = requests.post(url, data=data, timeout=15)  # increased timeout
        r.raise_for_status()
        print(f"{datetime.now()} - Called {phone_number} - Status: {r.status_code}")
        return True
    except Exception as e:
        print(f"{datetime.now()} - Failed to call {phone_number}: {e}")
        return False

def main():
    if not os.path.exists(CONTACTS_FILE):
        print(f"{CONTACTS_FILE} not found!")
        return

    with open(CONTACTS_FILE, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        contacts = [row for row in reader]

    called_contacts = []
    for contact in contacts:
        phone = contact.get("phone") or contact.get("Phone") or contact.get("PhoneNumber")
        if not phone:
            print("No phone number in row, skipping...")
            continue
        if call_sara(phone):
            called_contacts.append(contact)

    if called_contacts:
        fieldnames = called_contacts[0].keys()
        with open(CALLED_FILE, "w", newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(called_contacts)
        print(f"Called {len(called_contacts)} contacts. Saved to {CALLED_FILE}")

if __name__ == "__main__":
    main()
