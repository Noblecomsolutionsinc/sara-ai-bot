# gpt_client.py
import os
import requests
import logging
import time
import json

LOG = logging.getLogger("gpt_client")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "20"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "256"))

def call_gpt(prompt: str, model: str = "gpt-5-mini", max_tokens: int = None, temperature: float = 0.2, retries: int = 2):
    if not OPENAI_KEY:
        LOG.error("OPENAI_API_KEY not configured")
        return None
    max_tokens = max_tokens or min(OPENAI_MAX_TOKENS, 256)
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    backoff = 1.0
    for attempt in range(retries + 1):
        try:
            r = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=OPENAI_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            # Normalize outputs: try a few fields
            # Responses API may contain 'output' array with 'content' items
            if isinstance(data, dict):
                return data
            return data
        except Exception as e:
            LOG.warning("GPT call failed attempt %d: %s", attempt + 1, e)
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
            else:
                LOG.exception("GPT call final failure")
                return None
