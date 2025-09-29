# File: streaming_server.py
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

# Enhanced logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)
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
            'conversation_history': [],
            'last_activity': time.time(),
            'greeting_sent': False
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
        
        # Send Sara's greeting immediately
        await self.send_immediate_greeting(ws_id)
    
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
                conn['last_activity'] = time.time()
                
                # Process audio more frequently - lower threshold
                if len(conn['audio_buffer']) >= 8000:  # Reduced to ~0.5 seconds
                    conn['is_processing'] = True
                    asyncio.create_task(self.process_user_speech(ws_id))
                
            except Exception as e:
                log.error("Error processing media: %s", e)
    
    async def send_immediate_greeting(self, ws_id):
        """Send greeting immediately without waiting for audio"""
        conn = self.connections.get(ws_id)
        if not conn or conn['greeting_sent']:
            return
            
        greeting = "Hello! I'm Sara Hayes. How can I help you today?"
        log.info("🎙️ Sending immediate greeting")
        await self.send_ai_response(ws_id, greeting)
        conn['greeting_sent'] = True
    
    async def process_user_speech(self, ws_id):
        """Process user speech and generate AI response"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
            
        try:
            # Save audio to file
            audio_data = bytes(conn['audio_buffer'])
            current_buffer_size = len(conn['audio_buffer'])
            conn['audio_buffer'].clear()  # Clear buffer after processing
            
            log.info("🔊 Processing audio buffer: %d bytes", current_buffer_size)
            
            timestamp = int(time.time())
            raw_path = TMP_DIR / f"user_{ws_id}_{timestamp}.raw"
            wav_path = TMP_DIR / f"user_{ws_id}_{timestamp}.wav"
            
            raw_path.write_bytes(audio_data)
            
            # Convert to WAV for transcription
            await self.convert_audio(raw_path, wav_path, conn['sample_rate'])
            
            # Transcribe using Whisper
            transcript = await self.transcribe_audio(wav_path)
            
            if transcript and len(transcript.strip()) > 3:  # Reduced minimum characters
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
                    # Fallback response
                    await self.send_ai_response(ws_id, "I heard you, but I'm having trouble responding right now.")
            else:
                log.info("No speech detected or transcript too short")
                # If no speech detected, send a prompt
                if not conn.get('prompt_sent'):
                    prompt = "I'm listening. Please tell me how I can help you today."
                    await self.send_ai_response(ws_id, prompt)
                    conn['prompt_sent'] = True
            
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
        
        # ✅ FIXED: Using gpt-5-mini with max_tokens
        payload = {
            "model": "gpt-5-mini",  # ✅ CHANGED TO gpt-5-mini
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 150  # ✅ ADDED max_tokens
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
                        return f"I understand you said: {user_message}. How can I help you with that?"
        except Exception as e:
            log.error("❌ AI response error: %s", e)
            return "I heard what you said, but I'm having trouble processing it right now. Could you try again?"
    
    async def send_ai_response(self, ws_id, text):
        """Convert text to speech and stream to Twilio"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
        
        # Use ElevenLabs if available, otherwise fallback
        if ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID:
            await self.send_elevenlabs_audio(ws_id, text)
        else:
            await self.send_fallback_audio(ws_id, text)
    
    async def send_elevenlabs_audio(self, ws_id, text):
        """Send audio using ElevenLabs TTS"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
            
        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
            headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
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
                async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                    if resp.status == 200:
                        # Get MP3 data
                        mp3_data = await resp.read()
                        
                        # Save temporarily
                        mp3_path = TMP_DIR / f"response_{ws_id}_{int(time.time())}.mp3"
                        raw_path = TMP_DIR / f"response_{ws_id}_{int(time.time())}.raw"
                        
                        with open(mp3_path, "wb") as f:
                            f.write(mp3_data)
                        
                        # Convert to Twilio format
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, self.convert_to_twilio_format, mp3_path, raw_path, conn['sample_rate'])
                        
                        # Stream the audio
                        await self.stream_audio_file(ws_id, raw_path)
                        
                    else:
                        log.error("ElevenLabs TTS failed, using fallback")
                        await self.send_fallback_audio(ws_id, text)
                        
        except Exception as e:
            log.error("ElevenLabs error: %s, using fallback", e)
            await self.send_fallback_audio(ws_id, text)
    
    def convert_to_twilio_format(self, mp3_path, raw_path, sample_rate):
        """Convert MP3 to Twilio raw format"""
        cmd = [
            "ffmpeg", "-y",
            "-i", str(mp3_path),
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            str(raw_path)
        ]
        subprocess.run(cmd, capture_output=True)
    
    async def stream_audio_file(self, ws_id, audio_path):
        """Stream audio file to Twilio"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
            
        try:
            with open(audio_path, "rb") as f:
                chunk_size = 1600
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                        
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
                        await asyncio.sleep(0.1)
                    except Exception as e:
                        log.error("❌ Failed to send audio chunk: %s", e)
                        break
                        
        except Exception as e:
            log.error("Error streaming audio file: %s", e)
    
    async def send_fallback_audio(self, ws_id, text):
        """Send fallback audio (simple tones)"""
        conn = self.connections.get(ws_id)
        if not conn:
            return
        
        # Generate audio based on text
        words = len(text.split())
        duration_ms = min(words * 200, 3000)  # Max 3 seconds
        
        audio_data = self.generate_tone(440, duration_ms)
        
        # Stream the audio
        chunk_size = 1600
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
                await asyncio.sleep(0.1)
            except Exception as e:
                log.error("❌ Failed to send fallback audio: %s", e)
                break
        
        log.info("✅ Sent fallback audio for: '%s'", text[:50] + "..." if len(text) > 50 else text)
    
    def generate_tone(self, frequency, duration_ms, sample_rate=8000):
        """Generate a simple sine wave tone"""
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

# Add diagnostic endpoint
@ app.router.get("/diagnostic")
async def diagnostic_endpoint(request):
    """Test APIs from within Render"""
    results = {}
    
    # Test OpenAI
    try:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
        payload = {
            "model": "gpt-5-mini",
            "messages": [{"role": "user", "content": "Say 'OpenAI test successful'"}],
            "max_tokens": 20
        }
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    results['openai'] = {"status": "PASS", "response": data['choices'][0]['message']['content']}
                else:
                    error = await response.text()
                    results['openai'] = {"status": "FAIL", "error": f"HTTP {response.status}"}
    except Exception as e:
        results['openai'] = {"status": "FAIL", "error": str(e)}
    
    # Test ElevenLabs
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream"
        headers = {"xi-api-key": ELEVENLABS_API_KEY}
        payload = {"text": "Test", "voice_settings": {"stability": 0.5, "similarity_boost": 0.5}}
        
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=10) as response:
                if response.status == 200:
                    audio_data = await response.read()
                    results['elevenlabs'] = {"status": "PASS", "audio_size": len(audio_data)}
                else:
                    error = await response.text()
                    results['elevenlabs'] = {"status": "FAIL", "error": f"HTTP {response.status}"}
    except Exception as e:
        results['elevenlabs'] = {"status": "FAIL", "error": str(e)}
    
    return web.json_response(results)

if __name__ == '__main__':
    log.info("🚀 Starting Sara Streaming Server on port %d", PORT)
    log.info("✅ Media streaming: ACTIVE")
    log.info("🎙️ Speech recognition: READY")
    log.info("🤖 AI conversation: ENABLED (gpt-5-mini)")
    log.info("🔊 TTS: ElevenLabs" if ELEVENLABS_API_KEY else "🔊 TTS: Fallback tones")
    web.run_app(app, host='0.0.0.0', port=PORT)