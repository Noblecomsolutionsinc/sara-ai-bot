import os
from flask import Flask, request, jsonify
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

# Initialize Sentry
init_sentry()

app = Flask(__name__)

@app.route("/tts", methods=["POST"])
def tts():
    data = request.json
    text = data.get("text", "")
    log_event(service="tts_server", event="tts_request", status="ok", message=f"Received text: {text[:30]}...")
    return jsonify({"audio_url": f"/static/audio/{hash(text)}.mp3"})

if __name__ == "__main__":
    port = int(os.getenv("TTS_PORT", 6000))
    log_event(service="tts_server", event="startup", status="ok", message=f"Listening on {port}")
    app.run(host="0.0.0.0", port=port)
