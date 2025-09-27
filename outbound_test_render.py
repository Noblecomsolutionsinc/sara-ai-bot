# File: outbound_test_render.py
import requests
import json
import time

# 🔹 Replace with your Render URL
BASE_URL = "https://sara-ai-bot.onrender.com"

def main():
    url = f"{BASE_URL}/outbound"
    payload = {
        "name": "Render Prospect",
        "phone": "+13092045365"  # use your real test number
    }
    headers = {"Content-Type": "application/json"}

    print(">>")
    print("=== Verbose Outbound Test (Render) ===")
    print(f"POST {url}")
    print("Payload:", json.dumps(payload, indent=2))

    start = time.time()
    resp = requests.post(url, headers=headers, json=payload)
    duration = time.time() - start

    print(f"\nHTTP {resp.status_code} ({duration:.1f}s)\n")
    print("Response headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")

    print("\nResponse body (first 10000 chars):")
    print(resp.text[:10000])

    try:
        parsed = resp.json()
        print("\n\n--- Parsed JSON ---")
        print(json.dumps(parsed, indent=2))
    except Exception:
        print("\n\n(No JSON body)")

if __name__ == "__main__":
    main()
