#!/usr/bin/env python3
"""
tasks.py

Celery task definitions for Sara AI:
- process_event: light-weight event handler (stores meta)
- process_audio_chunk: STT -> GPT -> TTS pipeline (atomic audio consumption)
- ping: health check task

Notes:
- Uses Redis for audio buffers and history
- Enforces forbidden-phrase detection before TTS and triggers emergency hangup via Twilio if configured
"""
from __future__ import annotations

import os
import json
import time
import logging
import redis
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from celery_app import worker
from stt_client import transcribe_from_base64_chunks
from gpt_client import generate_reply
from tts_client import synthesize_speech

# Optional Twilio REST client import (for emergency hangup)
try:
    from twilio.rest import Client as TwilioClient
except Exception:
    TwilioClient = None

# Config
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FALLBACK_REPLY = os.getenv("FALLBACK_REPLY", "I'm sorry, I'm having trouble right now. Goodbye.")
MP3_RETENTION_HOURS = int(os.getenv("MP3_RETENTION_HOURS", "48"))

# Forbidden phrases (case-insensitive substrings)
FORBIDDEN_SUBSTRINGS = [
    "i am an ai", "i'm an ai", "as an ai", "i am a bot", "i'm a bot",
    "language model", "as a language model", "ai assistant", "automated system", "virtual assistant"
]

# Logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("tasks")

# Redis client
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Twilio client if available
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TwilioClient is not None:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("Twilio REST client initialized in tasks.")
    except Exception:
        logger.exception("Failed to initialize Twilio client in tasks; emergency hangup via Twilio disabled.")
        twilio_client = None


def now_epoch() -> float:
    return time.time()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@worker.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True)
def process_event(self, event: Dict[str, Any]):
    """
    Lightweight event handler: store some meta and enqueue audio processing when media frames arrive.
    """
    try:
        event_type = event.get("event") or event.get("type") or "unknown"
        call_sid = event.get("callSid") or event.get("streamSid") or event.get("CallSid")
        logger.info("process_event event_type=%s call_sid=%s", event_type, call_sid)

        if not call_sid:
            logger.warning("process_event received event without callSid; ignoring.")
            return

        meta_key = f"call:{call_sid}:meta"
        # update last event metadata
        r.hset(meta_key, mapping={
            "last_event_type": event_type,
            "last_event_ts": now_epoch(),
        })

        if event_type == "media":
            # Twilio sends media payload under event["media"]["payload"]
            media = event.get("media") or {}
            payload_b64 = media.get("payload") or media.get("chunk") or None
            if payload_b64:
                buf_key = f"call:{call_sid}:audio_buf"
                r.rpush(buf_key, payload_b64)
                # Optionally trigger processing immediately or in batches
                # Here we enqueue a dedicated task to consume the buffer atomically
                process_audio_chunk.delay(call_sid)
            else:
                logger.debug("media event missing payload for call %s", call_sid)

        # handle start/stop markers if sent by Twilio Media Streams
        if event_type == "start":
            r.hset(meta_key, mapping={"status": "started", "started_at_iso": now_iso(), "started_at_epoch": now_epoch()})
        if event_type == "stop":
            r.hset(meta_key, mapping={"status": "stopped", "stopped_at_iso": now_iso(), "stopped_at_epoch": now_epoch()})

    except Exception:
        logger.exception("process_event failed")
        raise


