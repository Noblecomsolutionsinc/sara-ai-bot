import os
import logging
from openai import OpenAI

# Setup logging
logger = logging.getLogger("gpt_client")
logger.setLevel(logging.INFO)

# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", 60))
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", 1000))

client = None

try:
    client = OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=OPENAI_TIMEOUT,
    )
    logger.info(
        f"GPT client configured: model={OPENAI_MODEL} "
        f"max_completion_tokens={OPENAI_MAX_TOKENS} "
        f"timeout={OPENAI_TIMEOUT}"
    )
except Exception as e:
    logger.error("Failed to initialize OpenAI client.", exc_info=e)


def generate_completion(messages):
    """
    Generate a completion from OpenAI using chat models.
    """
    if client is None:
        logger.error("OpenAI client not initialized. Cannot generate completion.")
        return None

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_completion_tokens=OPENAI_MAX_TOKENS,
        )
        return response
    except Exception as e:
        logger.error("Error generating completion from OpenAI.", exc_info=e)
        return None
