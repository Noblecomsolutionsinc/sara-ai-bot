# File: streaming_server.py - CONVERSATIONAL VERSION
import os
import json
import time
import base64
import logging
import asyncio
import aiohttp
from aiohttp import web, WSMsgType, ClientSession
import subprocess
import pathlib

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sara-streaming")

PORT = int(os.environ.get("PORT", 5001))

# Required APIs
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY") 
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID")

# Directories
TMP_DIR = pathlib.Path("tmp")
TMP_DIR.mkdir(exist_ok=True)

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
            'is_processing': False,
            'conversation_history': []
        }
        
        log.info("🎉 WebSocket connected: %s", ws_id)
        
        try:
            # Send connected event
            connected_msg = {
                "event": "connected",
                "protocol": "Call",
                "version": "1.0.0"
            }
            await ws.send_str(json.dumps(connected_msg))
            log.info("✅ Sent 'connected' event")
            
            # Process incoming messages
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
        
        # Send Sara's greeting
        await self.send_ai_response(ws_id, "Hello! I'm Sara Hayes. How can I help you today?")
    
    async def handle_media_event(self, ws_id, data):
        """Handle incoming media from Twilio"""
        conn = self.connections.get(ws_id)
        if not conn or not conn['stream_sid'] or conn['is_processing']:
            return
            
        media_data = data.get('media', {})
        payload = media_data.get('payload')
        
        if payload:
            try:
                # Decode the audio
                audio_chunk = base64.b64decode(payload)
                conn['audio_buffer'].extend(audio_chunk)
                
                # When we have enough audio (about 2 seconds), process it
                if len(conn['audio_buffer']) >= 32000:  # ~2 seconds of audio
                    conn['is_processing'] = True
                    asyncio.create_task(self.process_user_speech(ws_id))
                
            except Exception as e:
                log.error("Error processing media: %s", e)
    
    async def process_user_speech(self, ws_id):
        """Process user speech and generate AI response"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
            
        try:
            # Save audio to file
            audio_data = bytes(conn['audio_buffer'])
            conn['audio_buffer'].clear()  # Clear buffer after processing
            
            timestamp = int(time.time())
            raw_path = TMP_DIR / f"user_{ws_id}_{timestamp}.raw"
            wav_path = TMP_DIR / f"user_{ws_id}_{timestamp}.wav"
            
            raw_path.write_bytes(audio_data)
            
            # Convert to WAV for transcription
            await self.convert_audio(raw_path, wav_path, conn['sample_rate'])
            
            # Transcribe using Whisper
            transcript = await self.transcribe_audio(wav_path)
            
            if transcript and len(transcript.strip()) > 5:  # Minimum 5 characters
                log.info("🎙️ User said: %s", transcript)
                
                # Get AI response
                response = await self.get_ai_response(transcript, conn['conversation_history'])
                if response:
                    log.info("🤖 Sara responds: %s", response)
                    
                    # Add to conversation history
                    conn['conversation_history'].append({"role": "user", "content": transcript})
                    conn['conversation_history'].append({"role": "assistant", "content": response})
                    
                    # Keep only last 6 messages to manage context
                    if len(conn['conversation_history']) > 6:
                        conn['conversation_history'] = conn['conversation_history'][-6:]
                    
                    # Convert to speech and stream
                    await self.send_ai_response(ws_id, response)
                else:
                    log.warning("No AI response generated")
            else:
                log.info("No speech detected or transcript too short")
            
        except Exception as e:
            log.error("❌ Error in speech processing: %s", e)
        finally:
            conn['is_processing'] = False
    
    async def convert_audio(self, input_path, output_path, sample_rate):
        """Convert raw audio to WAV"""
        loop = asyncio.get_running_loop()
        cmd = [
            "ffmpeg", "-y",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            "-i", str(input_path),
            "-ar", "16000", "-ac", "1",
            str(output_path)
        ]
        process = await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True))
        if process.returncode != 0:
            log.error("FFmpeg conversion failed: %s", process.stderr.decode())
    
    async def transcribe_audio(self, wav_path):
        """Transcribe audio using Whisper"""
        if not OPENAI_API_KEY:
            log.warning("No OpenAI API key - using test transcription")
            return "This is a test transcription. Please configure your OpenAI API key."
            
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        
        try:
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
                            error_text = await resp.text()
                            log.error("❌ Transcription failed: %s", error_text)
                            return None
        except Exception as e:
            log.error("❌ Transcription error: %s", e)
            return None
    
    async def get_ai_response(self, user_message, conversation_history):
        """Get response from AI"""
        if not OPENAI_API_KEY:
            return "I heard you speak! This is Sara Hayes. To enable full AI conversations, please configure your OpenAI API key."
        
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Build conversation context
        messages = [
            {
                "role": "system", 
                "content": """You are Sara Hayes, a friendly and professional AI assistant. 
                Keep your responses conversational, brief (1-2 sentences), and natural for voice conversation.
                Speak like you're on a phone call - be warm and engaging."""
            }
        ]
        
        # Add conversation history
        messages.extend(conversation_history)
        
        # Add current user message
        messages.append({"role": "user", "content": user_message})
        
        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 150
        }
        
        try:
            async with ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result["choices"][0]["message"]["content"].strip()
                    else:
                        error_text = await resp.text()
                        log.error("❌ AI response failed: %s", error_text)
                        return f"I understand you said: {user_message}. Please configure your AI API keys for full functionality."
        except Exception as e:
            log.error("❌ AI response error: %s", e)
            return "I heard what you said, but I'm having trouble processing it right now. Could you try again?"
    
    async def send_ai_response(self, ws_id, text):
        """Convert text to speech and stream to Twilio"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
        
        # For now, use fallback audio - you can implement ElevenLabs TTS here
        await self.send_fallback_audio(ws_id, text)
    
    async def send_fallback_audio(self, ws_id, text):
        """Send fallback audio (you can replace with ElevenLabs TTS)"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
        
        # Generate audio based on text length
        duration_ms = min(len(text) * 100, 5000)  # Max 5 seconds
        tone_frequency = 440 if "hello" in text.lower() else 550
        
        audio_data = self.generate_tone(tone_frequency, duration_ms)
        
        # Stream the audio
        chunk_size = 1600  # 100ms chunks
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i + chunk_size]
            if len(chunk) < chunk_size:
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
                await asyncio.sleep(0.1)  # 100ms between chunks
            except Exception as e:
                log.error("❌ Failed to send audio: %s", e)
                break
        
        log.info("✅ Sent AI response audio: '%s'", text[:50] + "..." if len(text) > 50 else text)
    
    def generate_tone(self, frequency, duration_ms, sample_rate=8000):
        """Generate a simple sine wave tone (fallback)"""
        import math
        samples = int(sample_rate * duration_ms / 1000)
        audio_data = bytearray()
        
        for i in range(samples):
            sample = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
            audio_data.extend([sample & 0xFF, (sample >> 8) & 0xFF])
        
        return bytes(audio_data)

# Setup application
handler = TwilioMediaHandler()
app = web.Application()
app.router.add_get('/ws', handler.handle_websocket)
app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "connections": len(handler.connections)}))

if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %d", PORT)
    log.info("✅ Media streaming: ACTIVE")
    log.info("🎙️ Speech recognition: READY")
    log.info("🤖 AI conversation: ENABLED")
    web.run_app(app, host='0.0.0.0', port=PORT)