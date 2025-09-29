# File: sara_precheck.py
import os
import csv
import logging
import asyncio
import requests
import websockets
from time import sleep
from twilio.rest import Client
from dotenv import load_dotenv

# --- Load .env locally ---
load_dotenv()

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("sara_precheck.log"), logging.StreamHandler()]
)
log = logging.getLogger("sara-precheck")

# --- Required environment variables ---
REQUIRED_ENVS = [
    "SERVER_URL",
    "PUBLIC_STREAMING_URL",
    "TWILIO_PHONE_NUMBER",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN"
]

missing = [v for v in REQUIRED_ENVS if not os.environ.get(v)]
if missing:
    log.error("Missing required environment variables: %s", missing)
    raise SystemExit(f"Missing env vars: {missing}")

# --- Load environment ---
FLASK_URL = os.environ["SERVER_URL"].rstrip("/")
STREAM_URL = os.environ["PUBLIC_STREAMING_URL"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

RETRY_ATTEMPTS = int(os.environ.get("RETRY_ATTEMPTS", 2))
RETRY_BACKOFF = int(os.environ.get("RETRY_BACKOFF_SECONDS", 1))

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
CSV_PATH = "contacts.csv"

# --- 1️⃣ Check Flask health ---
def check_flask_health():
    try:
        resp = requests.get(f"{FLASK_URL}/health", timeout=5)
        if resp.status_code == 200:
            log.info("Flask server healthy ✅")
            return True
        else:
            log.error("Flask health check failed: %s", resp.status_code)
            return False
    except Exception:
        log.exception("Flask health check exception")
        return False

# --- 2️⃣ Check WebSocket streaming server ---
async def check_streaming_server():
    try:
        # connect with a short timeout
        async with websockets.connect(STREAM_URL, ping_interval=10, close_timeout=5) as ws:
            log.info("Streaming server connected ✅")
            return True
    except Exception:
        log.exception("Streaming server connection failed")
        return False

# --- 3️⃣ Safe Twilio call with retries ---
def safe_call(to, name):
    for attempt in range(1, RETRY_ATTEMPTS + 2):
        try:
            call = client.calls.create(
                to=to,
                from_=TWILIO_PHONE_NUMBER,
                url=f"{FLASK_URL}/outbound",
                method="POST"
            )
            return call
        except Exception as e:
            log.warning("Attempt %d failed for %s: %s", attempt, name, e)
            if attempt <= RETRY_ATTEMPTS:
                sleep(RETRY_BACKOFF)
    log.error("All retry attempts failed for %s", name)
    return None

# --- 4️⃣ Dry-run / full campaign ---
def run_campaign():
    if not os.path.exists(CSV_PATH):
        log.error("contacts.csv not found, aborting campaign")
        return

    with open(CSV_PATH, newline='', encoding='utf-8') as f:
        reader = list(csv.DictReader(f))
        count = len(reader)

        if count == 0:
            log.warning("contacts.csv is empty")
            return

        dry_run_mode = count == 1
        mode_text = "Dry-run mode" if dry_run_mode else "Full campaign mode"
        log.info("Starting campaign: %s, total contacts: %d", mode_text, count)

        for idx, row in enumerate(reader, start=1):
            name = row.get("name", "unknown")
            phone = row.get("phone") or row.get("mobile") or row.get("number")
            if not phone:
                log.warning("Skipping row with empty phone: %s", row)
                continue

            call = safe_call(phone, name)
            if call:
                log.info("%s call %d/%d: %s (%s) SID=%s", mode_text, idx, count, name, phone, call.sid)
            if dry_run_mode:
                log.info("Dry-run finished ✅")
                break

# --- 5️⃣ Run all checks ---
async def main():
    log.info("Starting Sara pre-check...")

    flask_ok = check_flask_health()
    try:
        stream_ok = await asyncio.wait_for(check_streaming_server(), timeout=8)
    except asyncio.TimeoutError:
        log.error("Streaming server check timed out ❌")
        stream_ok = False

    if flask_ok and stream_ok:
        log.info("All servers live ✅")
        run_campaign()
    else:
        log.error("Pre-check failed ❌")
        if not flask_ok:
            log.error("Flask server down")
        if not stream_ok:
            log.error("Streaming server down")

if __name__ == "__main__":
    asyncio.run(main())
