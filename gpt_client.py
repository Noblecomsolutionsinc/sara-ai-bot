# gpt_client.py
import os
import time
import logging

try:
    # prefer the modern OpenAI client
    from openai import OpenAI
    _OPENAI_CLASS = True
except Exception:
    import openai
    _OPENAI_CLASS = False

LOG = logging.getLogger("gpt_client")
LOG.setLevel(os.getenv("LOG_LEVEL", "INFO"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini-2025-08-07")
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))

# initialize client
client = None
if _OPENAI_CLASS:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        LOG.exception("Failed to init OpenAI OpenAI() client")
else:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        client = openai
    except Exception:
        LOG.exception("Failed to init openai module client")

def call_gpt(prompt_or_messages, max_completion_tokens=256, temperature=0.0, retries=2):
    """
    prompt_or_messages: either a string or a list of message dicts
    returns: string (GPT content) or None
    """
    if client is None:
        LOG.error("OpenAI client not initialized")
        return None

    if isinstance(prompt_or_messages, str):
        messages = [{"role": "system", "content": "You are Sara AI assistant."},
                    {"role": "user", "content": prompt_or_messages}]
    else:
        messages = prompt_or_messages

    for attempt in range(1, retries + 1):
        try:
            if _OPENAI_CLASS:
                resp = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    max_completion_tokens=max_completion_tokens,
                    temperature=temperature,
                    request_timeout=OPENAI_TIMEOUT
                )
                content = resp.choices[0].message.content
            else:
                resp = client.ChatCompletion.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    max_tokens=max_completion_tokens,
                    temperature=temperature,
                    timeout=OPENAI_TIMEOUT
                )
                content = resp.choices[0].message["content"]
            return content
        except Exception as e:
            LOG.warning("call_gpt attempt %d failed: %s", attempt, e)
            time.sleep(1 * attempt)
    LOG.error("All call_gpt attempts failed")
    return None
