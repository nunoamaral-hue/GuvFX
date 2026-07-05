# Wayond read-only listener image = the backend image + Telethon.
# Telethon is intentionally kept OUT of the base API/worker image (leaner); the
# listener is the only service that needs it. Build from the repo root:
#   docker build -f deploy/wayond-listener/Dockerfile \
#     --build-arg BACKEND_IMAGE=guvfx-backend:latest -t guvfx-wayond-listener:latest .
ARG BACKEND_IMAGE=guvfx-backend:latest
FROM ${BACKEND_IMAGE}

# Pinned to match backend/requirements-telegram.txt.
RUN pip install --no-cache-dir "telethon>=1.36,<2"
