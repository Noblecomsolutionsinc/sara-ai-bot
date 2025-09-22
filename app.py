import os
import uuid
import time
from flask import Flask, request, send_from_directory, jsonify
from dotenv import load_dotenv
import requests
from openai import OpenAI
from twilio.rest import Client

load_dotenv()

# ---------------------------
# Environment Variables
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
SARA_COMPANY = os.getenv("COMPANY_NAME")
CALENDLY_LINK = os.getenv("MEETING_LINK")

# ---------------------------
# Flask App Setup
# ---------------------------
app = Flask(__name__)
AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

# ---------------------------
# OpenAI Client
# ---------------------------
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def generate_gpt_response(prompt, retries=2):
    for attempt in range(retries + 1):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-5-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                raise Exception(f"GPT generation failed after {retries+1} attempts: {str(e)}")

# ---------------------------
# ElevenLabs TTS
# ---------------------------
def generate_voice(text, retries=2):
    filename = f"{uuid.uuid4().hex}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    data = {"text": text, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=data, headers=headers, timeout=30)
            if response.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(response.content)
                return filename
            else:
                raise Exception(f"ElevenLabs TTS failed: {response.text}")
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                raise Exception(f"TTS failed after {retries+1} attempts: {str(e)}")

# ---------------------------
# Twilio Client
# ---------------------------
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def make_twilio_call(phone, audio_url):
    try:
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            twiml=f'<Response><Play>{audio_url}</Play></Response>'
        )
        return call.sid
    except Exception as e:
        raise Exception(f"Twilio call failed: {str(e)}")

# ---------------------------
# Routes
# ---------------------------
@app.route("/", methods=["GET"])
def health_check():
    return "Sara AI Server is running ✅", 200

@app.route("/call_audio/<filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename)

@app.route("/outbound", methods=["POST"])
def outbound_call():
    data = request.get_json()
    name = data.get("name")
    phone = data.get("phone")
    
    if not name or not phone:
        return jsonify({"error": "Missing name or phone"}), 400

    prompt = f"""
    You are {SARA_NAME}, a {SARA_ROLE} from {SARA_COMPANY}.
    Call {name} and introduce yourself professionally.
    Your goal is to create urgency, explain lost revenue opportunity, 
    handle objections smoothly, and book a meeting at {CALENDLY_LINK}.
    """
    try:
        gpt_response = generate_gpt_response(prompt)
        audio_file = generate_voice(gpt_response)
        audio_url = f"{SERVER_URL}/call_audio/{audio_file}"
        call_sid = make_twilio_call(phone, audio_url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "status": "success",
        "audio_url": audio_url,
        "message_text": gpt_response,
        "twilio_call_sid": call_sid
    })

@app.route("/conversation", methods=["POST"])
def conversation():
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "Missing messages"}), 400
    try:
        response_text = generate_gpt_response(messages[-1]["content"])
        audio_file = generate_voice(response_text)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
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
