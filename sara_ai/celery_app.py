from celery import Celery
from sara_ai.logging_utils import log_event

celery = Celery(
    "sara_ai",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
)

# Explicit import ensures task registration
import sara_ai.tasks  # noqa

log_event(
    service="celery_app",
    event="startup",
    status="success",
    message="Celery initialized successfully",
)
