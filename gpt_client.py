#!/usr/bin/env python3
"""
gpt_client.py

OpenAI GPT client wrapper using OpenAI Python client (v1.x semantics).
- Uses model from OPENAI_MODEL (default "gpt-5-mini").
- Uses max_completion_tokens for GPT-5 family.
- Conditional temperature: only included if OPENAI_ALLOW_TEMPERATURE=true.
- Retries & backoff on transient errors.
- Safe fallback if OPENAI_API_KEY missing or all attempts fail.

Public API:
    generate_reply(history: list[dict]) -> str
        history: list of {"role": "system"/"user"/"assistant", "content": str}

Returns assistant reply text (str). Never raises -- returns fallback string on error.
"""
from __future__ import annotations

import os
import time
import logging
from typing import List, Dict, Any, Optional

from openai import OpenAI
import requests  # used only for health-check fallback where relevant

# ---------------------------
# Configuration
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini")
OPENAI_MAX_COMPLETION_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "200"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "20"))
OPENAI_ALLOW_TEMPERATURE = os.getenv("OPENAI_ALLOW_TEMPERATURE", "false").lower() in ("1", "true", "yes")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
RETRY_ATTEMPTS = int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = float(os.getenv("OPENAI_RETRY_BACKOFF_SECONDS", "2"))

FALLBACK_REPLY = "I'm sorry, I'm having trouble right now. Goodbye."

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("gpt_client")

# Instantiate client if key present
_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    try:
        _client = OpenAI(api_key=OPENAI_API_KEY, default_request_timeout=OPENAI_TIMEOUT)
    except Exception:
        logger.exception("Failed to initialize OpenAI client.")
        _client = None
else:
    logger.warning("OPENAI_API_KEY is not set. GPT calls will return fallback replies.")


logger.info(
    "GPT client configured: model=%s max_completion_tokens=%s allow_temperature=%s timeout=%s",
    OPENAI_MODEL,
    OPENAI_MAX_COMPLETION_TOKENS,
    OPENAI_ALLOW_TEMPERATURE,
    OPENAI_TIMEOUT,
)


# Helper: whether to include temperature for this model
def _should_include_temperature(model: str) -> bool:
    # Default safe behavior: include only if OPENAI_ALLOW_TEMPERATURE is true.
    # You can extend with a model-name whitelist if some models do not support temperature.
    return OPENAI_ALLOW_TEMPERATURE


# Core function
def generate_reply(history: List[Dict[str, str]]) -> str:
    """
    Generate assistant reply from conversation history (list of role/content dicts).
    Returns assistant reply string or FALLBACK_REPLY on failure.
    """
    if not OPENAI_API_KEY or _client is None:
        logger.error("OPENAI_API_KEY missing or OpenAI client not initialized; returning fallback.")
        return FALLBACK_REPLY

    # Build the payload
    payload: Dict[str, Any] = {
        "model": OPENAI_MODEL,
        "messages": history,
        "max_completion_tokens": OPENAI_MAX_COMPLETION_TOKENS,
    }

    # Conditionally attach temperature
    if _should_include_temperature(OPENAI_MODEL):
        payload["temperature"] = OPENAI_TEMPERATURE

    # Use client.chat.completions.create per v1.x semantics
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            logger.debug("Calling OpenAI (attempt %s) payload keys: %s", attempt, list(payload.keys()))
            response = _client.chat.completions.create(
                model=payload["model"],
                messages=payload["messages"],
                max_completion_tokens=payload["max_completion_tokens"],
                # temperature may or may not be included; handle both cases
                **({"temperature": payload["temperature"]} if "temperature" in payload else {}),
            )
            # Parse response
            if not response or not hasattr(response, "choices"):
                logger.warning("OpenAI returned unexpected response struct: %s", response)
                raise RuntimeError("Unexpected OpenAI response")

            choice = response.choices[0]
            # New SDK shape: choice.message.content
            text = ""
            try:
                text = choice.message.content
            except Exception:
                # Fallback: try choice["message"]["content"]
                try:
                    text = choice["message"]["content"]
                except Exception:
                    logger.exception("Unable to extract content from OpenAI response: %s", choice)
                    text = ""

            if text and isinstance(text, str):
                reply = text.strip()
                logger.info("OpenAI reply (len=%d) received", len(reply))
                return reply
            else:
                logger.warning("OpenAI reply empty or invalid, attempt %s", attempt)
                # Fallthrough to retry logic
        except Exception as exc:
            # If error mentions temperature not supported and we included it, retry without it
            err_str = str(exc).lower()
            logger.warning("OpenAI call failed on attempt %s: %s", attempt, exc)
            if "temperature" in payload and ("invalid" in err_str or "unsupported" in err_str):
                logger.warning("OpenAI rejected temperature parameter; retrying without temperature.")
                payload.pop("temperature", None)
                # retry immediately (do not backoff for this specific fix)
                continue
            # Else exponential backoff and retry
            if attempt < RETRY_ATTEMPTS:
                backoff = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                logger.debug("Sleeping %.1fs before retrying OpenAI", backoff)
                time.sleep(backoff)
                continue
            else:
                logger.exception("OpenAI all attempts failed.")
                return FALLBACK_REPLY

    # If somehow no reply obtained
    logger.error("OpenAI failed to produce a reply after retries; returning fallback.")
    return FALLBACK_REPLY
