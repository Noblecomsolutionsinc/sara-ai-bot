import os
import logging
import asyncio
from openai import OpenAI
from dotenv import load_dotenv

# --------------------------------------------------
# Load environment
# --------------------------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("❌ OPENAI_API_KEY is not set in environment")

# --------------------------------------------------
# Logging
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gpt_client")

# --------------------------------------------------
# Config
# --------------------------------------------------
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", 200))
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))

# Retry / Timeout
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", 2))
RETRY_BACKOFF_SECONDS = int(os.getenv("RETRY_BACKOFF_SECONDS", 1))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", 30))

# --------------------------------------------------
# Init OpenAI client (no proxies arg!)
# --------------------------------------------------
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("✅ OpenAI client initialized successfully")
except Exception as e:
    logger.exception("❌ Failed to init OpenAI client")
    raise


# --------------------------------------------------
# Generate GPT reply with retry + timeout
# --------------------------------------------------
async def generate_reply(user_input: str) -> str:
    last_exception = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            logger.info(f"🔄 GPT call attempt {attempt}/{RETRY_ATTEMPTS}")

            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.chat.completions.create,
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": "You are Sara, a professional outbound AI cold caller. Be concise, clear, and persuasive."},
                        {"role": "user", "content": user_input},
                    ],
                    max_tokens=MAX_TOKENS,
                    temperature=TEMPERATURE,
                ),
                timeout=OPENAI_TIMEOUT
            )

            reply = response.choices[0].message.content.strip()
            return reply

        except asyncio.TimeoutError:
            logger.error(f"⏱️ OpenAI call timed out (attempt {attempt})")
            last_exception = "OpenAI API timeout"
        except Exception as e:
            logger.error(f"❌ GPT call failed (attempt {attempt}): {e}")
            last_exception = str(e)

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)

    return f"[System Error: {last_exception}]"
