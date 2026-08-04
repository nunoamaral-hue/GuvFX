"""WP5.1 — the deterministic Operational Summary Service (ADR-0032).

Builds a per-account operational summary by combining:
  (a) LIVE authoritative state, read READ-ONLY from the WP1A/WP3/WP2 sources — ``validation_status`` /
      ``validated_at`` on the account, ``reliability.broker_health.get_contract`` (flag-gated),
      ``execution.runtime_pause.pause_state`` / ``is_broker_paused``, ``password_enc`` presence, and
      ``disconnected_at``; with
  (b) AGGREGATES over the OperationalEvent timeline — latest error, latest warning, latest validation
      event, and event counts.

It MUTATES NOTHING and is deterministic given the DB state (only ``generated_at`` reflects wall-clock).
Each cross-source live read is defensive (fail-open) so a failing dependency cannot raise into the
summary, and the flag-gated sources return ``None``/``False`` when their capability is DARK — treated
here as "not observed", not an error.

Visibility: when ``customer_only`` is set (the API passes ``customer_only=not is_staff``), EVERY event
aggregate — latest validation/error/warning and the counts — is scoped to ``customer_visible=True`` so
a non-staff owner's summary never discloses operator-only event content. The live *state* fields are the
customer's own account posture and are always shown.
"""
from __future__ import annotations

import logging

from django.db.models import Count
from django.utils import timezone

from .constants import OPEN_SEVERITIES, SEV_CRITICAL, SEV_ERROR, SEV_WARNING
from .dto import OperationalEventDTO, OperationalSummaryDTO
from .models import OperationalEvent

logger = logging.getLogger("guvfx.operational_events")


def _iso(dt):
    return dt.isoformat() if dt else None


def _validation_state(account) -> dict:
    return {
        "status": getattr(account, "validation_status", "") or "",
        "validated_at": _iso(getattr(account, "validated_at", None)),
    }


def _health_state(account) -> dict:
    # Flag-gated live read; None when the WP3 health engine is DARK or no row exists yet.
    try:
        from reliability.broker_health import get_contract
        contract = get_contract(account)
    except Exception:
        logger.exception("operational summary: health read failed")
        contract = None
    if not contract:
        return {"state": "UNKNOWN", "available": False}
    return {
        "state": contract.get("state", "UNKNOWN"),
        "eligible": contract.get("eligible"),
        "pause_required": contract.get("pause_required"),
        "reason_code": contract.get("reason_code", ""),
        "state_version": contract.get("state_version"),
        "updated_at": contract.get("updated_at"),
        "available": True,
    }


def _runtime_pause(account) -> dict:
    try:
        from execution.runtime_pause import is_broker_paused, pause_state
        durable = pause_state(account)      # durable snapshot, ungated (None when no row)
        live = is_broker_paused(account)    # live bool, needs BOTH broker-connectivity flags
    except Exception:
        logger.exception("operational summary: pause read failed")
        durable, live = None, False
    if durable is None:
        return {"paused": bool(live), "live_paused": bool(live), "record": None}
    return {
        "paused": bool(durable.get("paused")),
        "live_paused": bool(live),
        "reason_code": durable.get("reason_code", ""),
        "record": durable,
    }


def _credential_status(account) -> dict:
    present = bool(getattr(account, "password_enc", "") or "")
    return {"present": present, "state": "present" if present else "missing"}


def _disconnect_state(account) -> dict:
    dt = getattr(account, "disconnected_at", None)
    return {"disconnected": dt is not None, "disconnected_at": _iso(dt)}


def build_operational_summary(account, *, customer_only=False) -> OperationalSummaryDTO:
    from .query import OperationalQueryService as Q

    # The base set the aggregates read. Scoping it once here means EVERY timeline aggregate below —
    # latest_error/warning, counts, and the last-event timestamp — respects the visibility boundary.
    events = OperationalEvent.objects.filter(account=account)
    if customer_only:
        events = events.filter(customer_visible=True)

    # Event-timeline aggregates (all DB-side; safe and bounded on an empty table).
    latest_validation = Q.latest_in_category(account, "VALIDATION", customer_only=customer_only)

    err_row = events.filter(severity__in=(SEV_ERROR, SEV_CRITICAL)).order_by("-created_at", "-id").first()
    latest_error = OperationalEventDTO.from_model(err_row).as_dict() if err_row else None

    warn_row = events.filter(severity=SEV_WARNING).order_by("-created_at", "-id").first()
    latest_warning = OperationalEventDTO.from_model(warn_row).as_dict() if warn_row else None

    # order_by(field) makes the aggregate dict key-order deterministic across calls on identical state.
    by_sev = {r["severity"]: r["n"]
              for r in events.values("severity").annotate(n=Count("id")).order_by("severity")}
    by_cat = {r["category"]: r["n"]
              for r in events.values("category").annotate(n=Count("id")).order_by("category")}
    open_count = events.filter(resolved=False, severity__in=OPEN_SEVERITIES).count()
    event_counts = {
        "total": sum(by_sev.values()),
        "open": open_count,
        "by_severity": by_sev,
        "by_category": by_cat,
    }

    # last_update = the most recent timestamp across the authoritative live state and the timeline.
    candidates = []
    latest_ev = events.order_by("-created_at", "-id").first()
    if latest_ev and latest_ev.created_at:
        candidates.append(latest_ev.created_at)
    for dt in (getattr(account, "validated_at", None),
               getattr(account, "disconnected_at", None),
               getattr(account, "updated_at", None)):
        if dt:
            candidates.append(dt)
    last_update = _iso(max(candidates)) if candidates else None

    return OperationalSummaryDTO(
        account_id=account.id,
        generated_at=timezone.now().isoformat(),
        validation_state=_validation_state(account),
        health_state=_health_state(account),
        runtime_pause=_runtime_pause(account),
        credential_status=_credential_status(account),
        disconnect_state=_disconnect_state(account),
        latest_validation=latest_validation.as_dict() if latest_validation else None,
        latest_error=latest_error,
        latest_warning=latest_warning,
        event_counts=event_counts,
        last_update=last_update,
    )
