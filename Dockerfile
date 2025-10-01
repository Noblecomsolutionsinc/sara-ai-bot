# Use an official Python runtime as a base
FROM python:3.11-slim

# Install system dependencies (ffmpeg + curl)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose port 5000 for Flask
EXPOSE 5000

# Command to run your app
CMD ["gunicorn", "app:app", "--workers=1", "--threads=2", "--timeout=300", "--bind", "0.0.0.0:5000"]
