# gpt_client.py
"""
Robust OpenAI client using the official python SDK.
Logs full request info (but not API key) for debugging.
Retries on failure with exponential backoff.
Designed for gpt-5-mini via ChatCompletion API.
"""

import os
import time
import logging
from typing import List, Dict, Optional

import openai

LOG = logging.getLogger("gpt_client")
LOG.setLevel(logging.INFO)

# envs
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = os.environ.get("OPENAI_API_URL")  # optional override (like https://api.openai.com/v1)
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "1000"))
GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-5-mini")
RETRIES = int(os.environ.get("OPENAI_RETRIES", "3"))

if not OPENAI_API_KEY:
    LOG.error("OPENAI_API_KEY is not set. Set it in environment.")
openai.api_key = OPENAI_API_KEY

# Optionally override base URL (careful: usually not needed)
if OPENAI_API_URL:
    openai.api_base = OPENAI_API_URL.rstrip("/")  # e.g. https://api.openai.com/v1
    LOG.info("openai.api_base set to %s", openai.api_base)

def call_gpt(messages: List[Dict[str, str]], max_tokens: Optional[int] = None, temperature: float = 0.2) -> Optional[Dict]:
    """
    messages: list of {"role":"system|user|assistant","content":"..."}
    Returns the response dict or None on final failure.
    """
    if not isinstance(messages, list) or len(messages) == 0:
        LOG.error("call_gpt: invalid messages payload")
        return None

    max_tokens = max_tokens or OPENAI_MAX_TOKENS
    attempt = 0
    backoff = 1.0

    # Log safe request summary (avoid logging API key)
    try:
        LOG.info("call_gpt: model=%s messages=%d max_tokens=%s timeout=%s",
                 GPT_MODEL, len(messages), max_tokens, OPENAI_TIMEOUT)
    except Exception:
        pass

    while attempt < RETRIES:
        attempt += 1
        try:
            LOG.debug("call_gpt: attempt=%d", attempt)
            # Use ChatCompletion API
            resp = openai.ChatCompletion.create(
                model=GPT_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                request_timeout=OPENAI_TIMEOUT,
            )
            LOG.info("call_gpt: success attempt=%d model=%s", attempt, GPT_MODEL)
            # return a normal dict for callers
            return resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
        except openai.error.OpenAIError as e:
            # SDK-level errors
            LOG.warning("call_gpt: OpenAIError attempt=%d: %s", attempt, repr(e))
            # try to extract http status / error body
            try:
                if hasattr(e, "http_status"):
                    LOG.warning("call_gpt: http_status=%s", e.http_status)
                if hasattr(e, "error") and isinstance(e.error, dict):
                    LOG.warning("call_gpt: error body=%s", e.error)
            except Exception:
                LOG.exception("call_gpt: error logging failure")
        except Exception as e:
            LOG.exception("call_gpt: unexpected exception attempt=%d: %s", attempt, e)

        # backoff and retry
        LOG.info("call_gpt: backing off %.1fs before retry (attempt=%d)", backoff, attempt)
        time.sleep(backoff)
        backoff *= 2.0

    LOG.error("call_gpt: all %d attempts failed", RETRIES)
    return None
