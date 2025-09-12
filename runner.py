# runner.py (robust, csv-based; avoids pandas & handles int phone values)
import os
import csv
import re
import sys
import requests
from dotenv import load_dotenv

load_dotenv()
SERVER_URL = os.environ.get("SERVER_URL")

if not SERVER_URL:
    print("SERVER_URL not found. Set SERVER_URL in your .env or run: python runner.py https://your-server")
    sys.exit(1)

print("SERVER_URL =", SERVER_URL)

CSV_IN = "contacts.csv"
CSV_OUT = "contacts_called.csv"

if not os.path.exists(CSV_IN):
    print(f"{CSV_IN} not found in {os.getcwd()}")
    sys.exit(1)

def sanitize_phone(raw):
    """Return sanitized phone string (eg +12345678900) or empty string if not possible."""
    if raw is None:
        return ""
    s = str(raw).strip()
    # remove surrounding backticks, quotes, zero-width spaces
    s = s.strip("`'\" \u200b")
    # remove any character except digits and plus
    s = re.sub(r"[^\d+]", "", s)
    # if multiple plus signs, remove extras
    if s.count("+") > 1:
        s = s.replace("+", "")
    digits = re.sub(r"[^\d]", "", s)
    if s.startswith("+") and digits:
        return f"+{digits}"
    # US 10-digit -> +1
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) >= 11:
        return f"+{digits}"
    return ""  # couldn't sanitize

# read contacts (csv module keeps original strings; Excel-int issue avoided)
contacts = []
with open(CSV_IN, newline='', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    headers = reader.fieldnames or ["name", "phone", "type"]
    for row in reader:
        # Normalize all values to strings so .strip() works
        norm_row = {k: ("" if row.get(k) is None else str(row.get(k))) for k in headers}
        contacts.append(norm_row)

if not contacts:
    print("No contacts found in", CSV_IN)
    sys.exit(0)

# Prepare called log file and header
if os.path.exists(CSV_OUT):
    called_file_exists = True
else:
    called_file_exists = False

# Counters
total = len(contacts)
success = 0
fail = 0

# Open called csv for appending
with open(CSV_OUT, 'a', newline='', encoding='utf-8') as outf:
    writer = csv.DictWriter(outf, fieldnames=headers)
    if not called_file_exists:
        writer.writeheader()

    # Iterate and call
    for idx, row in enumerate(contacts, start=1):
        name = row.get('name', '').strip()
        raw_phone = row.get('phone', '')
        business_type = row.get('type', '').strip()

        phone_clean = sanitize_phone(raw_phone)

        print(f"\n[{idx}/{total}] Attempting: {name} | raw phone: '{raw_phone}' | sanitized: '{phone_clean}'")

        if not phone_clean:
            print("  -> Skipping: phone could not be sanitized. Fix CSV or run fix script.")
            fail += 1
            continue

        # Trigger outbound on the server (server reads its own contacts.csv; this is the current flow)
        try:
            r = requests.get(f"{SERVER_URL.rstrip('/')}/outbound", timeout=60)
            print("  HTTP status:", r.status_code)
            # print limited server response for debugging
            text = (r.text or "")[:500].replace('\n', ' ')
            print("  Server response (truncated):", text)

            if r.status_code == 200:
                # log the row as contacted
                writer.writerow(row)
                outf.flush()
                success += 1
                print("  -> Recorded to", CSV_OUT)
            else:
                fail += 1
                print("  -> Not recorded (non-200).")
        except requests.RequestException as e:
            fail += 1
            print("  -> Request exception:", str(e))

# Summary
print("\n=== SUMMARY ===")
print("Total contacts in CSV:", total)
print("Successfully recorded (200):", success)
print("Failed/skipped:", fail)
print(f"Called contacts appended to: {CSV_OUT}")
