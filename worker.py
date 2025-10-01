# worker.py
"""
Sara Worker Process
-------------------
Runs background tasks (campaigns, scheduled calls, redis jobs).
This complements app.py which serves Flask endpoints and Twilio webhooks.
"""

import os
import time
import logging
import redis
from rq import Worker, Queue, Connection

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [WORKER] %(levelname)s: %(message)s"
)

# Redis connection
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_conn = redis.from_url(REDIS_URL)

# Define RQ queues
listen = ["default", "calls", "campaigns"]

def start_worker():
    logging.info(f"Starting Sara Worker. Redis URL: {REDIS_URL}")
    with Connection(redis_conn):
        worker = Worker(list(map(Queue, listen)))
        worker.work(with_scheduler=True)

if __name__ == "__main__":
    start_worker()
