# celery_app.py
import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER = os.environ.get("CELERY_BROKER", REDIS_URL)
CELERY_BACKEND = os.environ.get("CELERY_BACKEND", REDIS_URL)

celery = Celery(
    "sara_tasks",
    broker=CELERY_BROKER,
    backend=CELERY_BACKEND,
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)
