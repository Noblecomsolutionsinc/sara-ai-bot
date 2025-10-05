# ---- Base Image ----
FROM python:3.11-slim as base

# Set working directory
WORKDIR /app

# Install system dependencies (for eventlet, psycopg2, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ---- Install Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Copy project ----
COPY . .

# Default environment variables
ENV ENV_MODE=render \
    SERVICE_TYPE=app \
    PYTHONUNBUFFERED=1

# ---- Entrypoint ----
# Multi-service support: app (Flask API), worker (Celery), streaming (Twilio/WS)
CMD if [ "$SERVICE_TYPE" = "worker" ]; then \
      celery -A sara_ai.celery_app.celery worker --loglevel=INFO; \
    elif [ "$SERVICE_TYPE" = "streaming" ]; then \
      gunicorn -b 0.0.0.0:6000 -k eventlet sara_ai.streaming_server:app; \
    else \
      gunicorn -b 0.0.0.0:5000 sara_ai.app:app; \
    fi
