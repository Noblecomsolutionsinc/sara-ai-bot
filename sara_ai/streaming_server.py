import asyncio
import websockets
import os
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

# Initialize Sentry
init_sentry()

async def handler(websocket, path):
    try:
        async for message in websocket:
            log_event(service="streaming_server", event="message", status="received", message=message)
            await websocket.send(f"Echo: {message}")
    except Exception as e:
        log_event(service="streaming_server", event="error", status="failed", message=str(e))

async def main():
    port = int(os.getenv("STREAMING_PORT", 8765))
    log_event(service="streaming_server", event="startup", status="ok", message=f"Listening on {port}")
    async with websockets.serve(handler, "0.0.0.0", port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
