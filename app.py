import os
import tempfile
import requests
from flask import Flask, request, Response, send_file
from twilio.twiml.voice_response import VoiceResponse, Gather
from openai import OpenAI
from dotenv import load_dotenv

# -----------------------------
# Load environment variables
# -----------------------------
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
SERVER_URL = os.getenv("SERVER_URL")

required_vars = ["OPENAI_KEY","ELEVENLABS_KEY","VOICE_ID","TWILIO_NUMBER","SERVER_URL"]
missing = [v for v in required_vars if not globals().get(v)]
if missing:
    raise Exception(f"Missing required environment variables: {missing}")

# OpenAI client
client = OpenAI(api_key=OPENAI_KEY)

# -----------------------------
# Load Sara Brain text files
# -----------------------------
TXT_FILES = [
    "Sara_SystemPrompt.txt",
    "Sara_Flow.txt",
    "Sara_Opening.txt",
    "Sara_Objections_Playbook_Full.txt",
    "Sara_Knowledgebase.txt",
    "Sara_MasterPrompt.txt"
]

SARA_BRAIN = {}
for f in TXT_FILES:
    path = os.path.join(os.getcwd(), f)
    if not os.path.exists(path):
        raise FileNotFoundError(f"{f} not found!")
    with open(path, "r", encoding="utf-8") as file:
        SARA_BRAIN[f] = file.read()

# -----------------------------
# Flask app
# -----------------------------
app = Flask(__name__)

# Audio storage
STATIC_AUDIO_DIR = os.path.join("static", "audio")
os.makedirs(STATIC_AUDIO_DIR, exist_ok=True)

# -----------------------------
# GPT Brain Response
# -----------------------------
def sara_gpt_response(prospect_input, conversation_history=[]):
    system_prompt = SARA_BRAIN["Sara_SystemPrompt.txt"] + "\n" + SARA_BRAIN["Sara_MasterPrompt.txt"]
    messages = [{"role": "system", "content": system_prompt}]
    for c in conversation_history:
        messages.append(c)
    messages.append({"role": "user", "content": prospect_input})

    resp = client.chat.completions.create(
        model="gpt-5-mini",
        messages=messages
    )
    return resp.choices[0].message.content

# -----------------------------
# ElevenLabs TTS
# -----------------------------
def generate_voice_mp3(text, call_sid):
    mp3_filename = f"sara_{call_sid}.mp3"
    mp3_path = os.path.join(STATIC_AUDIO_DIR, mp3_filename)

    if os.path.exists(mp3_path):
        return mp3_path

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability":0.75, "similarity_boost":0.8}}
    r = requests.post(url, headers=headers, json=payload, stream=True)
    with open(mp3_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024):
            f.write(chunk)
    return mp3_path

# -----------------------------
# Outbound Call
# -----------------------------
@app.route("/outbound", methods=["POST"])
def outbound():
    to_number = request.form.get("To") or request.form.get("phone")
    if not to_number:
        return "No number provided", 400

    resp = VoiceResponse()
    gather = Gather(input="speech", timeout=5, action=f"{SERVER_URL}/conversation", method="POST")
    gather.say("Hi! This is Sara calling you. Please respond after the beep.")
    resp.append(gather)
    return Response(str(resp), mimetype="application/xml")

# -----------------------------
# Conversation Loop
# -----------------------------
@app.route("/conversation", methods=["POST"])
def conversation():
    prospect_input = request.form.get("SpeechResult", "")
    call_sid = request.form.get("CallSid")
    if not call_sid:
        return "CallSid missing", 400

    if not hasattr(app, "call_histories"):
        app.call_histories = {}
    history = app.call_histories.get(call_sid, [])

    sara_text = sara_gpt_response(prospect_input, history)
    history.append({"role":"user","content":prospect_input})
    history.append({"role":"assistant","content":sara_text})
    app.call_histories[call_sid] = history

    audio_file = generate_voice_mp3(sara_text, call_sid)
    audio_url = f"{SERVER_URL}/call_audio/{os.path.basename(audio_file)}"

    resp = VoiceResponse()
    gather = Gather(input="speech", timeout=5, action=f"{SERVER_URL}/conversation", method="POST")
    gather.play(audio_url)
    resp.append(gather)

    return Response(str(resp), mimetype="application/xml")

# -----------------------------
# Serve audio
# -----------------------------
@app.route("/call_audio/<filename>")
def serve_audio(filename):
    path = os.path.join(STATIC_AUDIO_DIR, filename)
    if not os.path.exists(path):
        return "Audio not found", 404
    return send_file(path, mimetype="audio/mpeg")

# -----------------------------
# Cleanup old MP3s
# -----------------------------
def cleanup_audio_folder(max_files=50):
    files = sorted(
        [os.path.join(STATIC_AUDIO_DIR, f) for f in os.listdir(STATIC_AUDIO_DIR)],
        key=os.path.getmtime
    )
    while len(files) > max_files:
        os.remove(files[0])
        files.pop(0)

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    cleanup_audio_folder()
    app.run(debug=True)
