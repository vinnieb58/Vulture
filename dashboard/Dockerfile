# Nest v1 Dashboard — tablet-first household/Raven observability container.
# Vulture runtime (bot/scheduler) stays on the host; this container observes host
# state via carefully scoped read-only mounts.
# Routes: / (Nest home)  /storage  /vulture  /raven/health  /advanced  /health

FROM python:3.12-slim

ARG BUILD_GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        procps \
        iproute2 \
        iputils-ping \
        tmux \
        util-linux \
        systemd \
        docker.io \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy every dashboard module so new local imports are not dropped from the image.
COPY dashboard/*.py ./
COPY kestrel/telemetry_retention.py ./telemetry_retention.py
# Replace the dev shim with the canonical Kestrel implementation.
COPY kestrel/nest_history.py ./nest_history.py
COPY dashboard/templates/ templates/
COPY dashboard/static/ static/

ENV VULTURE_DB_PATH=/app/data/vulture.db \
    VULTURE_LOG_PATH=/app/logs/vulture.log \
    KESTREL_DB_PATH=/app/data/kestrel/kestrel.db \
    DASHBOARD_HOST_ROOT=/host/root \
    DASHBOARD_HOST_PROC=/host/proc \
    DASHBOARD_BUILD_GIT_COMMIT=${BUILD_GIT_COMMIT} \
    DASHBOARD_BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8088/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
