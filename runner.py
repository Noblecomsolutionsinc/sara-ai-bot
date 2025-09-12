import os
import csv
import re
import sys
import requests
from dotenv import load_dotenv

# -----------------------------
# Load environment
# -----------------------------
load_dotenv()
SERVER_URL = os.environ.get("SERVER_URL")

if not SERVER_URL:
    print("ERROR: SERVER_URL not set in .env")
    sys.exit(1)

CSV_IN = "contacts.csv"
CSV_OUT = "contacts_called.csv"

# -----------------------------
# Phone sanitizer
# -----------------------------
def sanitize_phone(raw):
    if raw is None:
        return ""
    s = str(raw).strip()
    s = s.strip("`'\" \u200b")
    s = re.sub(r"[^\d+]", "", s)
    if s.count("+") > 1:
        s = s.replace("+", "")
    digits = re.sub(r"[^\d]", "", s)
    if s.startswith("+") and digits:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) >= 11:
        return f"+{digits}"
    return ""

# -----------------------------
# Read contacts
# -----------------------------
if not os.path.exists(CSV_IN):
    print(f"{CSV_IN} not found in {os.getcwd()}")
    sys.exit(1)

contacts = []
with open(CSV_IN, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    headers = reader.fieldnames or ["name", "phone", "type"]
    for row in reader:
        norm_row = {k: ("" if row.get(k) is None else str(row.get(k))) for k in headers}
        contacts.append(norm_row)

if not contacts:
    print("No contacts found in CSV.")
    sys.exit(0)

# -----------------------------
# Prepare output CSV
# -----------------------------
called_file_exists = os.path.exists(CSV_OUT)
outf = open(CSV_OUT, 'a', newline='', encoding='utf-8')
writer = csv.DictWriter(outf, fieldnames=headers)
if not called_file_exists:
    writer.writeheader()

# -----------------------------
# Trigger outbound calls
# -----------------------------
total = len(contacts)
success = 0
fail = 0

for idx, row in enumerate(contacts, start=1):
    name = row.get('name', '').strip()
    raw_phone = row.get('phone', '')
    business_type = row.get('type', '').strip()
    phone_clean = sanitize_phone(raw_phone)

    print(f"\n[{idx}/{total}] Attempting: {name} | raw: '{raw_phone}' | sanitized: '{phone_clean}'")

    if not phone_clean:
        print("  -> Skipping: invalid phone.")
        fail += 1
        continue

    # Make POST request to /outbound
    try:
        r = requests.post(
            f"{SERVER_URL.rstrip('/')}/outbound",
            data={"phone": phone_clean},
            timeout=60
        )
        print("  HTTP status:", r.status_code)
        text = (r.text or "")[:500].replace('\n', ' ')
        print("  Server response (truncated):", text)

        if r.status_code == 200:
            writer.writerow(row)
            outf.flush()
            success += 1
            print("  -> Recorded in", CSV_OUT)
        else:
            fail += 1
            print("  -> Not recorded (non-200).")

    except requests.RequestException as e:
        fail += 1
        print("  -> Request exception:", str(e))

# -----------------------------
# Summary
# -----------------------------
outf.close()
print("\n=== SUMMARY ===")
print("Total contacts in CSV:", total)
print("Successfully recorded (200):", success)
print("Failed/skipped:", fail)
print(f"Called contacts appended to: {CSV_OUT}")
