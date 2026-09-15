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
    pip install --no-cache-dir -e . uv

# Prebake the external DuckDuckGo MCP server in its OWN venv (pinned).
# It requires mcp>=2.1 while the agent pins mcp<2.0, so the two must not
# share one environment — the MCP stdio boundary is also the dependency
# boundary. The agent spawns the installed console script directly: zero
# resolution or installs at runtime. ([browser] adds curl-cffi because
# html.duckduckgo.com rejects plain httpx TLS fingerprints with empty 202s.)
RUN python -m venv /opt/mcp/ddg && \
    /opt/mcp/ddg/bin/pip install --no-cache-dir "duckduckgo-mcp-server[browser]==0.7.0"

# Ensure workspace directories exist
RUN mkdir -p workspace/data workspace/artifacts

# Environment defaults
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
# The prebaked external MCP server is reachable by bare command name
# (config: tools.mcp.search_command), keeping config portable across
# container and host installs.
ENV PATH="/opt/mcp/ddg/bin:${PATH}"

# Entrypoint launches the terminal agent
ENTRYPOINT ["studymate"]
