# File: streaming_server.py
"""
TWILIO MEDIA STREAMS - PROTOCOL COMPLIANT VERSION
Exact implementation following Twilio's Media Streams specification
"""
import os
import json
import time
import base64
import logging
import asyncio
import aiofiles
import pathlib
import subprocess
import shutil
from datetime import datetime, timedelta
from aiohttp import web, ClientSession, WSMsgType
import aiohttp

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sara-streaming")

# --- Configuration ---
REQUIRED_ENV = ["OPENAI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "PUBLIC_STREAMING_URL"]
missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing:
    raise SystemExit(f"Missing env vars: {missing}")

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ELEVEN_API_KEY = os.environ["ELEVENLABS_API_KEY"]
ELEVEN_VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]
PUBLIC_STREAMING_URL = os.environ["PUBLIC_STREAMING_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", 5001))

# --- Directory setup ---
DATA_DIR = pathlib.Path("data")
STATIC_DIR = pathlib.Path("static")
TMP_DIR = pathlib.Path("tmp")
for d in [DATA_DIR, STATIC_DIR, TMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- Twilio Media Streams Protocol Implementation ---
class TwilioMediaStreams:
    """Exact implementation of Twilio Media Streams protocol"""
    
    @staticmethod
    def create_connected_event():
        """Create the initial connected event - NO streamSid here"""
        return {
            "event": "connected",
            "protocol": "Call",
            "version": "1.0.0"
        }
    
    @staticmethod
    def create_media_event(stream_sid, payload_b64, chunk_number, timestamp_ms=None):
        """
        Create EXACT media event structure that Twilio expects
        According to: https://www.twilio.com/docs/voice/twiml/stream#media-messages
        """
        if timestamp_ms is None:
            timestamp_ms = str(int(time.time() * 1000))
            
        return {
            "event": "media",
            "streamSid": stream_sid,  # REQUIRED for all media events
            "media": {
                "track": "outbound",  # REQUIRED - must be "inbound" or "outbound"
                "chunk": str(chunk_number),  # REQUIRED - sequential chunk counter
                "timestamp": timestamp_ms,  # REQUIRED - milliseconds
                "payload": payload_b64  # REQUIRED - base64 encoded audio
            }
        }
    
    @staticmethod
    def create_mark_event(stream_sid, name):
        """Create mark event for synchronization"""
        return {
            "event": "mark", 
            "streamSid": stream_sid,
            "mark": {
                "name": name
            }
        }

# --- Audio Processing Utilities ---
def ensure_ffmpeg():
    """Verify ffmpeg is available and working"""
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not found in PATH")
    
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
        log.info("✅ ffmpeg: %s", result.stdout.split('\n')[0])
    except Exception as e:
        raise SystemExit(f"ffmpeg test failed: {e}")

def convert_audio(input_path, output_path, input_format, output_format, sample_rate=8000):
    """Generic audio conversion using ffmpeg"""
    if input_format == "mp3" and output_format == "s16le":
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            output_path
        ]
    elif input_format == "s16le" and output_format == "wav":
        cmd = [
            "ffmpeg", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            "-i", input_path,
            "-ar", "16000", "-ac", "1",
            output_path
        ]
    else:
        raise ValueError(f"Unsupported conversion: {input_format} to {output_format}")
    
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[:500]}")
    return output_path

def generate_silence(duration_ms=100, sample_rate=8000):
    """Generate silent audio for initial stream establishment"""
    samples = int((duration_ms / 1000.0) * sample_rate * 2)  # *2 for 16-bit
    return b"\x00" * samples

# --- AI Services ---
async def transcribe_audio(wav_path):
    """Transcribe audio using OpenAI Whisper"""
    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    
    form = aiohttp.FormData()
    with open(wav_path, "rb") as f:
        form.add_field("file", f, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", "whisper-1")
        
        async with ClientSession() as session:
            async with session.post(url, headers=headers, data=form, timeout=30) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    return result.get("text", "").strip()
                else:
                    log.error("Transcription failed: %s", await resp.text())
                    return None

async def get_llm_response(transcript, system_prompt):
    """Get response from LLM"""
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript}
        ],
        "temperature": 0.7,
        "max_tokens": 150
    }
    
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
            if resp.status == 200:
                result = await resp.json()
                return result["choices"][0]["message"]["content"].strip()
            else:
                log.error("LLM failed: %s", await resp.text())
                return None

async def text_to_speech(text, output_path):
    """Convert text to speech using ElevenLabs"""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVEN_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json"
    }
    
    payload = {
        "text": text,
        "voice_settings": {
            "stability": 0.6,
            "similarity_boost": 0.7
        }
    }
    
    async with ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
            if resp.status == 200:
                audio_data = await resp.read()
                async with aiofiles.open(output_path, "wb") as f:
                    await f.write(audio_data)
                return output_path
            else:
                log.error("TTS failed: %s", await resp.text())
                return None

