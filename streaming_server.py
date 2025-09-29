# File: streaming_server.py - WORKING MINIMAL VERSION
import os
import json
import time
import base64
import logging
import asyncio
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sara-streaming")

PORT = int(os.environ.get("PORT", 5001))

class TwilioMediaHandler:
    def __init__(self):
        self.connections = {}
    
    async def handle_websocket(self, request):
        log.info("🔍 WebSocket connection received")
        
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        
        ws_id = f"conn_{int(time.time())}"
        self.connections[ws_id] = {
            'ws': ws,
            'stream_sid': None,
            'chunk_counter': 0,
            'audio_buffer': bytearray(),
            'last_audio_time': time.time()
        }
        
        log.info("🎉 WebSocket connected: %s", ws_id)
        
        try:
            # 1. Send connected event
            connected_msg = {
                "event": "connected",
                "protocol": "Call",
                "version": "1.0.0"
            }
            await ws.send_str(json.dumps(connected_msg))
            log.info("✅ Sent 'connected' event")
            
            # 2. Process incoming messages
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        event = data.get('event')
                        
                        if event == 'start':
                            await self.handle_start_event(ws_id, data)
                        elif event == 'media':
                            await self.handle_media_event(ws_id, data)
                        elif event == 'stop':
                            log.info("⏹️ Stop event received")
                            break
                            
                    except Exception as e:
                        log.error("Error processing message: %s", e)
                        
        except Exception as e:
            log.error("WebSocket error: %s", e)
        finally:
            log.info("🔚 Closing connection: %s", ws_id)
            self.connections.pop(ws_id, None)
            
        return ws
    
    async def handle_start_event(self, ws_id, data):
        """Handle stream start event"""
        start_data = data.get('start', {})
        stream_sid = start_data.get('streamSid')
        call_sid = start_data.get('callSid')
        sample_rate = start_data.get('sampleRate', 8000)
        
        if not stream_sid:
            log.error("❌ No streamSid in start event")
            return
            
        self.connections[ws_id]['stream_sid'] = stream_sid
        self.connections[ws_id]['call_sid'] = call_sid
        self.connections[ws_id]['sample_rate'] = sample_rate
        
        log.info("🎬 Stream started - stream_sid: %s", stream_sid)
        
        # Send initial greeting audio
        await self.send_greeting_audio(ws_id)
    
    async def handle_media_event(self, ws_id, data):
        """Handle incoming media from Twilio"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
            
        media_data = data.get('media', {})
        payload = media_data.get('payload')
        
        if payload:
            try:
                # Decode the audio
                audio_chunk = base64.b64decode(payload)
                conn['audio_buffer'].extend(audio_chunk)
                conn['last_audio_time'] = time.time()
                
                # When we have enough audio, send a response
                if len(conn['audio_buffer']) >= 8000:  # ~0.5 seconds of audio
                    await self.send_response_audio(ws_id)
                    conn['audio_buffer'].clear()
                
            except Exception as e:
                log.error("Error processing media: %s", e)
    
    async def send_greeting_audio(self, ws_id):
        """Send a simple greeting message"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
        
        # Generate a simple tone as greeting (instead of TTS for now)
        greeting_audio = self.generate_tone(440, 1000)  # 440Hz tone for 1 second
        
        log.info("🔊 SENDING GREETING AUDIO - %d bytes", len(greeting_audio))
        await self.send_audio_chunk(ws_id, greeting_audio)
    
    async def send_response_audio(self, ws_id):
        """Send response when user speaks"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
        
        # Generate a different tone as response
        response_audio = self.generate_tone(550, 1500)  # 550Hz tone for 1.5 seconds
        
        log.info("🔊 SENDING RESPONSE AUDIO - %d bytes", len(response_audio))
        await self.send_audio_chunk(ws_id, response_audio)
    
    def generate_tone(self, frequency, duration_ms, sample_rate=8000):
        """Generate a simple sine wave tone"""
        import math
        samples = int(sample_rate * duration_ms / 1000)
        audio_data = bytearray()
        
        for i in range(samples):
            # Generate sine wave (16-bit signed)
            sample = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
            # Convert to 16-bit little endian
            audio_data.extend([sample & 0xFF, (sample >> 8) & 0xFF])
        
        return bytes(audio_data)
    
    async def send_audio_chunk(self, ws_id, audio_data):
        """Send audio chunk to Twilio"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
        
        # Split audio into smaller chunks for streaming
        chunk_size = 1600  # 100ms chunks
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i + chunk_size]
            if len(chunk) < chunk_size:
                # Pad with silence if needed
                chunk += b'\x00' * (chunk_size - len(chunk))
            
            media_msg = {
                "event": "media",
                "streamSid": conn['stream_sid'],
                "media": {
                    "track": "outbound",
                    "chunk": str(conn['chunk_counter']),
                    "timestamp": str(int(time.time() * 1000)),
                    "payload": base64.b64encode(chunk).decode('ascii')
                }
            }
            
            try:
                await conn['ws'].send_str(json.dumps(media_msg))
                conn['chunk_counter'] += 1
                log.info("✅ Sent media chunk %d - %d bytes", conn['chunk_counter'], len(chunk))
                await asyncio.sleep(0.1)  # 100ms between chunks
            except Exception as e:
                log.error("❌ Failed to send media chunk: %s", e)
                break

# Setup application
handler = TwilioMediaHandler()
app = web.Application()
app.router.add_get('/ws', handler.handle_websocket)
app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "connections": len(handler.connections)}))

if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %d", PORT)
    log.info("✅ Media streaming: ACTIVE")
    web.run_app(app, host='0.0.0.0', port=PORT)