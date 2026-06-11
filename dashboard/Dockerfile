# Nest v1 Dashboard — tablet-first household/Raven observability container.
# Vulture runtime (bot/scheduler) stays on the host; this container observes host
# state via carefully scoped read-only mounts.
# Routes: / (Nest home)  /storage  /vulture  /advanced  /health

FROM python:3.12-slim

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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py parsers.py subprocess_util.py host_commands.py host_status.py db_readers.py log_readers.py vulture_runtime.py storage_probe.py storage_config.py ./
COPY templates/ templates/

ENV VULTURE_DB_PATH=/app/data/vulture.db \
    VULTURE_LOG_PATH=/app/logs/vulture.log \
    DASHBOARD_HOST_ROOT=/host/root \
    DASHBOARD_HOST_PROC=/host/proc

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -sf http://localhost:8088/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
