from sara_ai.celery_app import celery
from sara_ai.logging_utils import log_event

@celery.task(name="tasks.process_event")
def process_event(payload: dict) -> dict:
    try:
        log_event(
            service="tasks",
            event="process_event",
            status="started",
            message="Processing event",
            extra={"payload": payload},
        )

        # Placeholder business logic
        result = {"status": "ok", "echo": payload}

        log_event(
            service="tasks",
            event="process_event",
            status="success",
            message="Event processed successfully",
            extra={"result": result},
        )
        return result

    except Exception as e:
        log_event(
            service="tasks",
            event="process_event",
            status="error",
            message=str(e),
        )
        raise