@worker.task(bind=True, max_retries=3, autoretry_for=(Exception,), retry_backoff=True)
def process_audio_chunk(self, call_sid: str):
    """
    End-to-end processing for queued audio in Redis for a given callSid.
    Steps:
    1) Atomically retrieve and clear audio buffer (LRANGE + DEL)
    2) STT: transcribe concatenated audio
    3) Append user message to history
    4) Build messages list (system prompt from meta + history)
    5) Call GPT (generate_reply)
    6) Forbidden-phrase check -> emergency hangup if triggered
    7) TTS generation -> store last_tts_url in meta and append assistant entry to history
    """
    try:
        if not call_sid:
            logger.warning("process_audio_chunk called without call_sid")
            return

        logger.info("process_audio_chunk started for %s", call_sid)
        buf_key = f"call:{call_sid}:audio_buf"
        history_key = f"call:{call_sid}:history"
        meta_key = f"call:{call_sid}:meta"

        # Atomically fetch and delete buffered audio
        try:
            chunks = r.lrange(buf_key, 0, -1) or []
            r.delete(buf_key)
        except Exception:
            logger.exception("Failed to atomically fetch/delete audio buffer for %s", call_sid)
            chunks = []

        if not chunks:
            logger.debug("No audio chunks to process for %s", call_sid)
            return

        # STT (attempts internal)
        try:
            transcript = transcribe_from_base64_chunks(chunks)
        except Exception:
            logger.exception("STT raised exception for %s", call_sid)
            transcript = ""

        if not transcript:
            logger.info("Empty transcript for %s; storing silence marker", call_sid)
            # Optionally increment silence counter
            r.rpush(history_key, json.dumps({"role": "user", "content": "", "ts": now_iso(), "note": "silence"}))
            return

        # Append user transcription to history (as a JSON line)
        r.rpush(history_key, json.dumps({"role": "user", "content": transcript, "ts": now_iso()}))
        logger.info("Appended user transcript to history for %s", call_sid)

        # Build messages list for GPT: system prompt + conversation history
        messages: List[Dict[str, str]] = []
        meta = r.hgetall(meta_key) or {}
        system_prompt = meta.get("system_prompt")
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        else:
            # Provide a default system prompt that enforces non-disclosure
            messages.append({"role": "system", "content":
                             "You are Sara Hayes, a human sales representative for Noblecom Solutions. "
                             "You must never admit you are an AI. Keep responses short and human-like."})

        # Read history (r.lrange returns list of JSON strings)
        try:
            raw_history = r.lrange(history_key, 0, -1) or []
            for item in raw_history:
                try:
                    entry = json.loads(item)
                    role = entry.get("role") or "user"
                    content = entry.get("content") or ""
                    messages.append({"role": role, "content": content})
                except Exception:
                    logger.debug("Skipping malformed history item for %s: %s", call_sid, item)
        except Exception:
            logger.exception("Failed to read history for %s", call_sid)

        # Call GPT (with retries handled inside generate_reply)
        reply = generate_reply(messages)
        if not reply:
            logger.warning("GPT returned empty or None for %s, using fallback", call_sid)
            reply = FALLBACK_REPLY

        logger.info("GPT reply for %s: %s", call_sid, reply[:300])

        # Forbidden phrase runtime check (case-insensitive substring)
        low = reply.lower()
        forbidden = False
        offending_phrase = None
        for ph in FORBIDDEN_SUBSTRINGS:
            if ph in low:
                forbidden = True
                offending_phrase = ph
                break

        if forbidden:
            logger.warning("Forbidden phrase detected for %s: %s", call_sid, offending_phrase)
            # Record incident
            incident_key = f"sara:incident:{call_sid}"
            r.hset(incident_key, mapping={
                "offending_text": reply,
                "offending_phrase": offending_phrase,
                "detected_at_epoch": now_epoch(),
                "detected_at_iso": now_iso()
            })
            # Emergency hangup: if Twilio client is available, try to update the live call
            if twilio_client:
                try:
                    emergency_twiml = "<?xml version='1.0' encoding='UTF-8'?><Response><Say>I'm sorry, I'm having technical difficulties. Goodbye.</Say><Hangup/></Response>"
                    twilio_client.calls(call_sid).update(twiml=emergency_twiml)
                    logger.info("Issued emergency hangup for call %s via Twilio", call_sid)
                except Exception:
                    logger.exception("Failed to issue emergency hangup via Twilio for %s", call_sid)
            else:
                logger.warning("No Twilio client available to hang up call %s; ensure SKIP_TWILIO_VALIDATION is off and Twilio creds set", call_sid)
            # Do not create TTS for forbidden reply; stop here.
            return

        # Generate TTS for reply (with retries)
        tts_url = None
        try:
            tts_url = synthesize_speech(reply, session_id=call_sid)
        except Exception:
            logger.exception("TTS synthesis raised exception for %s", call_sid)
            tts_url = None

        if not tts_url:
            logger.error("TTS generation failed for %s; storing fallback and scheduling hangup", call_sid)
            # Store fallback in meta to be played then hangup
            fallback_url = None
            try:
                fallback_url = synthesize_speech(FALLBACK_REPLY, session_id=f"{call_sid}-fallback")
            except Exception:
                logger.exception("Fallback TTS generation failed for %s", call_sid)
                fallback_url = None

            if fallback_url:
                r.hset(meta_key, mapping={"last_tts_url": fallback_url, "playback_hangup_after_play": "1"})
            else:
                # Nothing to play; attempt emergency hangup directly
                if twilio_client:
                    try:
                        emergency_twiml = "<?xml version='1.0' encoding='UTF-8'?><Response><Say>I'm sorry, I'm having technical difficulties. Goodbye.</Say><Hangup/></Response>"
                        twilio_client.calls(call_sid).update(twiml=emergency_twiml)
                        logger.info("Issued emergency hangup for call %s due to TTS failure", call_sid)
                    except Exception:
                        logger.exception("Failed to issue emergency hangup via Twilio for %s", call_sid)
            return

        # Save assistant entry into history and set last_tts_url in meta
        assistant_entry = {"role": "assistant", "content": reply, "tts_url": tts_url, "ts": now_iso()}
        r.rpush(history_key, json.dumps(assistant_entry))
        r.hset(meta_key, mapping={"last_gpt": reply, "last_tts_url": tts_url, "last_pipeline_ts": now_epoch()})
        logger.info("Saved TTS URL for %s -> %s", call_sid, tts_url)

    except Exception:
        logger.exception("process_audio_chunk unexpected failure for %s", call_sid)
        raise


@worker.task(bind=True)
def ping(self, payload: Optional[dict] = None):
    """Health ping task."""
    logger.debug("ping received: %s", payload)
    return {"pong": payload, "ts": now_iso()}
