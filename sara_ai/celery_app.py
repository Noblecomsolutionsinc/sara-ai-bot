from celery import Celery
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

app = Celery("sara_ai", broker="redis://localhost:6379/0")

# Observability
init_sentry()
trace_id = log_event(service="celery", event="startup", status="ok", message="Celery app initialized")

@app.task
def example_task(data, trace_id=None):
    trace_id = log_event(service="celery", event="task_start", status="ok", message=f"Task received: {data}", trace_id=trace_id)
    # Task logic here...
    log_event(service="celery", event="task_complete", status="ok", message=f"Task completed: {data}", trace_id=trace_id)
    return {"status": "ok", "trace_id": trace_id}
