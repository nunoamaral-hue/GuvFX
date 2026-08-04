"""WP5.1 — the single operational-event RECORDING service (ADR-0032).

``record_event`` is the ONE write entry point for the operational-event read model. It is:
  * DARK by default — a no-op returning ``None`` unless ``operations_events_enabled()``.
  * FAIL-OPEN — recording never raises into the caller (mirrors ``core.audit.log_event``).
  * IDEMPOTENT — when a non-empty ``dedup_key`` is supplied, the same key is inserted at most once
    (enforced by a partial unique constraint + get_or_create; safe under concurrency).
  * SECRET-SAFE — ``metadata`` is defensively sanitised (key denylist); the model carries only
    non-secret, allow-listed operational fields.

SCOPE (ADR-0032): this packet builds the recorder; it does NOT yet wire the existing broker sources
(validation / health / gate / pause / resume / credential / disconnect) to call it. That source wiring
is a separate, later increment — see ADR-0032 §Future work. Building it here without wiring keeps WP5.1
strictly within the packet's hard boundary (no runtime / validation / execution behaviour change).
"""
from __future__ import annotations

import logging

from django.utils import timezone

from .constants import (
    REDACTED, SECRET_KEY_MARKERS, default_customer_visible, normalize_severity,
    operations_events_enabled,
)
from .dto import OperationalEventDTO
from .models import OperationalEvent

logger = logging.getLogger("guvfx.operational_events")


def _sanitize_metadata(value):
    """Recursively redact denylisted keys (defensive; the model is non-secret by contract). Mirrors
    ``core.audit._sanitize_metadata`` so operational metadata can never persist a mislabelled secret."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(marker in kl for marker in SECRET_KEY_MARKERS):
                out[k] = REDACTED
            else:
                out[k] = _sanitize_metadata(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata(v) for v in value]
    return value


def record_event(*, category, event_type, severity="INFO", account=None, source="",
                  summary="", reason_code="", status="", correlation_id="",
                  state_version=None, runtime_uuid="", actor="",
                  customer_visible=None, metadata=None, dedup_key="") -> "OperationalEventDTO | None":
    """Record ONE operational event and return its DTO. No-op (``None``) when DARK. Never raises.

    ``customer_visible`` defaults per-category (constants.CUSTOMER_VISIBLE_DEFAULT) when left ``None``.
    ``severity`` is normalised into INFO/WARNING/ERROR/CRITICAL. A non-empty ``dedup_key`` makes the call
    idempotent (returns the existing event without inserting a duplicate).
    """
    try:
        if not operations_events_enabled():
            return None
        sev = normalize_severity(severity)
        vis = (default_customer_visible(category) if customer_visible is None
               else bool(customer_visible))
        clean_meta = _sanitize_metadata(dict(metadata or {}))
        # Truncate free-form strings defensively to the column widths (never raise on over-length input).
        fields = dict(
            account=account,
            runtime_uuid=str(runtime_uuid or "")[:64],
            category=str(category or "")[:16],
            event_type=str(event_type or "")[:64],
            severity=sev,
            status=str(status or "")[:32],
            reason_code=str(reason_code or "")[:64],
            summary=str(summary or "")[:255],
            source=str(source or "")[:64],
            correlation_id=str(correlation_id or "")[:128],
            state_version=state_version,
            actor=str(actor or "")[:128],
            customer_visible=vis,
            metadata=clean_meta,
        )
        key = str(dedup_key or "")[:200]
        if key:
            # get_or_create wraps the insert in a savepoint and recovers from a concurrent duplicate.
            ev, _created = OperationalEvent.objects.get_or_create(dedup_key=key, defaults=fields)
        else:
            ev = OperationalEvent.objects.create(dedup_key="", **fields)
        return OperationalEventDTO.from_model(ev)
    except Exception:  # fail-open — recording must never break the caller
        logger.exception("operational_events.record_event failed")
        return None


def mark_resolved(*, account=None, dedup_key="", category=None, event_type=None, now=None) -> int:
    """Mark matching UNRESOLVED events resolved; return the count updated. No-op (0) when DARK; never
    raises. At least one of ``account``/``dedup_key`` must be given (guards against a global resolve)."""
    try:
        if not operations_events_enabled():
            return 0
        if account is None and not dedup_key:
            return 0
        qs = OperationalEvent.objects.filter(resolved=False)
        if account is not None:
            qs = qs.filter(account=account)
        if dedup_key:
            qs = qs.filter(dedup_key=str(dedup_key))
        if category:
            qs = qs.filter(category=str(category))
        if event_type:
            qs = qs.filter(event_type=str(event_type))
        return qs.update(resolved=True, resolved_at=now or timezone.now())
    except Exception:
        logger.exception("operational_events.mark_resolved failed")
        return 0
