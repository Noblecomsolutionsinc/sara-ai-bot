# streaming_server.py
import os
import asyncio
import websockets
import json
import logging
from pathlib import Path
from elevenlabs import generate, play, set_api_key

# --- Logging setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming")

# --- Environment Variables ---
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
MP3_RETENTION_HOURS = int(os.environ.get("MP3_RETENTION_HOURS", 24))
PUBLIC_STREAMING_URL = os.environ.get("PUBLIC_STREAMING_URL", "wss://sara-ai-streaming.onrender.com/ws")

if not ELEVENLABS_API_KEY:
    log.error("ELEVENLABS_API_KEY not set. TTS streaming will fail.")
set_api_key(ELEVENLABS_API_KEY)

# Load Sara JSONs
DATA_DIR = Path("data")
SARA_CALLFLOW = json.loads((DATA_DIR / "Sara_CallFlow.json").read_text(encoding="utf-8"))
SARA_KNOWLEDGE = json.loads((DATA_DIR / "Sara_KnowledgeBase.json").read_text(encoding="utf-8"))
SARA_OBJECTIONS = json.loads((DATA_DIR / "Sara_Objections.json").read_text(encoding="utf-8"))
SARA_OPENING = json.loads((DATA_DIR / "Sara_Opening.json").read_text(encoding="utf-8"))
SARA_PLAYBOOK = json.loads((DATA_DIR / "Sara_Playbook.json").read_text(encoding="utf-8"))
SARA_SYSTEM = json.loads((DATA_DIR / "Sara_SystemPrompt_Production.json").read_text(encoding="utf-8"))

# --- Active streams ---
active_streams = {}

# --- Core async handler ---
async def handle_stream(websocket, path):
    """
    Handles incoming Twilio Media Streams
    Receives audio frames in base64, processes with TTS and ChatGPT, sends back responses if needed
    """
    # Parse query params from path
    query = {}
    if "?" in path:
        qs = path.split("?", 1)[1]
        query = dict(qc.split("=") for qc in qs.split("&"))
    business_name = query.get("business_name", "Unknown Business")
    business_type = query.get("business_type", "general")

    log.info("New stream connected: %s (%s)", business_name, business_type)
    stream_id = id(websocket)
    active_streams[stream_id] = {"name": business_name, "type": business_type}

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                event = data.get("event")
                if event == "media":
                    payload = data.get("media", {})
                    audio_base64 = payload.get("payload")
                    # TODO: forward audio to ChatGPT or other processing pipeline
                    # For now just log receipt
                    log.info("Received audio frame for %s (%d bytes)", business_name, len(audio_base64 or ""))
                elif event == "start":
                    log.info("Stream started: %s", business_name)
                elif event == "stop":
                    log.info("Stream stopped: %s", business_name)
                else:
                    log.debug("Unknown event: %s", event)
            except Exception as e:
                log.exception("Failed to process stream message: %s", e)
    except websockets.exceptions.ConnectionClosed:
        log.info("Stream closed: %s", business_name)
    finally:
        active_streams.pop(stream_id, None)

# --- WebSocket server ---
async def main():
    port = int(os.environ.get("PORT", 8765))
    server = await websockets.serve(handle_stream, "0.0.0.0", port, ping_interval=30)
    log.info("Streaming server running on port %d", port)
    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down streaming server")
