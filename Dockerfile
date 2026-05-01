# Vera Bot — production container for fly.io / Render / any cloud.
# Build:    docker build -t vera-bot .
# Run:      docker run -p 8080:8080 --env-file .env vera-bot
# Healthz:  curl http://localhost:8080/v1/healthz

FROM python:3.11-slim AS base

# Don't bake bytecode + don't buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install uv (fast Python installer) — matches local dev toolchain
RUN pip install --no-cache-dir uv==0.4.* && \
    apt-get update -y && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy lock + project metadata first (better Docker layer caching)
COPY pyproject.toml ./
COPY uv.lock* ./

# Install runtime deps (no dev deps in container)
RUN uv pip install --system --no-cache \
    "anthropic>=0.40.0" \
    "openai>=1.50.0" \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "pydantic>=2.9.0" \
    "httpx>=0.27.0" \
    "python-dotenv>=1.0.0"

# Copy app code
COPY bot.py server.py state.py llm_client.py validator.py classifiers.py obs.py make_submission.py ./
COPY prompts/ ./prompts/
COPY dataset/ ./dataset/

# Cache + log dirs (writable by the runtime user)
RUN mkdir -p /app/.cache /app/logs && chmod -R 0777 /app/.cache /app/logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://localhost:8080/v1/healthz || exit 1

# Run as a non-root user
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 verabot && \
    chown -R verabot:verabot /app
USER verabot

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
