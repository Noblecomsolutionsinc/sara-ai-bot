# E:\Sara-AI-Bot\env_check.py
import os
import logging
from dotenv import load_dotenv

# Load .env into environment
load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
]

def validate_env_vars():
    all_good = True
    for var in REQUIRED_ENV_VARS:
        val = os.getenv(var)
        if not val:
            logging.error(f"{var} is MISSING.")
            all_good = False
        else:
            logging.debug(f"{var} loaded: {val[:5]}...{len(val)} chars")
    return all_good

if __name__ == "__main__":
    if validate_env_vars():
        logging.info("✅ All required environment variables loaded correctly.")
    else:
        logging.error("❌ One or more environment variables are missing. Check your .env file.")
