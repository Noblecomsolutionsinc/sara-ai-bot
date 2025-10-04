from flask import Flask, request, jsonify
from sara_ai.logging_utils import log_event
from sara_ai.celery_app import celery
from sara_ai.tasks import process_event

app = Flask(__name__)

# Startup log
log_event(
    service="flask_app",
    event="startup",
    status="success",
    message="Flask app started successfully",
)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

@app.route("/inference", methods=["POST"])
def inference():
    payload = request.get_json(force=True)
    task = process_event.delay(payload)
    return jsonify({"task_id": task.id}), 202

@app.route("/tts", methods=["POST"])
def tts():
    payload = request.get_json(force=True)
    task = process_event.delay(payload)
    return jsonify({"task_id": task.id}), 202

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
