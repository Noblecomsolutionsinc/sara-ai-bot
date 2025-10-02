#!/usr/bin/env python3
"""
celery_app.py

Creates a Celery app named `worker` and exposes it for use by tasks.py.
Broker/backend default to REDIS_URL if CELERY_BROKER/CELERY_BACKEND not set.
"""
from __future__ import annotations

import os
import logging
from celery import Celery

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("celery_app")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER = os.getenv("CELERY_BROKER", REDIS_URL)
CELERY_BACKEND = os.getenv("CELERY_BACKEND", REDIS_URL)

worker = Celery("worker", broker=CELERY_BROKER, backend=CELERY_BACKEND)

worker.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    enable_utc=True,
)

# Autodiscover tasks in the local package/module
worker.autodiscover_tasks(["tasks"])

logger.info("Celery worker configured: broker=%s backend=%s", CELERY_BROKER, CELERY_BACKEND)
