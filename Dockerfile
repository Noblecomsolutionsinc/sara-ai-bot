# Use slim Python base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip

# Install dependencies from requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose default Flask port
EXPOSE 5000

# Production entrypoint for Flask app
CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "sara_ai.app:app"]
