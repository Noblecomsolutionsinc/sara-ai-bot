import os
import logging
from openai import OpenAI, OpenAIError  # Correct import for the latest SDK

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("gpt_client")

# Load API key from environment
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY environment variable not set")
    raise ValueError("OPENAI_API_KEY is required")

# Initialize OpenAI client
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
    logger.info("✅ OpenAI client initialized successfully")
except OpenAIError as e:
    logger.error(f"❌ Failed to initialize OpenAI client: {e}")
    raise

def generate_reply(prompt: str, model: str = "gpt-4.1-mini", max_tokens: int = 500) -> str:
    """
    Generate a text reply using OpenAI GPT.
    """
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens
        )
        return response.choices[0].message.content.strip()
    except OpenAIError as e:
        logger.error(f"❌ Error generating reply: {e}")
        return "Error generating reply."

def stream_reply(prompt: str, model: str = "gpt-4o-mini") -> str:
    """
    Stream a reply from GPT for real-time applications.
    """
    try:
        streamed_text = ""
        with client.chat.completions.stream(
            model=model,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for event in stream:
                if event.type == "message":
                    streamed_text += event.delta.get("content", "")
        return streamed_text
    except OpenAIError as e:
        logger.error(f"❌ Error streaming reply: {e}")
        return "Error streaming reply."
