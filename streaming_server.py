# File: streaming_server.py - MINIMAL PROTOCOL VERSION
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
            'chunk_counter': 0
        }
        
        log.info("🎉 WebSocket connected: %s", ws_id)
        
        try:
            # 1. Send connected event (NO streamSid)
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
                            
                    except json.JSONDecodeError:
                        log.error("❌ Invalid JSON received")
                    except Exception as e:
                        log.error("❌ Error processing message: %s", e)
                        
        except Exception as e:
            log.error("❌ WebSocket error: %s", e)
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
        
        log.info("🎬 Stream started - stream_sid: %s, call_sid: %s, sample_rate: %s", 
                stream_sid, call_sid, sample_rate)
        
        # Send initial silence to establish media stream
        await self.send_silence(ws_id)
    
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
                log.info("🎵 Received audio chunk: %d bytes", len(audio_chunk))
                
                # Echo back the same audio to test bidirectional streaming
                await self.send_media_chunk(ws_id, audio_chunk)
                
            except Exception as e:
                log.error("❌ Error processing media: %s", e)
    
    async def send_silence(self, ws_id):
        """Send 100ms of silence to establish media stream"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
            
        # Generate 100ms of silence (8000 Hz, 16-bit mono)
        silence = b'\x00' * 1600  # 8000 samples/sec * 2 bytes/sample * 0.1 sec
        
        media_msg = {
            "event": "media",
            "streamSid": conn['stream_sid'],
            "media": {
                "track": "outbound",
                "chunk": str(conn['chunk_counter']),
                "timestamp": str(int(time.time() * 1000)),
                "payload": base64.b64encode(silence).decode('ascii')
            }
        }
        
        try:
            await conn['ws'].send_str(json.dumps(media_msg))
            conn['chunk_counter'] += 1
            log.info("🔊 Sent initial silence media message")
        except Exception as e:
            log.error("❌ Failed to send silence: %s", e)
    
    async def send_media_chunk(self, ws_id, audio_data):
        """Send audio chunk back to Twilio"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid']:
            return
            
        media_msg = {
            "event": "media",
            "streamSid": conn['stream_sid'],
            "media": {
                "track": "outbound",
                "chunk": str(conn['chunk_counter']),
                "timestamp": str(int(time.time() * 1000)),
                "payload": base64.b64encode(audio_data).decode('ascii')
            }
        }
        
        try:
            await conn['ws'].send_str(json.dumps(media_msg))
            conn['chunk_counter'] += 1
        except Exception as e:
            log.error("❌ Failed to send media chunk: %s", e)

# Setup application
handler = TwilioMediaHandler()
app = web.Application()
app.router.add_get('/ws', handler.handle_websocket)
app.router.add_get('/health', lambda r: web.json_response({"status": "ok"}))

if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %d", PORT)
    log.info("✅ Using Twilio Media Streams protocol")
    web.run_app(app, host='0.0.0.0', port=PORT)