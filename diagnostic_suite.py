# File: diagnostic_suite.py
"""
COMPREHENSIVE DIAGNOSTIC SUITE FOR SARA AI VOICE BOT
Tests all components systematically with actual API calls
"""
import os
import sys
import json
import time
import asyncio
import aiohttp
import subprocess
import requests
from datetime import datetime

class SaraDiagnostic:
    def __init__(self):
        self.results = {}
        self.test_start_time = datetime.now()
    
    def log_result(self, test_name, status, details=""):
        """Log test results"""
        self.results[test_name] = {
            "status": status,
            "details": details,
            "timestamp": datetime.now().isoformat()
        }
        status_icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
        print(f"{status_icon} {test_name}: {status}")
        if details:
            print(f"   Details: {details}")
        print()
    
    async def run_all_tests(self):
        """Run complete diagnostic suite"""
        print("🚀 SARA AI VOICE BOT - COMPREHENSIVE DIAGNOSTIC")
        print("=" * 60)
        
        # Environment Tests
        await self.test_environment_variables()
        await self.test_python_dependencies()
        
        # API Tests
        await self.test_openai_api()
        await self.test_openai_chat_completion()
        await self.test_openai_whisper_transcription()
        await self.test_elevenlabs_api()
        await self.test_elevenlabs_tts()
        await self.test_twilio_credentials()
        
        # Server Tests
        await self.test_flask_app()
        await self.test_streaming_server_health()
        await self.test_websocket_endpoint()
        
        # Network Tests
        await self.test_render_urls()
        
        # Protocol Tests
        await self.test_twiml_structure()
        
        # System Tests
        await self.test_ffmpeg()
        
        self.generate_report()
    
    async def test_environment_variables(self):
        """Test required environment variables"""
        required_vars = {
            "TWILIO_ACCOUNT_SID": "Twilio Account SID",
            "TWILIO_AUTH_TOKEN": "Twilio Auth Token", 
            "TWILIO_PHONE_NUMBER": "Twilio Phone Number",
            "OPENAI_API_KEY": "OpenAI API Key",
            "ELEVENLABS_API_KEY": "ElevenLabs API Key",
            "ELEVENLABS_VOICE_ID": "ElevenLabs Voice ID",
            "SERVER_URL": "Flask Server URL",
            "PUBLIC_STREAMING_URL": "Streaming Server URL"
        }
        
        missing = []
        for var, description in required_vars.items():
            if not os.environ.get(var):
                missing.append(f"{var} ({description})")
            else:
                # Mask sensitive values in logs
                value = os.environ[var]
                masked_value = value[:4] + "***" + value[-4:] if len(value) > 8 else "***"
                self.log_result(f"ENV: {var}", "PASS", f"Set: {masked_value}")
        
        if missing:
            self.log_result("Environment Variables", "FAIL", f"Missing: {', '.join(missing)}")
        else:
            self.log_result("Environment Variables", "PASS", "All required variables present")
    
    async def test_python_dependencies(self):
        """Test Python dependencies"""
        dependencies = [
            "flask", "twilio", "aiohttp", "requests"
        ]
        
        missing = []
        for dep in dependencies:
            try:
                __import__(dep)
                self.log_result(f"PYTHON: {dep}", "PASS")
            except ImportError:
                missing.append(dep)
                self.log_result(f"PYTHON: {dep}", "FAIL")
        
        if missing:
            self.log_result("Python Dependencies", "FAIL", f"Missing: {', '.join(missing)}")
        else:
            self.log_result("Python Dependencies", "PASS", "All dependencies available")
    
    async def test_openai_api(self):
        """Test OpenAI API connectivity"""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self.log_result("OpenAI API", "FAIL", "API key not set")
            return
        
        try:
            url = "https://api.openai.com/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        model_count = len(data.get('data', []))
                        self.log_result("OpenAI API", "PASS", f"API accessible - {model_count} models available")
                    else:
                        error = await response.text()
                        self.log_result("OpenAI API", "FAIL", f"HTTP {response.status}: {error[:100]}")
        except Exception as e:
            self.log_result("OpenAI API", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_openai_chat_completion(self):
        """Test OpenAI Chat Completion with actual message"""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self.log_result("OpenAI Chat Completion", "FAIL", "API key not set")
            return
        
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "You are a test assistant. Respond with exactly: 'OpenAI test successful'"},
                    {"role": "user", "content": "Say the test phrase."}
                ],
                "max_tokens": 20,
                "temperature": 0.1
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=15) as response:
                    if response.status == 200:
                        data = await response.json()
                        response_text = data['choices'][0]['message']['content'].strip()
                        if "OpenAI test successful" in response_text:
                            self.log_result("OpenAI Chat Completion", "PASS", f"Response: {response_text}")
                        else:
                            self.log_result("OpenAI Chat Completion", "FAIL", f"Unexpected response: {response_text}")
                    else:
                        error = await response.text()
                        self.log_result("OpenAI Chat Completion", "FAIL", f"HTTP {response.status}: {error[:200]}")
        except Exception as e:
            self.log_result("OpenAI Chat Completion", "FAIL", f"Request failed: {str(e)}")
    
    async def test_openai_whisper_transcription(self):
        """Test OpenAI Whisper transcription with test audio"""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            self.log_result("OpenAI Whisper", "FAIL", "API key not set")
            return
        
        try:
            # Create a simple test audio file (1 second of silence)
            import wave
            import io
            
            # Generate 1 second of silence at 16kHz
            sample_rate = 16000
            frames = sample_rate * 1  # 1 second
            silence_data = b'\x00' * (frames * 2)  # 16-bit = 2 bytes per sample
            
            # Create WAV file in memory
            wav_buffer = io.BytesIO()
            with wave.open(wav_buffer, 'wb') as wav_file:
                wav_file.setnchannels(1)  # Mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(silence_data)
            
            wav_buffer.seek(0)
            
            url = "https://api.openai.com/v1/audio/transcriptions"
            headers = {"Authorization": f"Bearer {api_key}"}
            
            form_data = aiohttp.FormData()
            form_data.add_field('file', wav_buffer, filename='test_silence.wav', content_type='audio/wav')
            form_data.add_field('model', 'whisper-1')
            form_data.add_field('language', 'en')
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, data=form_data, timeout=30) as response:
                    if response.status == 200:
                        data = await response.json()
                        transcription = data.get('text', '').strip()
                        # Whisper should return empty or very short text for silence
                        self.log_result("OpenAI Whisper", "PASS", f"Transcription successful: '{transcription}'")
                    else:
                        error = await response.text()
                        self.log_result("OpenAI Whisper", "FAIL", f"HTTP {response.status}: {error[:200]}")
        except Exception as e:
            self.log_result("OpenAI Whisper", "FAIL", f"Transcription failed: {str(e)}")
    
    async def test_elevenlabs_api(self):
        """Test ElevenLabs API connectivity"""
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
        
        if not api_key or not voice_id:
            self.log_result("ElevenLabs API", "FAIL", "API key or Voice ID not set")
            return
        
        try:
            url = f"https://api.elevenlabs.io/v1/voices/{voice_id}"
            headers = {"xi-api-key": api_key}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        voice_name = data.get('name', 'Unknown')
                        self.log_result("ElevenLabs API", "PASS", f"Voice '{voice_name}' accessible")
                    else:
                        error = await response.text()
                        self.log_result("ElevenLabs API", "FAIL", f"HTTP {response.status}: {error[:100]}")
        except Exception as e:
            self.log_result("ElevenLabs API", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_elevenlabs_tts(self):
        """Test ElevenLabs Text-to-Speech with actual synthesis"""
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        voice_id = os.environ.get("ELEVENLABS_VOICE_ID")
        
        if not api_key or not voice_id:
            self.log_result("ElevenLabs TTS", "FAIL", "API key or Voice ID not set")
            return
        
        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
            headers = {
                "xi-api-key": api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json"
            }
            
            payload = {
                "text": "This is a test of ElevenLabs text to speech synthesis.",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.5
                }
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=30) as response:
                    if response.status == 200:
                        audio_data = await response.read()
                        audio_size = len(audio_data)
                        if audio_size > 1000:  # Reasonable audio file size
                            self.log_result("ElevenLabs TTS", "PASS", f"Audio generated: {audio_size} bytes")
                        else:
                            self.log_result("ElevenLabs TTS", "FAIL", f"Audio too small: {audio_size} bytes")
                    else:
                        error = await response.text()
                        self.log_result("ElevenLabs TTS", "FAIL", f"HTTP {response.status}: {error[:200]}")
        except Exception as e:
            self.log_result("ElevenLabs TTS", "FAIL", f"TTS failed: {str(e)}")
    
    async def test_twilio_credentials(self):
        """Test Twilio credentials"""
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        
        if not account_sid or not auth_token:
            self.log_result("Twilio Credentials", "FAIL", "Account SID or Auth Token missing")
            return
        
        try:
            from twilio.rest import Client
            client = Client(account_sid, auth_token)
            # Test by fetching account details
            account = client.api.accounts(account_sid).fetch()
            self.log_result("Twilio Credentials", "PASS", f"Account '{account.friendly_name}' valid")
        except Exception as e:
            self.log_result("Twilio Credentials", "FAIL", f"Authentication failed: {str(e)}")
    
    async def test_flask_app(self):
        """Test Flask app health endpoint"""
        server_url = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com")
        
        try:
            response = requests.get(f"{server_url}/health", timeout=10)
            if response.status_code == 200:
                data = response.json()
                self.log_result("Flask App", "PASS", f"Health endpoint: {data}")
            else:
                self.log_result("Flask App", "FAIL", f"HTTP {response.status_code}")
        except Exception as e:
            self.log_result("Flask App", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_streaming_server_health(self):
        """Test streaming server health endpoint"""
        streaming_url = os.environ.get("PUBLIC_STREAMING_URL", "https://sara-ai-streaming.onrender.com")
        
        try:
            response = requests.get(f"{streaming_url}/health", timeout=10)
            if response.status_code == 200:
                data = response.json()
                connections = data.get('connections', 0)
                self.log_result("Streaming Server", "PASS", f"Health endpoint: {data}")
            else:
                self.log_result("Streaming Server", "FAIL", f"HTTP {response.status_code}")
        except Exception as e:
            self.log_result("Streaming Server", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_websocket_endpoint(self):
        """Test WebSocket endpoint connectivity"""
        streaming_url = os.environ.get("PUBLIC_STREAMING_URL", "https://sara-ai-streaming.onrender.com")
        ws_url = streaming_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, timeout=10) as ws:
                    # Wait for connected event
                    msg = await ws.receive(timeout=5)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get('event') == 'connected':
                            self.log_result("WebSocket Endpoint", "PASS", "Connected event received")
                        else:
                            self.log_result("WebSocket Endpoint", "FAIL", f"Unexpected message: {data}")
                    else:
                        self.log_result("WebSocket Endpoint", "FAIL", f"Unexpected message type: {msg.type}")
                    
                    await ws.close()
        except Exception as e:
            self.log_result("WebSocket Endpoint", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_render_urls(self):
        """Test Render URL accessibility"""
        urls_to_test = [
            "https://sara-ai-bot.onrender.com",
            "https://sara-ai-streaming.onrender.com"
        ]
        
        for url in urls_to_test:
            try:
                response = requests.get(url, timeout=10)
                if response.status_code in [200, 404]:  # 404 is OK for root path
                    self.log_result(f"URL: {url}", "PASS", f"Accessible (HTTP {response.status_code})")
                else:
                    self.log_result(f"URL: {url}", "FAIL", f"HTTP {response.status_code}")
            except Exception as e:
                self.log_result(f"URL: {url}", "FAIL", f"Connection failed: {str(e)}")
    
    async def test_twiml_structure(self):
        """Analyze TwiML structure for issues"""
        server_url = os.environ.get("SERVER_URL", "https://sara-ai-bot.onrender.com")
        
        try:
            # Simulate Twilio's POST request to outbound endpoint
            response = requests.post(
                f"{server_url}/outbound",
                data={"CallSid": "test_diagnostic", "From": "+15555555555", "To": "+15555555556"},
                timeout=10
            )
            
            if response.status_code == 200:
                twiml = response.text
                
                # Analyze TwiML structure
                issues = []
                
                if "<Start>" not in twiml:
                    issues.append("Missing <Start> verb")
                if "<Stream>" not in twiml:
                    issues.append("Missing <Stream> verb")
                if "wss://" not in twiml:
                    issues.append("WebSocket URL not using wss://")
                if "/ws" not in twiml:
                    issues.append("WebSocket endpoint missing /ws path")
                if "<Pause" not in twiml:
                    issues.append("Missing <Pause> to keep call alive")
                
                if issues:
                    self.log_result("TwiML Structure", "FAIL", f"Issues: {', '.join(issues)}")
                else:
                    self.log_result("TwiML Structure", "PASS", "TwiML structure appears correct")
                    
                # Log the actual TwiML for debugging
                print("   TwiML Response:")
                for line in twiml.split('\n'):
                    if line.strip():
                        print(f"     {line.strip()}")
            else:
                self.log_result("TwiML Structure", "FAIL", f"Endpoint returned HTTP {response.status_code}")
        except Exception as e:
            self.log_result("TwiML Structure", "FAIL", f"Test failed: {str(e)}")
    
    async def test_ffmpeg(self):
        """Test FFmpeg availability and functionality"""
        try:
            # Check if ffmpeg is available
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
            if result.returncode == 0:
                version_line = result.stdout.split('\n')[0]
                self.log_result("FFmpeg", "PASS", f"Available: {version_line}")
            else:
                self.log_result("FFmpeg", "FAIL", "Not available or not in PATH")
        except Exception as e:
            self.log_result("FFmpeg", "FAIL", f"Check failed: {str(e)}")
    
    def generate_report(self):
        """Generate comprehensive diagnostic report"""
        print("\n" + "=" * 60)
        print("📊 DIAGNOSTIC REPORT SUMMARY")
        print("=" * 60)
        
        passed = sum(1 for result in self.results.values() if result["status"] == "PASS")
        failed = sum(1 for result in self.results.values() if result["status"] == "FAIL")
        warnings = sum(1 for result in self.results.values() if result["status"] == "WARNING")
        
        print(f"✅ PASSED: {passed}")
        print(f"❌ FAILED: {failed}")
        print(f"⚠️ WARNINGS: {warnings}")
        
        # Show critical failures first
        print("\n🔴 CRITICAL ISSUES:")
        critical_failures = []
        for test, result in self.results.items():
            if result["status"] == "FAIL" and any(keyword in test for keyword in 
                ["OpenAI", "ElevenLabs", "Twilio", "TwiML", "WebSocket"]):
                critical_failures.append((test, result['details']))
        
        if critical_failures:
            for test, details in critical_failures:
                print(f"   • {test}: {details}")
        else:
            print("   No critical issues found!")
        
        # Show API-specific results
        print("\n🔬 API TEST RESULTS:")
        api_tests = [("OpenAI Chat", "OpenAI Chat Completion"), 
                    ("OpenAI Whisper", "OpenAI Whisper"),
                    ("ElevenLabs TTS", "ElevenLabs TTS")]
        
        for short_name, full_name in api_tests:
            result = self.results.get(full_name, {})
            status = result.get('status', 'NOT TESTED')
            details = result.get('details', '')
            icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚪"
            print(f"   {icon} {short_name}: {status} - {details}")
        
        if failed == 0:
            print("\n🎉 ALL SYSTEMS GO! All tests passed.")
            print("   The system should be fully operational.")
        else:
            print(f"\n🔧 {failed} issues need to be fixed.")

async def main():
    diagnostic = SaraDiagnostic()
    await diagnostic.run_all_tests()

if __name__ == "__main__":
    # Set environment variables for testing if running locally
    if not os.environ.get("TWILIO_ACCOUNT_SID"):
        print("⚠️  Running in local test mode - some tests may be skipped")
    
    asyncio.run(main())