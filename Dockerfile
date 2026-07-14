# Small, production-minded image. Single stage keeps it simple to explain;
# multi-stage would add complexity without meaningful size wins here.
FROM python:3.12-slim

# Fail fast, no .pyc, unbuffered logs so Railway shows output immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
USER appuser

# Railway injects $PORT; default to 8000 locally. Exec form for correct signals.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
