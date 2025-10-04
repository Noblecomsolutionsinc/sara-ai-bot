import os
import logging
from flask import Flask, jsonify
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

# Initialize Sentry (safe no-op if DSN missing)
init_sentry()

app = Flask(__name__)

@app.route("/")
def index():
    log_event(service="app", event="startup", status="ok", message="Flask app running")
    return jsonify({"status": "ok", "service": "app"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log_event(service="app", event="run", status="starting", message=f"Listening on port {port}")
    app.run(host="0.0.0.0", port=port)
