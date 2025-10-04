from flask import Flask, request, jsonify
from sara_ai.tasks import process_event
from sara_ai.logging_utils import log_event

app = Flask(__name__)

log_event(
    service="inference_server",
    event="startup",
    status="success",
    message="Inference server started successfully",
)

@app.route("/inference", methods=["POST"])
def inference():
    payload = request.get_json(force=True)
    task = process_event.delay(payload)
    return jsonify({"task_id": task.id}), 202

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=6000)
