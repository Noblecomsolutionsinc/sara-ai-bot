# app.py (minimal safe version with /outbound and /voice stub)
import os
from flask import Flask, jsonify, request, Response
from call_handler import call_next_contact  # must exist and return a string

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Sara AI outbound bot is running 🚀"})

@app.route("/outbound", methods=["GET"])
def outbound():
    """
    Trigger the next outbound call.
    call_next_contact() should return a simple string message on success.
    """
    try:
        message = call_next_contact()
        return jsonify({"message": message}), 200
    except Exception as e:
        # return error to help debugging in Render logs
        return jsonify({"error": str(e)}), 500

# keep /voice present as a safe stub so Twilio won't 404 later
@app.route("/voice", methods=["POST", "GET"])
def voice():
    # simple TwiML response for testing; will be replaced by full logic later
    from twilio.twiml.voice_response import VoiceResponse
    resp = VoiceResponse()
    resp.say("Hello — this is Sara. Your server's /voice endpoint is working.")
    return Response(str(resp), mimetype="text/xml")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
