"""
Worker authentication helpers.

Validates incoming worker requests against ``WorkerIdentity`` rows using
constant-time secret comparison (``hmac.compare_digest``).
"""

from __future__ import annotations

import logging
from hmac import compare_digest

from rest_framework.exceptions import PermissionDenied

from .models import WorkerIdentity

logger = logging.getLogger(__name__)


def authenticate_worker(worker_id: str, worker_secret: str) -> WorkerIdentity:
    """
    Authenticate a worker by *worker_id* + *worker_secret*.

    Returns the ``WorkerIdentity`` instance on success.

    Raises:
        PermissionDenied: if the worker is unknown, inactive, or the secret
            does not match.
    """
    try:
        worker = WorkerIdentity.objects.get(worker_id=worker_id)
    except WorkerIdentity.DoesNotExist:
        logger.warning("Worker auth failed: unknown worker_id=%s", worker_id)
        raise PermissionDenied("Invalid worker credentials")

    if worker.status != WorkerIdentity.Status.ACTIVE:
        logger.warning("Worker auth failed: worker_id=%s status=%s", worker_id, worker.status)
        raise PermissionDenied("Worker inactive")

    provided_hash = WorkerIdentity.hash_secret(worker_secret)
    if not compare_digest(provided_hash, worker.worker_secret_hash):
        logger.warning("Worker auth failed: secret mismatch for worker_id=%s", worker_id)
        raise PermissionDenied("Invalid worker credentials")

    return worker
