# Multi-stage Dockerfile (builder + runtime)
FROM python:3.11-slim AS builder
WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends build-essential gcc curl git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel
RUN python -m pip install --prefix=/install -r requirements.txt

# Runtime image
FROM python:3.11-slim
WORKDIR /app

# Add runtime deps (ffmpeg)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*

# Add non-root user
RUN useradd --create-home --shell /bin/bash appuser
USER appuser

# Copy installed packages from builder
COPY --from=builder /install /usr/local
# Copy application code
COPY --chown=appuser:appuser . .

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

EXPOSE 5000 8765

# Default command is to run web; docker-compose overrides for other services
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app", "--workers=2", "--log-level=info"]
