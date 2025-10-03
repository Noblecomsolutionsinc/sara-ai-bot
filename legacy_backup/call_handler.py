import os
import requests
import openai
from twilio.rest import Client
import tempfile

# --- Environment ---
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")

client = Client(TWILIO_SID, TWILIO_TOKEN)
openai.api_key = OPENAI_KEY

# --- Load Sara Brain ---
TXT_FILES = [
    "Sara_SystemPrompt.txt",
    "Sara_Flow.txt",
    "Sara_Opening.txt",
    "Sara_Objections_Playbook_Full.txt",
    "Sara_Knowledgebase.txt",
    "Sara_MasterPrompt.txt"
]

SARA_BRAIN = {f: open(f, "r", encoding="utf-8").read() for f in TXT_FILES}

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
    payload = {"text": text, "voice_settings": {"stability": 0.75, "similarity_boost": 0.8}}

    r = requests.post(url, headers=headers, json=payload, stream=True)
    if r.status_code != 200:
        raise Exception(f"ElevenLabs TTS failed: {r.text}")

    tmp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    for chunk in r.iter_content(chunk_size=1024):
        tmp_file.write(chunk)
    tmp_file.close()
    return tmp_file.name

# --- Make Call ---
def make_call(to_number):
    conversation_history = []

    opening_prompt = SARA_BRAIN["Sara_Opening.txt"] + "\nStart the call with curiosity and urgency."
    reply_text = sara_gpt_response(opening_prompt, conversation_history)
    conversation_history.append({"role": "assistant", "content": reply_text})

    audio_file = generate_voice(reply_text)
    public_url = f"https://sara-ai-bot.onrender.com/call_audio/{os.path.basename(audio_file)}"

    call = client.calls.create(
        to=to_number,
        from_=TWILIO_NUMBER,
        twiml=f'<Response><Play>{public_url}</Play></Response>'
    )
    print(f"[INFO] Call initiated to {to_number} | Call SID: {call.sid}")
