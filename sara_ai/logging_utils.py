import json
import logging
import os
from datetime import datetime

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)

def log_event(service: str, event: str, status: str, message: str, extra: dict = None):
    log = {
        "timestamp": datetime.utcnow().isoformat(),
        "service": service,
        "event": event,
        "status": status,
        "message": message,
    }
    if extra and LOG_LEVEL == "DEBUG":
        log.update(extra)

    print(json.dumps(log))
