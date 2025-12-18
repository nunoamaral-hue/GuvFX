FROM python:3.11-slim

WORKDIR /app
COPY mt5_worker.py /app/mt5_worker.py
COPY mt5_validate_worker.py /app/mt5_validate_worker.py

RUN pip install --no-cache-dir requests

# Default command remains the original worker
CMD ["python", "/app/mt5_worker.py"]
