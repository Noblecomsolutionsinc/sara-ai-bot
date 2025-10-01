"""
streaming_server.py
Twilio Media Stream <-> GPT+TTS pipeline server

- Accepts WebSocket connections from Twilio Media Streams
- Routes audio/text via Redis + Celery
- Exposes /health endpoint for Render health checks
"""

import os
import asyncio
import logging
import websockets
import json
from aiohttp import web

# Local imports
from tasks import handle_twilio_event  # your Celery/Redis event handler

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("streaming_server")

# --- Config ---
PORT = int(os.environ.get("PORT", 8765))   # Render provides $PORT, default 8765
HOST = "0.0.0.0"                           # listen on all interfaces
WS_PATH = "/ws"


# --- WebSocket Handler ---
async def twilio_ws_handler(websocket, path):
    """
    Handle Twilio Media Stream WebSocket messages.
    """
    logger.info("New WS connection: %s", path)
    try:
        async for message in websocket:
            try:
                event = json.loads(message)
                await handle_twilio_event(event, websocket)
            except Exception as e:
                logger.exception("Error handling Twilio WS message: %s", e)
                await websocket.send(json.dumps({"error": str(e)}))
    except websockets.exceptions.ConnectionClosed:
        logger.info("WS connection closed: %s", path)
    except Exception as e:
        logger.exception("Unexpected error in WS handler: %s", e)


# --- HTTP Health Endpoint ---
async def healthcheck(request):
    return web.json_response({"status": "ok", "service": "streaming_server"})


# --- Main entrypoint ---
async def main():
    # WebSocket server
    ws_server = await websockets.serve(
        twilio_ws_handler, HOST, PORT, ping_interval=20, ping_timeout=20
    )
    logger.info("WebSocket server started on ws://%s:%s%s", HOST, PORT, WS_PATH)

    # aiohttp web app for healthcheck
    app = web.Application()
    app.router.add_get("/health", healthcheck)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    logger.info("HTTP healthcheck available at http://%s:%s/health", HOST, PORT)

    # Keep running forever
    await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down streaming_server...")
