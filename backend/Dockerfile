FROM python:3.11-slim

# Don't write .pyc, flush stdout immediately so docker logs are live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps in a separate layer so source changes don't bust the cache.
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# Copy the rest of the backend.
COPY . /app

# Run as non-root for safety. The data directory needs to be writable by
# this user so the ingestion state file can be persisted.
RUN useradd -m -u 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Healthcheck hits the public /api/health route — no API key needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status==200 else 1)" \
  || exit 1

# Single process is fine for an MVP. Switch to "-w 4" with gunicorn
# behind a real proxy when you outgrow it.
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]