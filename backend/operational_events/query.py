"""WP5.1 — the central Operational Query Service (ADR-0032).

The single place ORM access to ``OperationalEvent`` is expressed for reads; callers use these methods
and never build raw querysets. Every method returns DTOs (or the summary DTO) — never model instances —
so ORM rows never leak past this boundary. Ownership is enforced by the CALLER (the API view); these
methods are owner-agnostic and operate on an already-authorised account.
"""
from __future__ import annotations

from .constants import (
    DEFAULT_RECENT_LIMIT, DEFAULT_TIMELINE_LIMIT, MAX_TIMELINE_LIMIT, OPEN_SEVERITIES,
)
from .dto import OperationalEventDTO
from .models import OperationalEvent

_ORDER = ("-created_at", "-id")


def _clamp_limit(limit, default, maximum=MAX_TIMELINE_LIMIT) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, maximum)


def _clamp_offset(offset) -> int:
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _base(account):
    return OperationalEvent.objects.filter(account=account)


class OperationalQueryService:
    """Read-only projection queries over the operational-event timeline."""

    @staticmethod
    def timeline(account, *, limit=DEFAULT_TIMELINE_LIMIT, offset=0, category=None,
                 customer_only=False):
        qs = _base(account)
        if category:
            qs = qs.filter(category=str(category))
        if customer_only:
            qs = qs.filter(customer_visible=True)
        limit = _clamp_limit(limit, DEFAULT_TIMELINE_LIMIT)
        offset = _clamp_offset(offset)
        rows = list(qs.order_by(*_ORDER)[offset:offset + limit])
        return [OperationalEventDTO.from_model(r) for r in rows]

    @staticmethod
    def recent(account, *, limit=DEFAULT_RECENT_LIMIT, customer_only=False):
        return OperationalQueryService.timeline(
            account, limit=_clamp_limit(limit, DEFAULT_RECENT_LIMIT),
            offset=0, customer_only=customer_only)

    @staticmethod
    def latest_in_category(account, category, *, customer_only=False):
        qs = _base(account).filter(category=str(category))
        if customer_only:
            qs = qs.filter(customer_visible=True)
        row = qs.order_by(*_ORDER).first()
        return OperationalEventDTO.from_model(row) if row else None

    @staticmethod
    def latest_of_type(account, event_type, *, customer_only=False):
        qs = _base(account).filter(event_type=str(event_type))
        if customer_only:
            qs = qs.filter(customer_visible=True)
        row = qs.order_by(*_ORDER).first()
        return OperationalEventDTO.from_model(row) if row else None

    @staticmethod
    def open_events(account, *, customer_only=False):
        qs = _base(account).filter(resolved=False, severity__in=OPEN_SEVERITIES)
        if customer_only:
            qs = qs.filter(customer_visible=True)
        rows = list(qs.order_by(*_ORDER)[:MAX_TIMELINE_LIMIT])
        return [OperationalEventDTO.from_model(r) for r in rows]

    @staticmethod
    def customer_visible(account, *, limit=DEFAULT_TIMELINE_LIMIT, offset=0, category=None):
        return OperationalQueryService.timeline(
            account, limit=limit, offset=offset, category=category, customer_only=True)

    @staticmethod
    def operator_visible(account, *, limit=DEFAULT_TIMELINE_LIMIT, offset=0, category=None):
        return OperationalQueryService.timeline(
            account, limit=limit, offset=offset, category=category, customer_only=False)

    @staticmethod
    def summary(account, *, customer_only=False):
        # Imported lazily to avoid any import cycle with the summary module (which imports live state).
        # customer_only MUST be threaded through so the summary's event aggregates honour the same
        # visibility boundary as the timeline (a non-staff owner never sees operator-only event content).
        from .summary import build_operational_summary
        return build_operational_summary(account, customer_only=customer_only)
