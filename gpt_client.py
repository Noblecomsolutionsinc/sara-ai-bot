# gpt_client.py
import os
import logging
from openai import OpenAI

logging.basicConfig(level=logging.INFO)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY environment variable")

try:
    client = OpenAI(api_key=OPENAI_API_KEY)
    logging.info("✅ OpenAI client initialized successfully")
except Exception as e:
    logging.error(f"❌ Failed to init OpenAI client: {e}")
    raise


def generate_reply(messages, model="gpt-4o-mini", max_tokens=500):
    """
    Call OpenAI to generate a conversational reply.
    :param messages: List of dicts [{"role": "user", "content": "Hello"}]
    :param model: OpenAI model to use
    :param max_tokens: Max tokens in response
    :return: String reply
    """
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"❌ OpenAI API call failed: {e}")
        return "I'm having trouble responding right now."
