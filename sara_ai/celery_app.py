import os
from celery import Celery
from sara_ai.logging_utils import log_event
from sara_ai.sentry_utils import init_sentry

# Initialize Sentry
init_sentry()

celery_app = Celery("sara_ai", broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"))

@celery_app.task
def example_task(x, y):
    log_event(service="celery", event="task_run", status="ok", message=f"Adding {x} + {y}")
    return x + y

if __name__ == "__main__":
    log_event(service="celery", event="startup", status="ok", message="Celery worker starting")
    celery_app.start()
