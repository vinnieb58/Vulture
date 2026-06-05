FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iproute2 \
        iputils-ping \
        procps \
        systemd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/canary/requirements.txt
COPY . /app/canary

ENV CANARY_PROJECT_ROOT=/app \
    CANARY_DATA_DIR=/app/data \
    CANARY_LOGS_DIR=/app/logs \
    CANARY_INTERVAL_SECONDS=300

CMD ["python", "-m", "canary.app"]
