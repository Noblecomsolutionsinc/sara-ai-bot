import os
import csv
import requests
from dotenv import load_dotenv

# Load .env
load_dotenv()

SERVER_URL = os.getenv("SERVER_URL")
CONTACTS_FILE = "contacts.csv"

def call_sara(name, phone):
    url = f"{SERVER_URL}/outbound"
    data = {"name": name, "phone": phone}
    try:
        response = requests.post(url, json=data, timeout=30)
        if response.status_code == 200:
            result = response.json()
            print(f"[✅] Called {name} ({phone})")
            print(f"Audio URL: {result.get('audio_url')}")
            print(f"Text: {result.get('message_text')}\n")
        else:
            print(f"[❌] Failed for {name} ({phone}): {response.status_code} {response.text}")
    except Exception as e:
        print(f"[❌] Failed for {name} ({phone}): {str(e)}")

def main():
    with open(CONTACTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name")
            phone = row.get("phone")
            call_sara(name, phone)

if __name__ == "__main__":
    main()
