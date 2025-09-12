from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import os
import openai
import requests
import tempfile

# Load environment
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

openai.api_key = OPENAI_KEY

# Load Sara Brain
TXT_FILES = [
    "Sara_SystemPrompt.txt",
    "Sara_Flow.txt",
    "Sara_Opening.txt",
    "Sara_Objections_Playbook_Full.txt",
    "Sara_Knowledgebase.txt",
    "Sara_MasterPrompt.txt"
]
SARA_BRAIN = {f: open(f, "r", encoding="utf-8").read() for f in TXT_FILES}

# Flask app
app = Flask(__name__)

# --- GPT Response ---
def sara_gpt_response(prospect_input, conversation_history=[]):
    system_prompt = SARA_BRAIN["Sara_SystemPrompt.txt"] + "\n" + SARA_BRAIN["Sara_MasterPrompt.txt"]
    messages = [{"role": "system", "content": system_prompt}]
    for c in conversation_history:
        messages.append(c)
    messages.append({"role": "user", "content": prospect_input})

    resp = openai.ChatCompletion.create(
        model="gpt-5-mini",
        messages=messages,
        temperature=0.7
    )
    return resp['choices'][0]['message']['content']

# --- ElevenLabs TTS ---
def generate_voice(text):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/stream"
    headers = {"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"}
    payload = {"text": text, "voice_settings": {"stability":0.75,"similarity_boost":0.8}}
    r = requests.post(url, headers=headers, json=payload, stream=True)
    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    for chunk in r.iter_content(chunk_size=1024):
        tmp_file.write(chunk)
    tmp_file.close()
    return tmp_file.name

# --- Outbound Call Entry ---
@app.route("/outbound", methods=["POST"])
def outbound():
    data = request.form
    to_number = data.get("To") or data.get("phone")
    if not to_number:
        return "No number provided", 400

    # Respond with initial TwiML to start call
    resp = VoiceResponse()
    gather = Gather(input="speech", timeout=5, action="/conversation", method="POST")
    gather.say("Hi! This is Sara calling you. Please respond after the beep.")
    resp.append(gather)
    return Response(str(resp), mimetype="application/xml")

# --- Conversation Loop ---
@app.route("/conversation", methods=["POST"])
def conversation():
    # Get speech from prospect
    prospect_input = request.form.get("SpeechResult", "")
    call_sid = request.form.get("CallSid")

    # Load or initialize conversation history (can be stored in memory or DB)
    if not hasattr(app, "call_histories"):
        app.call_histories = {}
    history = app.call_histories.get(call_sid, [])

    # GPT Response
    sara_text = sara_gpt_response(prospect_input, history)
    history.append({"role":"user","content":prospect_input})
    history.append({"role":"assistant","content":sara_text})
    app.call_histories[call_sid] = history

    # Generate voice
    audio_file = generate_voice(sara_text)
    audio_url = f"https://sara-ai-bot.onrender.com/call_audio/{audio_file.split('/')[-1]}"

    # TwiML Response
    resp = VoiceResponse()
    gather = Gather(input="speech", timeout=5, action="/conversation", method="POST")
    gather.play(audio_url)
    resp.append(gather)

    return Response(str(resp), mimetype="application/xml")

# --- Serve audio files dynamically ---
from flask import send_file
@app.route("/call_audio/<filename>")
def serve_audio(filename):
    return send_file(filename, mimetype="audio/mpeg")

if __name__ == "__main__":
    app.run(debug=True)
