# File: outbound_test_verbose.py
import requests
import json
import time

# ⚠️ Replace this with your actual Render service URL
BASE_URL = "https://sara-ai-bot.onrender.com"

ENDPOINT = f"{BASE_URL}/outbound"

payload = {
    "name": "The Artistic Monarchs",
    "phone": "+13092045365"  # Replace with a real test number
}

print("=== Verbose Outbound Test ===")
print(f"POST {ENDPOINT}")
print("Payload:", json.dumps(payload, indent=2))

start = time.time()
try:
    resp = requests.post(ENDPOINT, json=payload, timeout=30)
    elapsed = time.time() - start

    print(f"\nHTTP {resp.status_code} ({elapsed:.1f}s)\n")
    print("Response headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")

    body = resp.text
    print("\nResponse body (first 10000 chars):")
    print(body[:10000])

    try:
        parsed = resp.json()
        print("\n\n--- Parsed JSON ---")
        print(json.dumps(parsed, indent=2))
    except Exception:
        print("\n(Response was not valid JSON)")
except Exception as e:
    print("Request failed:", e)
