from flask import Flask, request, jsonify
import os
import traceback
from dotenv import load_dotenv
import openai
from elevenlabs import generate, set_api_key  # Adjust if using your own TTS wrapper

# Load environment variables
load_dotenv()

# Flask app
app = Flask(__name__)

# OpenAI setup
openai.api_key = os.getenv("OPENAI_API_KEY")
client = openai.OpenAI(api_key=openai.api_key)

# ElevenLabs setup
ELEVEN_API_KEY = os.getenv("ELEVENLABS_API_KEY")
set_api_key(ELEVEN_API_KEY)

# Function to generate GPT response
def generate_gpt_response(prompt):
    try:
        response = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content
    except Exception as e:
        print("[ERROR] GPT call failed")
        traceback.print_exc()
        raise

# Function to generate TTS audio
def generate_tts_audio(text, voice="alloy"):
    try:
        audio = generate(text=text, voice=voice)
        # Save locally or return as URL depending on your setup
        audio_file = f"temp_{voice}.mp3"
        with open(audio_file, "wb") as f:
            f.write(audio)
        return audio_file
    except Exception as e:
        print("[ERROR] TTS generation failed")
        traceback.print_exc()
        raise

# Outbound route
@app.route("/outbound", methods=["POST"])
def outbound_call():
    try:
        data = request.get_json()
        name = data.get("name")
        phone = data.get("phone")
        print(f"[DEBUG] Received call request: {name} ({phone})")

        prompt = f"Call script for {name}, phone: {phone}"

        # GPT step
        try:
            gpt_response = generate_gpt_response(prompt)
            print("[DEBUG] GPT response received")
        except Exception as e:
            return jsonify({"error": f"GPT generation failed: {str(e)}"}), 500

        # TTS step
        try:
            audio_url = generate_tts_audio(gpt_response)
            print("[DEBUG] TTS audio generated")
        except Exception as e:
            return jsonify({"error": f"TTS generation failed: {str(e)}"}), 500

        return jsonify({
            "message_text": gpt_response,
            "audio_url": audio_url
        })

    except Exception as e:
        print("[ERROR] Outbound call failed")
        traceback.print_exc()
        return jsonify({"error": f"Outbound call failed: {str(e)}"}), 500

# Health check
@app.route("/", methods=["GET"])
def health_check():
    return "Sara AI Server is running ✅"

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
