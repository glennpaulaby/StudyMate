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

# Copy project source and configuration first so editable install discovers study_agent
COPY src/ src/
COPY config/ config/

# Install Python dependencies and package in editable mode
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e .

# Ensure workspace directories exist
RUN mkdir -p workspace/data workspace/artifacts

# Environment defaults
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# Entrypoint launches the terminal agent
ENTRYPOINT ["studymate"]
