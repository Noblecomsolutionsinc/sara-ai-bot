"""
gpt_client.py

Robust OpenAI HTTP API client that avoids SDK import/init issues (proxies/httpx mismatches).
- Uses requests to call the chat completions endpoint
- Retries with backoff, timeout handling
- Exposes an async `generate_reply` function that can be awaited by the server
"""

import os
import time
import logging
import json
from typing import List, Dict, Optional, Any
import requests
import asyncio

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gpt_client")

# Config from env
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is required")

OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", os.getenv("GPT_MODEL", "gpt-5-mini-2025-08-07"))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "30"))           # per-request timeout (s)
OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "512"))
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))

RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BACKOFF_SECONDS = float(os.getenv("RETRY_BACKOFF_SECONDS", "1.0"))

# Shared requests session for connection reuse
_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        })
        _session = s
    return _session


def call_gpt_sync(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    max_completion_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: Optional[int] = None,
    retries: int = None,
    backoff: float = None
) -> Optional[str]:
    """
    Synchronous HTTP call to OpenAI Chat Completions endpoint with retries/backoff.
    Returns reply string or None on failure.
    """
    model = model or OPENAI_MODEL
    max_completion_tokens = max_completion_tokens or OPENAI_MAX_TOKENS
    temperature = temperature if temperature is not None else OPENAI_TEMPERATURE
    timeout = timeout or OPENAI_TIMEOUT
    retries = RETRY_ATTEMPTS if retries is None else retries
    backoff = RETRY_BACKOFF_SECONDS if backoff is None else backoff

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        # Newer GPT-5-style param; include for models that accept it.
        "max_completion_tokens": int(max_completion_tokens),
        "temperature": float(temperature),
    }

    session = _get_session()

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            logger.debug("OpenAI request attempt %d: model=%s tokens=%s", attempt, model, max_completion_tokens)
            resp = session.post(OPENAI_API_URL, json=payload, timeout=timeout)
            # Raise for HTTP errors
            resp.raise_for_status()
            data = resp.json()
            # Extract content safely
            choices = data.get("choices") or []
            if choices and len(choices) > 0:
                # New style: choices[0].message.content
                first = choices[0]
                msg = first.get("message") or {}
                content = msg.get("content")
                if content is None:
                    # Fallback: some responses use 'text' or direct 'content'
                    content = first.get("text") or first.get("content")
                if isinstance(content, str):
                    return content.strip()
            # If no choices, treat as error
            logger.warning("OpenAI returned no choices (attempt %d). Response: %s", attempt, resp.text[:1000])
            last_err = f"No choices in response: {resp.status_code}"
        except requests.exceptions.RequestException as e:
            logger.warning("OpenAI request exception (attempt %d/%d): %s", attempt, retries, e)
            last_err = str(e)
        except json.JSONDecodeError:
            logger.exception("OpenAI returned invalid JSON (attempt %d)", attempt)
            last_err = "invalid_json"
        except Exception as e:
            logger.exception("Unexpected error calling OpenAI (attempt %d)", attempt)
            last_err = str(e)

        # Backoff before retrying
        if attempt < retries:
            sleep_for = backoff * (2 ** (attempt - 1))  # exponential backoff
            logger.debug("Sleeping %s seconds before next attempt", sleep_for)
            time.sleep(sleep_for)

    logger.error("All OpenAI attempts failed. Last error: %s", last_err)
    return None


async def generate_reply(user_input: str,
                         system_prompt: Optional[str] = None,
                         model: Optional[str] = None,
                         max_completion_tokens: Optional[int] = None,
                         temperature: Optional[float] = None,
                         timeout: Optional[int] = None) -> str:
    """
    Async wrapper around call_gpt_sync so other async code can await it.
    Returns a non-empty string even on failure (keeps upstream robust).
    """
    system_prompt = system_prompt or os.getenv("GPT_SYSTEM_PROMPT", "You are Sara, a professional outbound AI cold caller. Be concise, clear, and persuasive.")
    model = model or OPENAI_MODEL
    max_completion_tokens = max_completion_tokens or OPENAI_MAX_TOKENS
    temperature = temperature if temperature is not None else OPENAI_TEMPERATURE
    timeout = timeout or OPENAI_TIMEOUT

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input}
    ]

    # run blocking HTTP call in a thread to avoid blocking the event loop
    reply = await asyncio.to_thread(
        call_gpt_sync,
        messages,
        model,
        max_completion_tokens,
        temperature,
        timeout,
        RETRY_ATTEMPTS,
        RETRY_BACKOFF_SECONDS
    )

    if reply:
        return reply
    # Fallback message to ensure upstream always receives something
    return "[System Error: GPT failed to produce a reply]"

# Backwards-compatible alias
call_gpt = call_gpt_sync
