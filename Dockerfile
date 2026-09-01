FROM python:3.12-slim-bookworm

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy project specification & dependencies
COPY pyproject.toml requirements.txt README.md ./

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e .

# Copy project source and configuration
COPY src/ src/
COPY config/ config/
COPY workspace/ workspace/

# Environment defaults
ENV PYTHONUNBUFFERED=1

# Entrypoint launches the terminal agent
ENTRYPOINT ["studymate"]
