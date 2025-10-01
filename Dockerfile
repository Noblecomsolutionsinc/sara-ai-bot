# --- Base image ---
FROM python:3.11-slim

# --- Environment ---
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8765

# --- Working directory ---
WORKDIR /app

# --- System dependencies ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# --- Install requirements ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Copy app code ---
COPY . .

# --- Expose port for Render ---
EXPOSE ${PORT}

# --- Entrypoint ---
CMD ["python", "streaming_server.py"]
