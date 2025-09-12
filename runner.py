import csv
import os
import requests

# Load environment
from dotenv import load_dotenv
load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")
if not SERVER_URL:
    raise Exception("SERVER_URL missing in .env")

CONTACTS_FILE = "contacts.csv"
CALLED_FILE = "contacts_called.csv"

# -----------------------------
# Helper to call Sara API
# -----------------------------
def call_sara(phone_number):
    url = f"{SERVER_URL}/outbound"
    data = {"phone": phone_number}
    try:
        r = requests.post(url, data=data, timeout=10)
        r.raise_for_status()
        print(f"Called {phone_number} - Status: {r.status_code}")
        return True
    except Exception as e:
        print(f"Failed to call {phone_number}: {e}")
        return False

# -----------------------------
# Main loop
# -----------------------------
def main():
    # Read contacts
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
            print("No phone number found in row, skipping...")
            continue
        success = call_sara(phone)
        if success:
            called_contacts.append(contact)

    # Save called contacts
    if called_contacts:
        fieldnames = called_contacts[0].keys()
        with open(CALLED_FILE, "w", newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(called_contacts)
        print(f"Successfully called {len(called_contacts)} contacts. Saved to {CALLED_FILE}")

if __name__ == "__main__":
    main()
