import requests

url = "http://127.0.0.1:5000/outbound"
payload = {"name": "John", "phone": "+15551234567"}

resp = requests.post(url, json=payload)
print("Status:", resp.status_code)
print("Response:", resp.json())
