# Dockerfile (for streaming_server service)
FROM python:3.11-slim

# Install ffmpeg + libs required for pydub/soundfile
RUN apt-get update && apt-get install -y ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

# Copy requirements and install
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy repo
COPY . .

ENV PYTHONUNBUFFERED=1

# Expose (Render supplies $PORT at runtime)
EXPOSE 8765

# Start the streaming server by default (it reads PORT env if provided)
CMD ["python", "streaming_server.py"]
