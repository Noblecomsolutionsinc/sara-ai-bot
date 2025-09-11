import os
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from twilio.rest import Client
from call_handler import handle_incoming_call

load_dotenv()

app = Flask(__name__)

# Twilio client
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
twilio_number = os.getenv("TWILIO_PHONE_NUMBER")
client = Client(account_sid, auth_token)

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "Sara AI outbound bot is running 🚀"})

@app.route("/start_call", methods=["POST"])
def start_call():
    data = request.get_json()
    to_number = data.get("to")
    if not to_number:
        return jsonify({"error": "Missing 'to' phone number"}), 400
    try:
        call = client.calls.create(
            to=to_number,
            from_=twilio_number,
            url=f"{os.getenv('SERVER_URL')}/voice"  # Twilio hits this when the call connects
        )
        return jsonify({"message": "Call initiated", "call_sid": call.sid}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/voice", methods=["POST"])
def voice():
    """Twilio webhook — controls the conversation after call connects"""
    return handle_incoming_call(request)

# Serve generated audio files (Twilio must be able to GET this URL)
@app.route("/audio/<path:filename>", methods=["GET"])
def serve_audio(filename):
    audio_dir = os.path.join(os.path.dirname(__file__), "public_audio")
    return send_from_directory(audio_dir, filename, as_attachment=False)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
