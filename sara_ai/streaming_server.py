import asyncio
import websockets
import json
from sara_ai.logging_utils import log_event

HOST = "0.0.0.0"
PORT = 8765

async def handler(websocket, path):
    try:
        async for message in websocket:
            event = json.loads(message)
            if "CallSid" not in event:
                log_event(
                    service="streaming_server",
                    event="validation",
                    status="error",
                    message="Missing CallSid in event",
                )
                await websocket.send(json.dumps({"error": "Missing CallSid"}))
                continue

            log_event(
                service="streaming_server",
                event="event_received",
                status="success",
                message="Event received from client",
                extra={"event": event},
            )
            await websocket.send(json.dumps({"status": "ack"}))

    except Exception as e:
        log_event(
            service="streaming_server",
            event="connection",
            status="error",
            message=str(e),
        )

async def main():
    async with websockets.serve(handler, HOST, PORT):
        log_event(
            service="streaming_server",
            event="startup",
            status="success",
            message=f"WebSocket server started on {HOST}:{PORT}",
        )
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
