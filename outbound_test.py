# outbound_test.py
# Sends a single test call to the first contact in contacts.csv

import csv
import json
import requests
import sys
import time
from dotenv import load_dotenv
import os

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL") or "https://sara-ai-bot.onrender.com"
CSV_FILE = "contacts.csv"
HEADERS = {"Content-Type": "application/json"}

def load_first_contact(csv_path):
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            first = next(reader, None)
            if not first:
                print("ERROR: contacts.csv is empty or has no rows.")
                sys.exit(1)
            return first
    except FileNotFoundError:
        print(f"ERROR: {csv_path} not found in the current directory.")
        sys.exit(1)
    except Exception as e:
        print("ERROR reading CSV:", str(e))
        sys.exit(1)

def main():
    first = load_first_contact(CSV_FILE)
    name = first.get("name")
    phone = first.get("phone")

    if not name or not phone:
        print("ERROR: First contact missing name or phone")
        sys.exit(1)

    payload = {"name": name, "phone": phone}

    print(f"Sending test call to {name} ({phone})...")
    try:
        resp = requests.post(f"{SERVER_URL}/outbound", headers=HEADERS, json=payload, timeout=60)
        print("HTTP", resp.status_code)
        try:
            print(json.dumps(resp.json(), indent=2))
        except Exception:
            print(resp.text)
    except Exception as e:
        print("Request failed:", str(e))

if __name__ == "__main__":
    main()
