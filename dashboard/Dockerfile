# Vulture Dashboard v0.1 — read-only FastAPI status page.
# Vulture runtime (bot/scheduler) stays on the host; this container only reads
# bind-mounted data and log files.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

ENV VULTURE_DB_PATH=/app/data/vulture.db \
    VULTURE_LOG_PATH=/app/logs/vulture.log

EXPOSE 8088

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
