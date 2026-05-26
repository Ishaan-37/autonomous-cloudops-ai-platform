# ============================================================
# Dockerfile
# Multi-stage build for the CloudOps Agent FastAPI app.
#
# WHY MULTI-STAGE?
#   Stage 1 (builder): installs all deps including build tools
#   Stage 2 (runtime): copies only what's needed to run
#   Result: final image is ~200MB instead of ~800MB
#   Smaller image = faster pulls = faster pod startup in EKS
#
# BUILD:
#   docker build -t cloudops-agent:latest .
#
# RUN LOCALLY:
#   docker run -p 8080:8080 \
#     -e OPENAI_API_KEY=sk-... \
#     -e PINECONE_API_KEY=pcsk-... \
#     -e SLACK_BOT_TOKEN=xoxb-... \
#     -e SLACK_SIGNING_SECRET=... \
#     -e CLOUDOPS_ENVIRONMENT=development \
#     cloudops-agent:latest
# ============================================================

# ── Stage 1: Builder ──────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv (fast pip replacement)
RUN pip install uv --no-cache-dir

# Copy dependency files first (layer caching)
# If pyproject.toml hasn't changed, this layer is cached
COPY pyproject.toml .

# Install all dependencies into /app/.venv
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
    langgraph langchain-openai openai \
    fastapi uvicorn[standard] \
    pinecone-client boto3 \
    slack-sdk aiohttp beautifulsoup4 \
    pydantic pydantic-settings \
    opentelemetry-sdk --no-cache

# ── Stage 2: Runtime ──────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Create non-root user (security best practice)
# Never run containers as root in production
RUN groupadd -r cloudops && useradd -r -g cloudops cloudops

# Copy virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY agent/     ./agent/
COPY api/       ./api/
COPY rag/       ./rag/
COPY ingestion/ ./ingestion/

# Create directory for SQLite checkpoint database
RUN mkdir -p /app/data && chown -R cloudops:cloudops /app

# Switch to non-root user
USER cloudops

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# Health check built into Docker
# K8s will also do this, but Docker healthcheck helps during local testing
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

EXPOSE 8080

# Start FastAPI with uvicorn
# --workers 1: single worker (agent has async state, multi-worker needs Redis)
# --host 0.0.0.0: listen on all interfaces (required in containers)
CMD ["uvicorn", "api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info", \
     "--access-log"]
