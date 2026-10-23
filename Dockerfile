FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    LOG_DIR=/app/data/logs \
    WORKSPACE_DB=/app/data/marketing_workspace.sqlite3

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/samples \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=20s --start-period=40s --retries=3 \
    CMD python tools/healthcheck.py --quiet || exit 1
CMD ["python", "app.py"]

