# test_gpt_local.py
from gpt_client import call_gpt
import os

def main():
    # simple sanity message
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello in one short sentence."}
    ]
    resp = call_gpt(messages, max_tokens=30)
    print("RAW RESPONSE:", resp)
    if resp and "choices" in resp and len(resp["choices"])>0:
        print("TEXT:", resp["choices"][0].get("message", {}).get("content"))

if __name__ == "__main__":
    main()
