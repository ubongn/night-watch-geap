# Night Watch gateway — container image (alternative to Cloud Run buildpacks).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY night_watch ./night_watch
COPY Procfile ./

CMD ["sh", "-c", "uvicorn night_watch.server:app --host 0.0.0.0 --port ${PORT}"]
