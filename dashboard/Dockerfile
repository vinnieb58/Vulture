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

COPY dashboard/app.py dashboard/parsers.py dashboard/subprocess_util.py dashboard/host_commands.py dashboard/host_status.py dashboard/host_cpu_metrics.py dashboard/glances_client.py dashboard/raven_health_details.py dashboard/db_readers.py dashboard/log_readers.py dashboard/kestrel_status.py dashboard/kestrel_formatting.py dashboard/kestrel_metrics.py dashboard/house_status.py dashboard/house_formatting.py dashboard/vulture_runtime.py dashboard/storage_probe.py dashboard/storage_config.py dashboard/raven_metrics_history.py dashboard/metrics_sampler.py dashboard/nest_history.py dashboard/nest_hvac_runtime.py dashboard/nest_hvac_formatting.py dashboard/nest_energy_correlation.py dashboard/nest_collection_health.py dashboard/tuya_power_status.py dashboard/tuya_power_history.py dashboard/tuya_power_error_status.py dashboard/tuya_power_formatting.py ./
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
