FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps (psycopg2 + build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    fonts-noto-cjk \
  && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt && pip install gunicorn
# App code
COPY . /app

# Build provenance (IPR Area G). Injected at `docker build` time — the image is baked from a non-git
# rsync tree, so it cannot self-discover the commit at runtime (no git binary in the base image).
# All three are NON-SECRET (commit sha / timestamp / release id); default "unknown" degrades gracefully
# when build-args are absent. Placed AFTER `COPY . /app` so the pip layer cache is not busted.
# Surfaced by the staff-only GET /api/version/ and via `docker inspect` (OCI labels).
ARG GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown
ARG RELEASE_ID=unknown
ENV GUVFX_GIT_COMMIT=$GIT_COMMIT \
    GUVFX_BUILD_TIMESTAMP=$BUILD_TIMESTAMP \
    GUVFX_RELEASE_ID=$RELEASE_ID
LABEL org.opencontainers.image.revision=$GIT_COMMIT \
      org.opencontainers.image.created=$BUILD_TIMESTAMP

# Security: create non-root user
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Run Django via gunicorn.
# --timeout MUST exceed the synchronous VALIDATE_LOGIN budget, or gunicorn kills the worker mid-request and
# the browser's fetch() rejects with a raw "Failed to fetch". The backend VALIDATE_LOGIN read timeout is 175s
# (terminal_provisioning.beta_worker.OP_TIMEOUTS), which itself exceeds the Agent's 165s result-wait floor
# (login 120s + cleanup 45s). 190s > 175s leaves margin for connect + response handling.
CMD ["gunicorn", "guvfx_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "190"]
