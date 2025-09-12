# call_handler.py (minimal, safe: read first contact and return a string)
import os
import pandas as pd

def call_next_contact():
    csv_path = "contacts.csv"
    if not os.path.exists(csv_path):
        return f"contacts.csv not found at {os.path.abspath(csv_path)}"

    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        return f"Error reading contacts.csv: {e}"

    if df.empty:
        return "contacts.csv is empty"

    # read first row but DO NOT modify CSV (we'll keep that behavior)
    row = df.iloc[0]
    name = row.get("name", "unknown")
    phone = row.get("phone", "unknown")
    business_type = row.get("type", "")

    # return an informative message so /outbound returns 200 and helpful text
    return f"Would call: {name} ({phone}) - type: {business_type}"
