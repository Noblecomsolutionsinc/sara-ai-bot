import os
import uuid
from flask import Flask, request, send_from_directory, jsonify
import openai
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------
# Environment Variables (from your .env)
# ---------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")
SARA_NAME = os.getenv("SARA_NAME", "Sara")
SARA_ROLE = os.getenv("SARA_ROLE", "Digital Marketing Consultant")
SARA_COMPANY = os.getenv("COMPANY_NAME")       # matches your .env
CALENDLY_LINK = os.getenv("MEETING_LINK")      # matches your .env

# ---------------------------
# OpenAI Initialization
# ---------------------------
openai.api_key = OPENAI_API_KEY

# ---------------------------
# Flask App Setup
# ---------------------------
app = Flask(__name__)
AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# ---------------------------
# Helper Functions
# ---------------------------
def generate_gpt_response(prompt, temperature=0.7):
    """Generate GPT-5-mini response from prompt"""
    response = openai.chat.completions.create(
        model="gpt-5-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature
    )
    return response.choices[0].message["content"]

def generate_voice(text):
    """Generate MP3 via ElevenLabs"""
    filename = f"{uuid.uuid4().hex}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)
    
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json"
    }
    data = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    response = requests.post(url, json=data, headers=headers)
    
    if response.status_code == 200:
        with open(filepath, "wb") as f:
            f.write(response.content)
        return filename
    else:
        raise Exception(f"ElevenLabs TTS failed: {response.text}")

# ---------------------------
# Routes
# ---------------------------
@app.route("/call_audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route("/outbound", methods=["POST"])
def outbound_call():
    """
    Trigger a new call:
    Expects JSON: {"name": "John Doe", "phone": "+123456789"}
    """
    data = request.get_json()
    name = data.get("name")
    phone = data.get("phone")
    
    if not name or not phone:
        return jsonify({"error": "Missing name or phone"}), 400

    # Build opening prompt
    prompt = f"""
    You are {SARA_NAME}, a {SARA_ROLE} from {SARA_COMPANY}.
    Call {name} and introduce yourself professionally.
    Your goal is to create urgency, explain lost revenue opportunity, 
    handle objections smoothly, and book a meeting at {CALENDLY_LINK}.
    """
    
    gpt_response = generate_gpt_response(prompt)
    audio_file = generate_voice(gpt_response)

    return jsonify({
        "status": "success",
        "audio_url": f"{SERVER_URL}/call_audio/{audio_file}",
        "message_text": gpt_response
    })

@app.route("/conversation", methods=["POST"])
def conversation():
    """
    Continue conversation: Expects JSON {"messages": [{"role":"user","content":"..."}]}
    Returns GPT + TTS
    """
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "Missing messages"}), 400

    response_text = generate_gpt_response(messages[-1]["content"])
    audio_file = generate_voice(response_text)
    
    return jsonify({
        "status": "success",
        "audio_url": f"{SERVER_URL}/call_audio/{audio_file}",
        "message_text": response_text
    })

# ---------------------------
# Run Flask App
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
