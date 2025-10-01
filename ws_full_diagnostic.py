import asyncio
import websockets
import logging
import socket
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')


def check_port(host, port, timeout=3):
    """Check if TCP port is open."""
    logging.info(f"Checking TCP port {host}:{port}...")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            logging.info("✅ Port is open.")
            return True
        except Exception as e:
            logging.error(f"❌ Port check failed: {e}")
            logging.info("💡 Suggestion: Ensure the server is running and firewall allows this port.")
            return False


async def test_ws_connection(ws_url, origin=None, token=None):
    headers = {}
    if origin:
        headers['Origin'] = origin
    if token:
        headers['Authorization'] = f"Bearer {token}"

    logging.info(f"Testing WebSocket: {ws_url} | Origin: {origin} | Token: {token}")

    try:
        async with websockets.connect(ws_url, extra_headers=headers) as ws:
            logging.info("✅ WebSocket connection successful!")

            # Test sending and receiving
            try:
                await ws.send("ping")
                msg = await ws.recv()
                logging.info(f"✅ Message roundtrip success. Received: {msg}")
            except Exception as e:
                logging.warning(f"⚠️ Connection OK, but failed to send/receive message: {e}")

    except websockets.InvalidStatusCode as e:
        logging.error(f"❌ Connection rejected. Status code: {e.status_code}")
        logging.info("💡 Suggestion: Check server authentication, origin header, or token.")
    except TypeError as e:
        logging.error(f"❌ Connection failed: {e}")
        logging.info("💡 Suggestion: 'extra_headers' may not be supported in this websockets version.")
    except Exception as e:
        logging.error(f"❌ Connection failed: {e}")
        logging.info("💡 Suggestion: Check server logs, network, and headers.")


def main():
    # Config: adjust to your setup
    ws_url = "ws://localhost:8765"
    origin = "http://localhost"
    token = None  # Add your token if needed

    # Parse host/port from URL
    parsed = urlparse(ws_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    # Step 1: Check TCP port
    if not check_port(host, port):
        logging.error("❌ Aborting WebSocket test due to closed port.")
        return

    # Step 2: Test WebSocket
    try:
        asyncio.run(test_ws_connection(ws_url, origin, token))
    except KeyboardInterrupt:
        logging.info("Test aborted by user.")


if __name__ == "__main__":
    main()