# --- Connection Management ---
class ConnectionManager:
    """Manage WebSocket connections and audio processing"""
    
    def __init__(self):
        self.connections = {}
        
    def add_connection(self, ws_id, websocket):
        """Add new WebSocket connection"""
        self.connections[ws_id] = {
            "ws": websocket,
            "stream_sid": None,
            "call_sid": None,
            "sample_rate": 8000,
            "audio_buffer": bytearray(),
            "is_processing": False,
            "chunk_counter": 0,
            "lock": asyncio.Lock()
        }
        return self.connections[ws_id]
    
    def remove_connection(self, ws_id):
        """Remove WebSocket connection"""
        return self.connections.pop(ws_id, None)
    
    def get_connection(self, ws_id):
        """Get connection by ID"""
        return self.connections.get(ws_id)

connection_manager = ConnectionManager()

async def process_audio_and_respond(ws_id):
    """Process audio buffer and generate response"""
    conn = connection_manager.get_connection(ws_id)
    if not conn or conn["is_processing"]:
        return
        
    async with conn["lock"]:
        conn["is_processing"] = True
        try:
            # Get audio buffer
            audio_buffer = bytes(conn["audio_buffer"])
            conn["audio_buffer"].clear()
            
            if len(audio_buffer) < 8000:  # Less than 0.5 seconds
                return
                
            # Save raw audio
            timestamp = int(time.time())
            raw_path = TMP_DIR / f"input_{ws_id}_{timestamp}.raw"
            wav_path = TMP_DIR / f"input_{ws_id}_{timestamp}.wav"
            
            raw_path.write_bytes(audio_buffer)
            
            # Convert to WAV for transcription
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, convert_audio, 
                str(raw_path), str(wav_path), "s16le", "wav", conn["sample_rate"]
            )
            
            # Transcribe
            transcript = await transcribe_audio(str(wav_path))
            if not transcript or len(transcript.strip().split()) < 2:
                log.info("No meaningful transcript detected")
                return
                
            log.info("Transcript: %s", transcript[:200])
            
            # Get LLM response (using simple prompt for now)
            system_prompt = "You are Sara, a friendly AI assistant. Keep responses brief and conversational."
            response = await get_llm_response(transcript, system_prompt)
            if not response:
                return
                
            log.info("Response: %s", response[:200])
            
            # Convert to speech
            mp3_path = TMP_DIR / f"response_{ws_id}_{timestamp}.mp3"
            raw_response_path = TMP_DIR / f"response_{ws_id}_{timestamp}.raw"
            
            if await text_to_speech(response, str(mp3_path)):
                # Convert to Twilio format
                await loop.run_in_executor(
                    None, convert_audio,
                    str(mp3_path), str(raw_response_path), "mp3", "s16le", conn["sample_rate"]
                )
                
                # Stream response audio
                await stream_audio_to_twilio(ws_id, str(raw_response_path))
                
        except Exception as e:
            log.error("Error processing audio: %s", e)
        finally:
            conn["is_processing"] = False

async def stream_audio_to_twilio(ws_id, audio_path):
    """Stream audio to Twilio with PROTOCOL COMPLIANCE"""
    conn = connection_manager.get_connection(ws_id)
    if not conn or not conn["stream_sid"]:
        return
        
    try:
        chunk_size = 3200  # 200ms of 8kHz 16-bit audio
        chunk_number = conn["chunk_counter"]
        
        with open(audio_path, "rb") as audio_file:
            while True:
                audio_chunk = audio_file.read(chunk_size)
                if not audio_chunk:
                    break
                    
                # Pad with silence if chunk is too small
                if len(audio_chunk) < chunk_size:
                    audio_chunk += b"\x00" * (chunk_size - len(audio_chunk))
                
                # Create protocol-compliant media message
                payload_b64 = base64.b64encode(audio_chunk).decode('ascii')
                media_message = TwilioMediaStreams.create_media_event(
                    stream_sid=conn["stream_sid"],
                    payload_b64=payload_b64,
                    chunk_number=chunk_number
                )
                
                # Send the message
                try:
                    await conn["ws"].send_str(json.dumps(media_message))
                    chunk_number += 1
                    await asyncio.sleep(0.02)  # ~50ms between chunks
                except Exception as e:
                    log.error("Failed to send audio chunk: %s", e)
                    break
                    
        conn["chunk_counter"] = chunk_number
        
    except Exception as e:
        log.error("Error streaming audio: %s", e)

