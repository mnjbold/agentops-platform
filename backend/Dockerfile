FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/mybijouai-creator/agentops"
LABEL org.opencontainers.image.description="agentops webhook + dashboard API (FastAPI)"

WORKDIR /app

# Install OS deps (curl for healthcheck, sqlite3 for the storage layer)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Python deps first so this layer is cacheable
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the backend
COPY . /app

# Healthcheck hits /api/state
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/state >/dev/null || exit 1

# Server reads .env from the working dir at startup
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "webhooks.server", "--host", "0.0.0.0", "--port", "8080"]
