# Pulse — Phase 1 (Observer). Single container, no build step.
FROM python:3.12-slim

# Non-root runtime user; /data is the only writable path (SQLite WAL lives here).
RUN useradd --system --uid 10001 --create-home --home-dir /app pulse \
    && mkdir -p /data && chown pulse:pulse /data

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code + static dashboard.
COPY pulse/ ./pulse/
COPY web/ ./web/

USER pulse

ENV PULSE_HOST=0.0.0.0 \
    PULSE_PORT=8080 \
    PULSE_DB_PATH=/data/pulse.sqlite \
    PULSE_INVENTORY=/app/inventory.yaml

EXPOSE 8080
VOLUME ["/data"]

# One worker on purpose: the prober is an in-process singleton background loop.
# Multiple workers would each start their own prober and multiply SSH load.
CMD ["uvicorn", "pulse.api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