# --- WebSocket Handler ---
async def websocket_handler(request):
    """Handle Twilio Media Streams WebSocket connection"""
    log.info("🔍 New WebSocket connection attempt")
    
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    ws_id = f"{int(time.time() * 1000)}_{id(ws)}"
    conn = connection_manager.add_connection(ws_id, ws)
    
    log.info("🎉 WebSocket connected: %s", ws_id)
    
    try:
        # ✅ Send initial connected event (NO streamSid)
        connected_msg = TwilioMediaStreams.create_connected_event()
        await ws.send_str(json.dumps(connected_msg))
        log.info("📞 Sent 'connected' event")
        
        # Process messages from Twilio
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                try:
                    data = json.loads(message.data)
                    event_type = data.get("event")
                    
                    if event_type == "start":
                        # ✅ Extract stream details
                        start_info = data.get("start", {})
                        stream_sid = start_info.get("streamSid")
                        call_sid = start_info.get("callSid")
                        sample_rate = int(start_info.get("sampleRate", 8000))
                        
                        if not stream_sid:
                            log.error("❌ No streamSid in start event")
                            continue
                            
                        conn["stream_sid"] = stream_sid
                        conn["call_sid"] = call_sid
                        conn["sample_rate"] = sample_rate
                        
                        log.info("🎬 Stream started: %s, call: %s, rate: %dHz", 
                                stream_sid, call_sid, sample_rate)
                        
                        # ✅ Send initial silence to establish media stream
                        silence = generate_silence(100, sample_rate)
                        silence_b64 = base64.b64encode(silence).decode('ascii')
                        
                        initial_media = TwilioMediaStreams.create_media_event(
                            stream_sid=stream_sid,
                            payload_b64=silence_b64,
                            chunk_number=0,
                            timestamp_ms="0"
                        )
                        
                        await ws.send_str(json.dumps(initial_media))
                        log.info("🔊 Sent initial media with streamSid")
                        
                    elif event_type == "media":
                        # ✅ Handle incoming audio from Twilio
                        if not conn["stream_sid"]:
                            continue
                            
                        media_info = data.get("media", {})
                        payload = media_info.get("payload")
                        
                        if payload:
                            try:
                                audio_data = base64.b64decode(payload)
                                conn["audio_buffer"].extend(audio_data)
                                
                                # Process when we have enough audio
                                if len(conn["audio_buffer"]) >= 16000:  # ~1 second
                                    asyncio.create_task(process_audio_and_respond(ws_id))
                                    
                            except Exception as e:
                                log.error("Error decoding media: %s", e)
                                
                    elif event_type == "stop":
                        log.info("⏹️ Stream stopped: %s", ws_id)
                        break
                        
                except json.JSONDecodeError:
                    log.error("❌ Invalid JSON received")
                except Exception as e:
                    log.error("❌ Error processing message: %s", e)
                    
    except Exception as e:
        log.error("❌ WebSocket error: %s", e)
    finally:
        log.info("🔚 Closing connection: %s", ws_id)
        connection_manager.remove_connection(ws_id)
        
    return ws

# --- Web Application ---
routes = web.RouteTableDef()

@routes.get("/health")
async def health_check(request):
    return web.json_response({
        "status": "ok", 
        "timestamp": datetime.utcnow().isoformat(),
        "connections": len(connection_manager.connections)
    })

@routes.get("/")
async def root_handler(request):
    return web.json_response({
        "service": "Sara AI Voice Bot",
        "status": "running",
        "protocol": "Twilio Media Streams"
    })

@routes.get("/ws")
async def websocket_route(request):
    return await websocket_handler(request)

async def cleanup_task():
    """Clean up temporary files periodically"""
    while True:
        try:
            cutoff = datetime.utcnow() - timedelta(hours=1)
            for temp_file in TMP_DIR.glob("*"):
                if temp_file.stat().st_mtime < cutoff.timestamp():
                    temp_file.unlink(missing_ok=True)
        except Exception as e:
            log.error("Cleanup error: %s", e)
        await asyncio.sleep(3600)  # Run every hour

def create_app():
    """Create and configure the web application"""
    ensure_ffmpeg()
    app = web.Application()
    app.add_routes(routes)
    return app

async def start_server():
    """Start the streaming server"""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    log.info("🚀 Sara Streaming Server started on port %d", PORT)
    log.info("✅ Twilio Media Streams protocol: ACTIVE")
    log.info("✅ FFmpeg: READY")
    log.info("✅ WebSocket endpoint: /ws")
    
    # Start cleanup task
    asyncio.create_task(cleanup_task())
    
    # Keep server running
    await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        log.info("Server stopped by user")
    except Exception as e:
        log.error("Server failed to start: %s", e)
        raise