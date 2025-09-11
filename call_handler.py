import os
import uuid
import time
from flask import Response
from twilio.twiml.voice_response import VoiceResponse, Gather
import openai
import requests
from dotenv import load_dotenv

load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")

ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID")
SERVER_URL = os.getenv("SERVER_URL")  # must be public (ngrok or Render URL)

# load Sara brain files
def load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except:
        return ""

SYSTEM_PROMPT = load_file("Sara_SystemPrompt.txt")
OPENING_SCRIPT = load_file("Sara_Opening.txt")
FLOW = load_file("Sara_Flow.txt")
KNOWLEDGEBASE = load_file("Sara_Knowledgebase.txt")
OBJECTIONS = load_file("Sara_Objections_Playbook_Full.txt")
MASTER_PROMPT = load_file("Sara_MasterPrompt.txt")

# storage for conversation per CallSid
conversation_map = {}  # call_sid -> list of messages

# make sure public_audio dir exists
BASE_DIR = os.path.dirname(__file__)
AUDIO_DIR = os.path.join(BASE_DIR, "public_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)

def generate_sara_response(user_input, call_sid):
    """
    Use OpenAI to generate Sara's reply. Keeps context per call.
    """
    history = conversation_map.get(call_sid, [])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Flow:\n{FLOW}"},
        {"role": "system", "content": f"Knowledgebase:\n{KNOWLEDGEBASE}"},
        {"role": "system", "content": f"Objections:\n{OBJECTIONS}"},
        {"role": "system", "content": f"MasterPrompt:\n{MASTER_PROMPT}"}
    ]
    messages.extend(history)
    if user_input:
        messages.append({"role": "user", "content": user_input})

    resp = openai.ChatCompletion.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_tokens=300,
        temperature=0.7
    )
    reply = resp["choices"][0]["message"]["content"].strip()
    # append to history
    conversation_map.setdefault(call_sid, []).append({"role": "user", "content": user_input} if user_input else {})
    conversation_map.setdefault(call_sid, []).append({"role": "assistant", "content": reply})
    return reply

def synthesize_elevenlabs_to_file(text):
    """
    Call ElevenLabs TTS REST API and save returned audio bytes to a file.
    NOTE: ElevenLabs API surface may change — this is a best-effort example.
    """
    if not ELEVEN_API_KEY or not ELEVEN_VOICE_ID:
        return None

    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVEN_API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg"
        }
        payload = {
            "text": text,
            "model_id": "eleven_monolingual_v1"
        }
        r = requests.post(url, json=payload, headers=headers, stream=True, timeout=30)
        if r.status_code in (200, 201):
            filename = f"{uuid.uuid4().hex}.mp3"
            path = os.path.join(AUDIO_DIR, filename)
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return filename
        else:
            print("ElevenLabs TTS failed:", r.status_code, r.text)
            return None
    except Exception as e:
        print("ElevenLabs exception:", e)
        return None

def handle_incoming_call(req):
    """
    Twilio webhook. Uses SpeechResult to pick up what the user said.
    """
    call_sid = req.values.get("CallSid", str(uuid.uuid4()))
    user_input = req.values.get("SpeechResult", None)
    response = VoiceResponse()

    # If first turn (no user input and no history), play opening
    history = conversation_map.get(call_sid, [])
    if not user_input and not history:
        # opening
        opening = OPENING_SCRIPT or "Hi, this is Sara calling about your marketing. Can I steal 30 seconds?"
        # try ElevenLabs -> file -> URL -> <Play>
        filename = synthesize_elevenlabs_to_file(opening)
        if filename:
            audio_url = f"{SERVER_URL}/audio/{filename}"
            response.play(audio_url)
        else:
            # fallback to Twilio text-to-speech
            response.say(opening, voice="Polly.Joanna")

        # wait/listen for reply
        gather = Gather(input="speech", action="/voice", method="POST", timeout=6, speechTimeout="auto")
        response.append(gather)

        # save opening to history
        conversation_map[call_sid] = [{"role": "assistant", "content": opening}]
        return Response(str(response), mimetype="text/xml")

    # Else, we have user input (or follow-up)
    if user_input:
        conversation_map.setdefault(call_sid, []).append({"role": "user", "content": user_input})

    # generate reply
    reply = generate_sara_response(user_input, call_sid)

    # synthesize reply
    filename = synthesize_elevenlabs_to_file(reply)
    if filename:
        audio_url = f"{SERVER_URL}/audio/{filename}"
        response.play(audio_url)
    else:
        response.say(reply, voice="Polly.Joanna")

    # continue gathering for more speech
    gather = Gather(input="speech", action="/voice", method="POST", timeout=6, speechTimeout="auto")
    response.append(gather)

    return Response(str(response), mimetype="text/xml")
