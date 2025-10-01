"""
tasks.py

Celery tasks for processing Twilio Media Stream events:
- handle_twilio_event -> enqueues
- process_event -> handles start/media/stop
- Uses stt_client.transcribe_from_base64_chunks for STT (OpenAI Whisper)
- Uses gpt_client.generate_reply for conversation
- Uses tts_client.synthesize_speech for ElevenLabs TTS
"""

import os
import json
import time
import logging
import redis
import asyncio
from celery_app import celery
from stt_client import transcribe_from_base64_chunks
import gpt_client
import tts_client

logger = logging.getLogger("tasks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

# Behavior tuning
# Number of Twilio audio frames to collect before calling STT
STT_CHUNK_COUNT = int(os.getenv("STT_CHUNK_COUNT", "8"))
# Time (seconds) to wait after last media before forcing STT (simple debounce)
STT_DEBOUNCE_SECONDS = float(os.getenv("STT_DEBOUNCE_SECONDS", "0.8"))

@celery.task
def process_event(event: dict):
    """
    Main Celery task to process Twilio events.
    """
    try:
        event_type = event.get("event")
        stream_sid = event.get("streamSid") or event.get("callSid") or event.get("streamsid") or "unknown"
        logger.debug("process_event: type=%s streamSid=%s", event_type, stream_sid)
    except Exception as e:
        logger.exception("Malformed event: %s", e)
        return

    if event_type == "start":
        # Set call started state
        r.hset(f"call:{stream_sid}:meta", mapping={"status": "started", "started_at": time.time()})
        # Initialize buffers
        r.delete(f"call:{stream_sid}:audio_buf")
        r.delete(f"call:{stream_sid}:history")
        r.hset(f"call:{stream_sid}:meta", "playback_active", "0")
        logger.info("Call %s started", stream_sid)
        return {"status": "started"}

    if event_type == "stop":
        # mark stopped
        r.hset(f"call:{stream_sid}:meta", mapping={"status": "stopped", "stopped_at": time.time()})
        # cleanup buffers if needed
        r.delete(f"call:{stream_sid}:audio_buf")
        logger.info("Call %s stopped", stream_sid)
        return {"status": "stopped"}

    if event_type == "media":
        # media event contains event["media"]["payload"]
        media = event.get("media", {})
        payload_b64 = media.get("payload")
        if not payload_b64:
            logger.debug("Empty media payload")
            return {"status": "empty"}

        # Push chunk into Redis list (right push)
        buf_key = f"call:{stream_sid}:audio_buf"
        r.rpush(buf_key, payload_b64)
        r.hset(f"call:{stream_sid}:meta", "last_media_ts", time.time())

        # If playback is active, and human speaks -> barge-in
        playback_active = r.hget(f"call:{stream_sid}:meta", "playback_active")
        if playback_active and playback_active == "1":
            # Human spoke during playback -> interrupt
            logger.info("Barge-in detected for %s; requesting playback stop", stream_sid)
            # Set flag so streaming_server will send stop to Twilio (streaming_server monitors this key)
            r.hset(f"call:{stream_sid}:meta", "request_playback_stop", "1")
            # Continue processing media (we will transcribe)
        # Check buffer length and maybe trigger STT
        buf_len = r.llen(buf_key)
        if buf_len >= STT_CHUNK_COUNT:
            # Pop all current chunks for transcription
            chunks = []
            for _ in range(buf_len):
                c = r.lpop(buf_key)
                if c:
                    chunks.append(c)
            # Transcribe in a synchronous Celery worker (blocking ok)
            logger.debug("Transcribing %d chunks for call %s", len(chunks), stream_sid)
            transcript = transcribe_from_base64_chunks(chunks)
            if transcript:
                logger.info("Transcript for %s: %s", stream_sid, transcript)
                # Append to conversation history
                hist_key = f"call:{stream_sid}:history"
                history_json = r.get(hist_key) or "[]"
                try:
                    history = json.loads(history_json)
                except Exception:
                    history = []
                # Append user message
                history.append({"role": "user", "content": transcript})
                r.set(hist_key, json.dumps(history))
                # Call GPT to generate reply (sync or async)
                try:
                    # gpt_client.generate_reply might be async or sync; handle both
                    if asyncio.iscoroutinefunction(gpt_client.generate_reply):
                        # run in event loop
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        reply = loop.run_until_complete(gpt_client.generate_reply(transcript, system_prompt=None))
                        loop.close()
                    else:
                        # blocking call; run directly
                        reply = gpt_client.generate_reply(transcript, system_prompt=None)
                except Exception as e:
                    logger.exception("GPT generation failed: %s", e)
                    reply = "I'm sorry — I didn't catch that. Could you repeat?"

                logger.info("GPT reply for %s: %s", stream_sid, reply)

                # Append assistant reply to history
                history.append({"role": "assistant", "content": reply})
                r.set(hist_key, json.dumps(history))

                # Generate TTS
                try:
                    mp3_url = tts_client.synthesize_speech(reply, stream_sid)
                    if mp3_url:
                        # Store the last tts url so streaming server will pick it up
                        r.hset(f"call:{stream_sid}:meta", "last_tts_url", mp3_url)
                        logger.info("Stored TTS URL for %s -> %s", stream_sid, mp3_url)
                    else:
                        logger.warning("TTS generation returned empty for %s", stream_sid)
                except Exception as e:
                    logger.exception("TTS error for %s: %s", stream_sid, e)
            else:
                logger.debug("No transcript produced for %s", stream_sid)

        # Optionally, schedule a debounce check to flush any remaining audio after a short silence
        # (This implementation leaves it simple: subsequent media events will trigger more processing.)
        return {"status": "media_enqueued", "buf_len": buf_len}

    return {"status": "ignored", "type": event.get("event")}


def handle_twilio_event(event: dict):
    """
    Entrypoint called by streaming_server.py.
    Delegates event to Celery worker.
    """
    try:
        process_event.delay(event)
    except Exception:
        # Fallback: call synchronously if Celery isn't running
        try:
            process_event(event)
        except Exception:
            logger.exception("Failed to handle event synchronously")
