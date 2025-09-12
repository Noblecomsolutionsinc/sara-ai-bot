# call_handler.py (pandas removed; uses builtin csv for server)
import os
import csv

def call_next_contact():
    csv_path = "contacts.csv"
    if not os.path.exists(csv_path):
        return f"contacts.csv not found at {os.path.abspath(csv_path)}"

    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            first = None
            for row in reader:
                first = row
                break

    except Exception as e:
        return f"Error reading contacts.csv: {e}"

    if not first:
        return "contacts.csv is empty"

    name = first.get("name", "unknown")
    phone = first.get("phone", "unknown")
    business_type = first.get("type", "")

    return f"Would call: {name} ({phone}) - type: {business_type}"
