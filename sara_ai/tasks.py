"""
tasks.py

Celery task definitions for Sara AI.
Handles GPT reply generation and other async background jobs.
"""

import logging
from sara_ai.celery_app import celery
from sara_ai.gpt_client import generate_reply
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

# Initialize Sentry once for observability
init_sentry()

logger = logging.getLogger("tasks")
logger.setLevel(logging.INFO)

@celery.task(name="process_event")
def process_event(event: dict) -> dict:
    """
    Celery task to process incoming events and generate GPT replies.
    """
    trace_id = log_event(
        service="celery_task",
        event="task_start",
        status="ok",
        message="Task process_event started",
    )

    try:
        prompt = event.get("prompt", "")
        task_id = process_event.request.id
        logger.info(f"[{trace_id}] Task {task_id} received (prompt length={len(prompt)})")

        # ✅ Ensure prompt is not empty
        if not prompt:
            raise ValueError("Missing prompt in event payload")

        # ✅ Generate GPT reply
        reply = generate_reply(prompt)

        log_event(
            service="celery_task",
            event="task_complete",
            status="ok",
            message="GPT reply generated successfully",
            trace_id=trace_id,
        )

        return {
            "status": "ok",
            "reply": reply,
            "trace_id": trace_id,
            "task_id": task_id,
        }

    except Exception as e:
        log_event(
            service="celery_task",
            event="task_error",
            status="failed",
            message=str(e),
            level="ERROR",
            trace_id=trace_id,
        )
        logger.exception(f"[{trace_id}] Task {process_event.request.id} failed: {e}")
        return {"status": "error", "message": str(e), "trace_id": trace_id}
