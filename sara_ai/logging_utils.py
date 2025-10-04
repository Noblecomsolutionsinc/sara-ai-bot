import json
import logging
from datetime import datetime

"""
Schema fields:
- timestamp: ISO8601 UTC
- service: component name (e.g., tts_server)
- event: short event tag
- status: success|failure|pending
- message: descriptive text
- level: INFO|WARNING|ERROR
- extra: optional JSON metadata
"""

def log_event(service, event, status, message, level="INFO", extra=None):
    try:
        log_record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "service": service,
            "event": event,
            "status": status,
            "message": message,
            "level": level.upper(),
            "extra": extra if isinstance(extra, dict) else {}
        }
        line = json.dumps(log_record, ensure_ascii=False)

    except Exception as e:
        # Fallback if serialization fails
        line = json.dumps({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "service": "logging_utils",
            "event": "serialization_error",
            "status": "failure",
            "message": f"Failed to serialize log: {e}",
            "level": "ERROR",
            "extra": {}
        })

    # Map level string to logging function
    level_upper = level.upper()
    logger_func = getattr(logging, level_upper.lower(), logging.info)
    logger_func(line)
