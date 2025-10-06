"""
Celery Application Configuration for Sara AI
--------------------------------------------
This module configures the Celery app used for background task processing.
It integrates Sentry for error tracking, structured logging with trace IDs,
and platform-specific settings for Windows (solo pool).
"""

import os
import platform
from celery import Celery
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry


# --- Platform Compatibility ---
# Auto-adjust for Windows to avoid multiprocessing fork issues
if platform.system() == "Windows":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")
    os.environ.setdefault("CELERYD_POOL", "solo")


# --- Broker Configuration ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Initialize Celery
celery = Celery("sara_ai", broker=REDIS_URL, backend=REDIS_URL)

# Observability
init_sentry()
trace_id = log_event(
    service="celery",
    event="startup",
    status="ok",
    message=f"Celery initialized with broker={REDIS_URL}",
)

# Example task (for test & healthcheck)
@celery.task(name="example_task")
def example_task(data, trace_id=None):
    trace_id = log_event(
        service="celery",
        event="task_start",
        status="ok",
        message=f"Task received: {data}",
        trace_id=trace_id,
    )

    # Simulated logic
    result = {"status": "ok", "data": data}

    log_event(
        service="celery",
        event="task_complete",
        status="ok",
        message=f"Task completed: {data}",
        trace_id=trace_id,
    )
    return {"status": "ok", "trace_id": trace_id, "result": result}


# --- Export for imports in other modules ---
celery_app = celery
