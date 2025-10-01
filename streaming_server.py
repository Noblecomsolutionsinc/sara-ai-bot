import asyncio
import websockets
import json
import logging
import os
from gpt_client import generate_reply

# --------------------------------------------------
# Logging Setup
# --------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("streaming_server")

# --------------------------------------------------
# Config
# --------------------------------------------------
WS_PORT = 8000
WS_HOST = "0.0.0.0"

# Retry / Timeout Settings
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", 2))
RETRY_BACKOFF_SECONDS = int(os.getenv("RETRY_BACKOFF_SECONDS", 1))
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", 30))


# --------------------------------------------------
# Safe Call to GPT with Retry + Timeout
# --------------------------------------------------
async def safe_generate_reply(user_input: str) -> str:
    last_exception = None

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            logger.info(f"🔄 Attempt {attempt}/{RETRY_ATTEMPTS} for GPT call")

            return await asyncio.wait_for(
                generate_reply(user_input),
                timeout=OPENAI_TIMEOUT
            )

        except asyncio.TimeoutError:
            logger.error(f"⏱️ GPT call timed out (attempt {attempt})")
            last_exception = "OpenAI API timeout"
        except Exception as e:
            logger.error(f"❌ GPT error (attempt {attempt}): {e}")
            last_exception = str(e)

        if attempt < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)

    return f"[System Error: {last_exception}]"


# --------------------------------------------------
# WebSocket Client Handler
# --------------------------------------------------
async def handler(websocket, path):
    logger.info(f"✅ Client connected: {websocket.remote_address}")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                user_input = data.get("text", "").strip()

                if not user_input:
                    await websocket.send(json.dumps({"error": "Empty input"}))
                    continue

                logger.info(f"➡️ User said: {user_input}")

                # GPT reply (robust)
                reply = await safe_generate_reply(user_input)

                logger.info(f"⬅️ Sara reply: {reply}")

                await websocket.send(json.dumps({"reply": reply}))

            except json.JSONDecodeError:
                logger.warning("⚠️ Invalid JSON from client")
                await websocket.send(json.dumps({"error": "Invalid JSON format"}))
            except Exception as e:
                logger.exception("❌ Error processing message")
                await websocket.send(json.dumps({"error": str(e)}))

    except websockets.exceptions.ConnectionClosed as e:
        logger.info(f"🔌 Connection closed: {e}")


# --------------------------------------------------
# Server Entry Point
# --------------------------------------------------
async def main():
    logger.info(f"🚀 Starting Sara AI WebSocket server on {WS_HOST}:{WS_PORT}")
    async with websockets.serve(handler, WS_HOST, WS_PORT):
        await asyncio.Future()  # keep running forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Server stopped manually")
