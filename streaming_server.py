import asyncio
import websockets
import logging
import os
from aiohttp import web   # 👈 new import
from sara_brain import SaraBrain

logger = logging.getLogger("sara-stream")

TWILIO_MEDIA_PORT = int(os.getenv("TWILIO_MEDIA_PORT", "8765"))

brain = SaraBrain()

async def handle_connection(websocket, path):
    logger.info("New connection")
    async for message in websocket:
        logger.debug(f"Received: {message[:100]}...")  # truncate log
        # TODO: process Twilio audio + GPT + ElevenLabs streaming here
        await websocket.send("ok")  # placeholder

async def websocket_server():
    server = await websockets.serve(
        handle_connection,
        "0.0.0.0",
        TWILIO_MEDIA_PORT,
        max_size=2**24
    )
    logger.info(f"Streaming server running on port {TWILIO_MEDIA_PORT}")
    await server.wait_closed()

# --- Health check HTTP server ---
async def handle_health(request):
    return web.json_response({"status": "ok", "service": "sara-streaming"})

async def http_server():
    app = web.Application()
    app.add_routes([web.get("/", handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)  # 👈 HTTP on port 8080
    await site.start()
    logger.info("Health-check HTTP server running on port 8080")

async def main():
    # Run both websocket + HTTP servers together
    await asyncio.gather(
        websocket_server(),
        http_server()
    )

if __name__ == "__main__":
    asyncio.run(main())
