FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
# gcc and build-essential might be required for building PyNaCl if wheel is not available
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus-dev \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code (without .env or settings.json)
COPY bot.py .

# Create directory for settings persistence
RUN mkdir -p /app/data

# Run the bot
CMD ["python", "bot.py"]
