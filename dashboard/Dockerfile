# Vulture Dashboard v0.2 — read-only FastAPI status page.
# Vulture runtime (bot/scheduler) stays on the host; this container observes host
# state via carefully scoped read-only mounts.

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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py parsers.py subprocess_util.py host_commands.py host_status.py db_readers.py log_readers.py vulture_runtime.py ./
COPY templates/ templates/

ENV VULTURE_DB_PATH=/app/data/vulture.db \
    VULTURE_LOG_PATH=/app/logs/vulture.log \
    DASHBOARD_HOST_ROOT=/host/root \
    DASHBOARD_HOST_PROC=/host/proc

EXPOSE 8088

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
