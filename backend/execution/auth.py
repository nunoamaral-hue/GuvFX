"""
Worker authentication helpers.

Validates incoming worker requests against ``WorkerIdentity`` rows using
constant-time secret comparison (``hmac.compare_digest``).
"""

from __future__ import annotations

import logging
import os
from hmac import compare_digest

from django.conf import settings as django_settings
from rest_framework.exceptions import PermissionDenied

from .models import WorkerIdentity

logger = logging.getLogger(__name__)

# The well-known worker_id used to resolve legacy X-Worker-Token requests
# to a WorkerIdentity row.  Operators must create this row before the legacy
# path will succeed.
LEGACY_WORKER_ID = "legacy-worker"


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


def authenticate_legacy_worker(provided_token: str) -> WorkerIdentity:
    """
    Authenticate a legacy ``X-Worker-Token`` request **through** WorkerIdentity trust.

    Flow:
    1. Check ``ENABLE_LEGACY_WORKER_TOKEN`` setting – reject if disabled.
    2. Compare *provided_token* against the ``MT5_WORKER_TOKEN`` env-var using
       constant-time comparison.
    3. Resolve the well-known ``legacy-worker`` WorkerIdentity row.
    4. Validate ``status == ACTIVE``.
    5. Return the WorkerIdentity (so callers can attach it to the request and
       enforce permissions identically to the modern path).

    Raises:
        PermissionDenied: on any failure (disabled, token mismatch, missing
            WorkerIdentity, revoked status).
    """
    # 1. Config gate
    if not getattr(django_settings, "ENABLE_LEGACY_WORKER_TOKEN", False):
        logger.warning("Legacy worker auth rejected: ENABLE_LEGACY_WORKER_TOKEN is disabled")
        raise PermissionDenied("Legacy worker authentication is disabled")

    # 2. Constant-time token comparison
    expected_token = os.getenv("MT5_WORKER_TOKEN", "")
    if not expected_token or not compare_digest(expected_token, provided_token):
        logger.warning("Legacy worker auth failed: token mismatch")
        raise PermissionDenied("Invalid worker credentials")

    # 3. Resolve WorkerIdentity row for legacy-worker
    try:
        worker = WorkerIdentity.objects.get(worker_id=LEGACY_WORKER_ID)
    except WorkerIdentity.DoesNotExist:
        logger.error(
            "Legacy worker auth failed: WorkerIdentity '%s' row does not exist. "
            "Create it with: WorkerIdentity.objects.create(worker_id='%s', "
            "worker_secret_hash='unused', status='ACTIVE')",
            LEGACY_WORKER_ID,
            LEGACY_WORKER_ID,
        )
        raise PermissionDenied("Legacy worker identity not configured")

    # 4. Status validation
    if worker.status != WorkerIdentity.Status.ACTIVE:
        logger.warning(
            "Legacy worker auth failed: worker_id=%s status=%s",
            LEGACY_WORKER_ID,
            worker.status,
        )
        raise PermissionDenied("Legacy worker identity revoked")

    return worker
