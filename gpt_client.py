# gpt_client.py
import os
import time
import logging
from typing import List, Dict, Optional

import openai
from requests import HTTPError

LOG = logging.getLogger("gpt_client")
LOG.setLevel(logging.INFO)

# Read envs
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_API_URL = os.environ.get("OPENAI_API_URL")  # optional override
OPENAI_TIMEOUT = int(os.environ.get("OPENAI_TIMEOUT", "60"))
OPENAI_MAX_TOKENS = int(os.environ.get("OPENAI_MAX_TOKENS", "1000"))
GPT_MODEL = os.environ.get("GPT_MODEL", "gpt-5-mini")  # default to gpt-5-mini

if not OPENAI_API_KEY:
    LOG.warning("OPENAI_API_KEY not set. GPT calls will fail unless set in environment.")

# Configure openai library
openai.api_key = OPENAI_API_KEY
if OPENAI_API_URL:
    # If user provided a full URL, allow the SDK to use it as base
    openai.api_base = OPENAI_API_URL.rstrip("/")  # e.g. https://api.openai.com/v1
    LOG.info("openai.api_base set to %s", openai.api_base)

def call_gpt(messages: List[Dict[str, str]], max_tokens: Optional[int] = None, temperature: float = 0.2, retries: int = 3) -> Optional[Dict]:
    """
    Calls OpenAI ChatCompletion and returns the response dict, or None on failure.
    `messages` is a list like [{"role":"system","content":"..."}, {"role":"user","content":"..."}]
    """
    if not messages or not isinstance(messages, list):
        LOG.error("call_gpt: invalid `messages` payload (empty or not a list).")
        return None

    max_tokens = max_tokens or OPENAI_MAX_TOKENS

    attempt = 0
    backoff = 1.0
    while attempt < retries:
        attempt += 1
        try:
            LOG.debug("call_gpt attempt=%d model=%s tokens=%s msgs=%d", attempt, GPT_MODEL, max_tokens, len(messages))
            resp = openai.ChatCompletion.create(
                model=GPT_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                request_timeout=OPENAI_TIMEOUT,
            )
            LOG.info("gpt_client: success model=%s usage=%s", GPT_MODEL, getattr(resp, "usage", {}))
            # Convert response to plain dict and return
            return resp.to_dict() if hasattr(resp, "to_dict") else dict(resp)
        except openai.error.OpenAIError as e:
            # Log helpful details
            LOG.warning("GPT call failed attempt %d: %s", attempt, repr(e))
            try:
                # Some OpenAI errors include .http_status or .error details
                if hasattr(e, "http_status"):
                    LOG.warning("OpenAI HTTP status: %s", e.http_status)
                if hasattr(e, "error") and isinstance(e.error, dict):
                    LOG.warning("OpenAI error body: %s", e.error)
            except Exception:
                LOG.exception("failed to log openai error details")
        except Exception as e:
            LOG.exception("Unexpected exception while calling GPT (attempt %d): %s", attempt, e)

        # exponential backoff
        LOG.info("gpt_client: backing off %s seconds before retry", backoff)
        time.sleep(backoff)
        backoff *= 2.0

    LOG.error("gpt_client: all %d attempts failed", retries)
    return None
