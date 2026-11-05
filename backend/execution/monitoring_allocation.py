"""execution.monitoring_allocation — deterministic monitoring-route allocation + lifecycle (ADR-0075).

GFX-MT5-BRIDGE-SCALE-001, Phase 4 (allocator half) + the runtime-identity invariant from
`.claude/rules/architecture`. Every mutating operation:

  * runs inside a row-locking ``transaction.atomic`` (the DB unique constraints are the final backstop);
  * is deterministic (lowest-free local port, reproducible);
  * is restart-safe (durable rows; a reboot reconstructs identically) and reclaimable (a TOMBSTONED route
    frees its local port for a new occupancy, at a bumped generation);
  * fails CLOSED on any missing/ambiguous binding or capacity exhaustion;
  * asserts the occupancy-identity invariant before acting and QUARANTINES on any disagreement
    (``(host_key, local_port, generation)`` is one occupancy; generation increments by exactly one on
    release after TOMBSTONE and never decreases, repeats, or skips). The quarantine is written WITHIN the
    same locked transaction and the integrity error is raised only AFTER that transaction commits, so it
    persists for every current caller — all of which (reconcile, onboarding) run in autocommit and catch
    the error. A violation is never silently repaired. (Precondition: a caller that wraps a lifecycle op
    in its OWN ``atomic`` and then rolls that transaction back would also discard the quarantine — such a
    caller must treat the raised ``MonitoringIntegrityError`` as terminal for its transaction; no current
    caller wraps these ops.)

Nothing here contacts a host, places an order, arms execution, or changes a Tailscale policy — it only
records the durable routing authority the (separately deployed, DARK) gateway is configured from.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from execution.endpoint_service import PORT_RANGE_END, PORT_RANGE_START, RESERVED_PORTS

logger = logging.getLogger("guvfx.execution.monitoring")

# ── stable, secret-free allocation reason codes ──
ALLOC_OK = "route_allocated"
ALLOC_REACTIVATED = "route_reactivated"
ALLOC_POOL_EXHAUSTED = "route_pool_exhausted"
ALLOC_CONTENTION = "route_alloc_contention"
ALLOC_NO_GATEWAY = "route_no_gateway"
ALLOC_CAPACITY_EXCEEDED = "route_host_capacity_exceeded"
ALLOC_SUBSYSTEM_DISABLED = "monitoring_subsystem_disabled"
ALLOC_INTEGRITY = "route_integrity_violation"
ALLOC_IDENTITY_MISMATCH = "bridge_identity_mismatch"
ALLOC_QUARANTINED = "route_quarantined_requires_operator"

_ALLOC_MAX_ATTEMPTS = 6


class MonitoringError(Exception):
    """Fail-closed allocation/lifecycle condition carrying a stable, secret-free ``reason``."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason


class MonitoringIntegrityError(MonitoringError):
    """A permanent occupancy-identity integrity failure. The route is QUARANTINED, never silently
    repaired (`.claude/rules/architecture`). Requires operator intervention."""


class _Quarantine(Exception):
    """Internal signal raised (BEFORE any write) inside a locked block when the route must be quarantined."""

    def __init__(self, detail: str, reason: str = ALLOC_INTEGRITY):
        super().__init__(reason)
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True)
class RouteResult:
    ok: bool
    reason: str
    route_id: int | None = None
    local_port: int | None = None
    generation: int | None = None


def _enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_gateway_enabled
        return monitoring_gateway_enabled()
    except Exception:  # noqa: BLE001 — absence ⇒ disabled
        return False


def ensure_gateway(host_key: str, *, gateway_host: str = "", gateway_port: int | None = None,
                   display_name: str = "", actor: str = "monitoring_gateway"):
    """Idempotently record the per-physical-host gateway row (REGISTERED). Does NOT health-attest it and
    does NOT contact the host. Returns the ``PhysicalHostGateway``. Fail-closed if the subsystem is off."""
    if not _enabled():
        raise MonitoringError(ALLOC_SUBSYSTEM_DISABLED)
    from execution.monitoring_models import PhysicalHostGateway

    host_key = str(host_key or "").strip()
    if not host_key:
        raise MonitoringError(ALLOC_NO_GATEWAY, "empty host_key")
    with transaction.atomic():
        gw, created = PhysicalHostGateway.objects.select_for_update().get_or_create(
            host_key=host_key,
            defaults=dict(gateway_host=gateway_host or host_key, gateway_port=gateway_port,
                          gateway_base_url=(f"http://{gateway_host or host_key}:{gateway_port}"
                                            if gateway_port else ""),
                          display_name=display_name, last_reason="gateway_registered"),
        )
        if not created and gateway_port and not gw.gateway_port:
            # Fill in the endpoint once known, without ever flipping an attested gateway's identity.
            gw.gateway_host = gateway_host or gw.gateway_host or host_key
            gw.gateway_port = gateway_port
            gw.gateway_base_url = f"http://{gw.gateway_host}:{gateway_port}"
            gw.save(update_fields=["gateway_host", "gateway_port", "gateway_base_url", "updated_at"])
    return gw


def _free_local_port(host_key: str) -> int:
    """Lowest free local port in [PORT_RANGE_START, PORT_RANGE_END] for ``host_key`` — excluding reserved
    ports and ports held by a non-tombstoned route on that host. Deterministic (lowest-first). Must be
    called inside the allocating transaction with candidate rows locked; the DB unique constraint on
    (host_key, local_port) among active routes is the final backstop."""
    from execution.monitoring_models import MonitoringRoute

    taken = set(
        MonitoringRoute.objects.exclude(state=MonitoringRoute.State.TOMBSTONED)
        .filter(host_key=host_key).values_list("local_port", flat=True)
    )
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port in RESERVED_PORTS or port in taken:
            continue
        return port
    raise MonitoringError(ALLOC_POOL_EXHAUSTED, f"host={host_key}")


def _next_generation(host_key: str, local_port: int) -> int:
    """The next occupancy generation for a SLOT ``(host_key, local_port)``: max existing generation on
    that slot + 1 (or 1). Monotonic by construction across ALL occupants of the slot (not per-account);
    the ``uniq_slot_generation`` constraint guarantees no reuse (`.claude/rules/architecture`)."""
    from execution.monitoring_models import MonitoringRoute

    prev = (MonitoringRoute.objects.filter(host_key=host_key, local_port=local_port)
            .aggregate(m=Max("allocation_generation"))["m"] or 0)
    return int(prev) + 1


def _effective_ceiling(host_key: str, gateway) -> int | None:
    """Effective host-wide route ceiling = min(gateway.max_routes or ∞, HostCapacity.max_total_occupants
    or ∞). Returns None when neither is set. Never OVERRIDES the certified ADR-0069 physical-host
    capacity; it only narrows it."""
    ceilings = []
    if gateway and gateway.max_routes:
        ceilings.append(int(gateway.max_routes))
    try:
        from execution.models import HostCapacity
        hc = HostCapacity.objects.filter(physical_host_key=host_key).first()
        if hc and hc.max_total_occupants:
            ceilings.append(int(hc.max_total_occupants))
    except Exception:  # noqa: BLE001
        pass
    return min(ceilings) if ceilings else None


def _active_route_count(host_key: str) -> int:
    from execution.monitoring_models import MonitoringRoute
    return (MonitoringRoute.objects.filter(host_key=host_key)
            .exclude(state=MonitoringRoute.State.TOMBSTONED).count())


def reserve_route(trading_account, *, gateway, actor: str = "monitoring_gateway",
                  workspace_uuid=None, execution_endpoint=None) -> RouteResult:
    """Reserve (or return the existing active) monitoring route for ``trading_account`` on ``gateway``'s
    physical host. Idempotent. A new route is created RESERVED (NOT routable) at the next monotonic
    generation, on the lowest-free local port, under a row-locked transaction; a lost port race is retried
    with a fresh scan. Fails CLOSED on pool exhaustion or a certified host-capacity ceiling."""
    if not _enabled():
        raise MonitoringError(ALLOC_SUBSYSTEM_DISABLED)
    from execution.monitoring_models import MonitoringRoute

    if gateway is None:
        raise MonitoringError(ALLOC_NO_GATEWAY)
    host_key = gateway.host_key
    account_id = trading_account.id if hasattr(trading_account, "id") else int(trading_account)

    from execution.monitoring_models import PhysicalHostGateway
    last_exc = None
    for _attempt in range(_ALLOC_MAX_ATTEMPTS):
        try:
            with transaction.atomic():
                # Lock the host's gateway row FIRST — one row every reservation on this physical host must
                # contend on, so the capacity ceiling and the port scan are race-free even when the host
                # starts empty (TOCTOU fix). This serializes reservations per host (the DB unique
                # constraint remains the final backstop).
                PhysicalHostGateway.objects.select_for_update().get(pk=gateway.pk)
                existing = (MonitoringRoute.objects.select_for_update()
                            .filter(trading_account_id=account_id)
                            .exclude(state=MonitoringRoute.State.TOMBSTONED).first())
                if existing is not None:
                    if existing.state == MonitoringRoute.State.QUARANTINED:
                        # A quarantined route is a fail-closed integrity state — never silently reused or
                        # reported as a successful reservation; it requires operator intervention.
                        raise MonitoringError(ALLOC_QUARANTINED,
                                              f"account={account_id} route={existing.pk}")
                    return RouteResult(True, ALLOC_REACTIVATED, route_id=existing.pk,
                                       local_port=existing.local_port,
                                       generation=existing.allocation_generation)
                ceiling = _effective_ceiling(host_key, gateway)
                if ceiling is not None and _active_route_count(host_key) >= ceiling:
                    raise MonitoringError(ALLOC_CAPACITY_EXCEEDED, f"host={host_key} ceiling={ceiling}")
                port = _free_local_port(host_key)
                gen = _next_generation(host_key, port)
                wsuuid = workspace_uuid or _derive_workspace_uuid(trading_account)
                route = MonitoringRoute.objects.create(
                    trading_account_id=account_id, gateway=gateway, host_key=host_key,
                    local_port=port, allocation_generation=gen, workspace_uuid=wsuuid,
                    execution_endpoint=execution_endpoint,
                    state=MonitoringRoute.State.RESERVED,
                    readiness=MonitoringRoute.Readiness.UNKNOWN,
                    last_reason_code="route_reserved", reserved_at=timezone.now())
                _audit(route, actor, "reserve", prev_state="", new_state=route.state,
                       prev_generation=None, new_generation=gen, reason=ALLOC_OK)
                logger.info("monitoring route reserved account=%s host=%s port=%s gen=%s actor=%s",
                            account_id, host_key, port, gen, actor)
                return RouteResult(True, ALLOC_OK, route_id=route.pk, local_port=port, generation=gen)
        except IntegrityError as e:
            last_exc = e
            continue  # port/account race — retry with a fresh scan
    raise MonitoringError(ALLOC_CONTENTION, f"host={host_key} after {_ALLOC_MAX_ATTEMPTS} attempts: {last_exc}")


def _derive_workspace_uuid(trading_account):
    try:
        ws = getattr(trading_account, "hosted_workspace", None)
        return getattr(ws, "workspace_uuid", None)
    except Exception:  # noqa: BLE001
        return None


# ── occupancy-identity integrity + the locked-mutation runner ──

def _check_drift(fresh, route) -> str | None:
    """Return a drift detail if the locked row's slot identity disagrees with the owning gateway's host
    or the caller's in-memory generation; else None."""
    gw_host = fresh.gateway.host_key if fresh.gateway_id else ""
    if fresh.host_key != gw_host or fresh.allocation_generation != route.allocation_generation:
        return (f"identity_drift host={fresh.host_key} gw={gw_host} "
                f"gen_db={fresh.allocation_generation} gen_mem={route.allocation_generation}")
    return None


def _run_locked(route, fn):
    """Lock the route, assert occupancy-identity integrity, then run ``fn(fresh)`` in the SAME transaction.
    On drift (or an ``fn``-raised ``_Quarantine`` before any write) the route is QUARANTINED WITHIN this
    same locked transaction and the ``MonitoringIntegrityError`` is raised only AFTER the ``with`` block
    commits — so the quarantine persists in the caller's transaction context. In autocommit (every current
    caller) that commit is immediate and durable. A caller that instead wraps this in its own ``atomic``
    and discards that transaction would discard the quarantine too, so a transactional caller must treat
    the raised error as terminal for its transaction (no current caller wraps these ops)."""
    from execution.monitoring_models import MonitoringRoute
    pending = None
    with transaction.atomic():
        fresh = MonitoringRoute.objects.select_for_update().get(pk=route.pk)
        drift = _check_drift(fresh, route)
        if drift:
            _write_quarantine(fresh, drift, ALLOC_INTEGRITY)
            pending = (ALLOC_INTEGRITY, route.pk)
        else:
            try:
                return fn(fresh)
            except _Quarantine as q:
                _write_quarantine(fresh, q.detail, q.reason)  # fn raises BEFORE any write
                pending = (q.reason, route.pk)
    # The atomic committed the quarantine; now surface the integrity failure (never silently repaired).
    raise MonitoringIntegrityError(pending[0], f"route={pending[1]}")


def _write_quarantine(r, detail: str, reason: str = ALLOC_INTEGRITY):
    """Mark an ALREADY-LOCKED route QUARANTINED + append an audit row, within the caller's current
    transaction. Persists with the surrounding commit; a violation is never silently repaired."""
    from execution.monitoring_models import MonitoringRoute
    prev = r.state
    r.state = MonitoringRoute.State.QUARANTINED
    r.readiness = MonitoringRoute.Readiness.MONITORING_UNAVAILABLE
    r.last_reason_code = reason
    r.last_failure_reason = (detail or "")[:64]
    r.save(update_fields=["state", "readiness", "last_reason_code", "last_failure_reason", "updated_at"])
    _audit(r, "monitoring_gateway", "quarantine", prev_state=prev, new_state=r.state,
           prev_generation=r.allocation_generation, new_generation=r.allocation_generation,
           reason=reason, detail={"detail": (detail or "")[:200]})
    logger.error("monitoring route QUARANTINED route=%s %s", r.pk, detail)


def register_route(route, *, execution_endpoint=None, local_port=None,
                   actor: str = "monitoring_gateway") -> RouteResult:
    """RESERVED → REGISTERED: bind the physical local endpoint (and optionally correct the local port).
    Idempotent (REGISTERED/VERIFIED/READY stay)."""
    from execution.monitoring_models import MonitoringRoute

    def _fn(r):
        if r.state in (MonitoringRoute.State.REGISTERED, MonitoringRoute.State.VERIFIED,
                       MonitoringRoute.State.READY):
            return RouteResult(True, "already_registered", route_id=r.pk, local_port=r.local_port,
                               generation=r.allocation_generation)
        if r.state != MonitoringRoute.State.RESERVED:
            raise MonitoringError("route_not_reservable", f"state={r.state}")
        prev = r.state
        if execution_endpoint is not None:
            r.execution_endpoint = execution_endpoint
        if local_port is not None:
            r.local_port = int(local_port)
        r.state = MonitoringRoute.State.REGISTERED
        r.registered_at = timezone.now()
        r.last_reason_code = "route_registered"
        r.save(update_fields=["execution_endpoint", "local_port", "state", "registered_at",
                              "last_reason_code", "updated_at"])
        _audit(r, actor, "register", prev_state=prev, new_state=r.state,
               prev_generation=r.allocation_generation, new_generation=r.allocation_generation)
        return RouteResult(True, "route_registered", route_id=r.pk, local_port=r.local_port,
                           generation=r.allocation_generation)
    return _run_locked(route, _fn)


def verify_route(route, *, attested: dict, actor: str = "monitoring_gateway") -> RouteResult:
    """REGISTERED → VERIFIED on an identity match; QUARANTINE on a mismatch (fail-closed). ``attested`` is
    the gateway's attested per-route identity: {login, server, trade_mode, is_demo, capabilities}. The
    attested (login, server) must equal the account's expected identity; a mismatch is an integrity fault,
    never a silently-accepted re-home."""
    from execution.monitoring_models import MonitoringRoute

    def _fn(r):
        if r.state not in (MonitoringRoute.State.REGISTERED, MonitoringRoute.State.VERIFIED,
                           MonitoringRoute.State.READY):
            raise MonitoringError("route_not_verifiable", f"state={r.state}")
        acct = r.trading_account
        exp_login = str(getattr(acct, "account_number", "") or "").strip()
        att_login = str(attested.get("login") or "").strip()
        exp_server = str(
            (acct.broker_server.server_name if getattr(acct, "broker_server_id", None) else "")
            or getattr(acct, "broker_name", "") or "").strip()
        att_server = str(attested.get("server") or "").strip()
        if not exp_login or att_login != exp_login or att_server.lower() != exp_server.lower():
            raise _Quarantine(
                f"identity_mismatch login_exp={_mask(exp_login)} login_att={_mask(att_login)} "
                f"server_match={att_server.lower() == exp_server.lower()}",
                reason=ALLOC_IDENTITY_MISMATCH)
        prev = r.state
        r.state = MonitoringRoute.State.VERIFIED
        r.verified_login = att_login
        r.verified_server = att_server
        r.verified_trade_mode = attested.get("trade_mode")
        r.verified_is_demo = attested.get("is_demo")
        caps = attested.get("capabilities")
        if isinstance(caps, (list, tuple)):
            r.capability_set = list(caps)
        r.verified_at = r.verified_at or timezone.now()
        r.last_verified_at = timezone.now()
        r.last_reason_code = "route_verified"
        r.save()
        _audit(r, actor, "verify", prev_state=prev, new_state=r.state,
               prev_generation=r.allocation_generation, new_generation=r.allocation_generation)
        return RouteResult(True, "route_verified", route_id=r.pk, local_port=r.local_port,
                           generation=r.allocation_generation)
    return _run_locked(route, _fn)


def set_readiness(route, *, readiness: str, reason_code: str, health_state: str | None = None,
                  make_ready: bool = False, actor: str = "monitoring_gateway") -> RouteResult:
    """Update the route's readiness/health + durable reason code (the Phase-5 contract output). When
    ``make_ready`` and the route is VERIFIED and readiness is MONITORING_READY, promote to READY; a
    non-ready readiness never promotes (fail-closed). Lifecycle state is otherwise unchanged."""
    from execution.monitoring_models import MonitoringRoute

    def _fn(r):
        prev = r.state
        r.readiness = readiness
        r.last_reason_code = reason_code
        if health_state is not None:
            r.health_state = health_state
            r.last_health_at = timezone.now()
        if make_ready and r.state == MonitoringRoute.State.VERIFIED \
                and readiness == MonitoringRoute.Readiness.MONITORING_READY:
            r.state = MonitoringRoute.State.READY
        r.save()
        if r.state != prev:
            _audit(r, actor, "set_ready", prev_state=prev, new_state=r.state,
                   prev_generation=r.allocation_generation, new_generation=r.allocation_generation,
                   reason=reason_code)
        return RouteResult(True, reason_code, route_id=r.pk, local_port=r.local_port,
                           generation=r.allocation_generation)
    return _run_locked(route, _fn)


def drain_route(route, *, actor: str = "monitoring_gateway") -> RouteResult:
    """Move a route to DRAINING (graceful retire-in-progress; still occupies its port until tombstoned)."""
    from execution.monitoring_models import MonitoringRoute

    def _fn(r):
        if r.state == MonitoringRoute.State.TOMBSTONED:
            return RouteResult(True, "already_tombstoned", route_id=r.pk)
        prev = r.state
        r.state = MonitoringRoute.State.DRAINING
        r.last_reason_code = "route_draining"
        r.save(update_fields=["state", "last_reason_code", "updated_at"])
        _audit(r, actor, "drain", prev_state=prev, new_state=r.state,
               prev_generation=r.allocation_generation, new_generation=r.allocation_generation)
        return RouteResult(True, "route_draining", route_id=r.pk)
    return _run_locked(route, _fn)


def tombstone_route(route, *, reason: str = "route_tombstoned", actor: str = "monitoring_gateway") -> RouteResult:
    """Retire a route (TOMBSTONED) and free its local port for a future occupancy at a bumped generation.
    Idempotent. The generation does NOT change here — it increments on the NEXT ``reserve_route`` for the
    account (which is the 'release' that bumps it by exactly one)."""
    from execution.monitoring_models import MonitoringRoute

    def _fn(r):
        if r.state == MonitoringRoute.State.TOMBSTONED:
            return RouteResult(True, "already_tombstoned", route_id=r.pk,
                               generation=r.allocation_generation)
        prev = r.state
        r.state = MonitoringRoute.State.TOMBSTONED
        r.readiness = MonitoringRoute.Readiness.MONITORING_UNAVAILABLE
        r.last_reason_code = reason
        r.tombstoned_at = timezone.now()
        r.save(update_fields=["state", "readiness", "last_reason_code", "tombstoned_at", "updated_at"])
        _audit(r, actor, "tombstone", prev_state=prev, new_state=r.state,
               prev_generation=r.allocation_generation, new_generation=r.allocation_generation,
               reason=reason)
        logger.info("monitoring route tombstoned account=%s host=%s port=%s gen=%s actor=%s",
                    r.trading_account_id, r.host_key, r.local_port, r.allocation_generation, actor)
        return RouteResult(True, reason, route_id=r.pk, generation=r.allocation_generation)
    return _run_locked(route, _fn)


def _audit(route, actor, event, *, prev_state="", new_state="", prev_generation=None,
           new_generation=None, reason="", detail=None):
    from execution.monitoring_models import MonitoringRouteAudit
    MonitoringRouteAudit.objects.create(
        route=route, actor=actor, event=event, prev_state=prev_state, new_state=new_state,
        prev_generation=prev_generation, new_generation=new_generation, reason=reason,
        detail=detail or {})


def _mask(login) -> str:
    s = str(login or "")
    return ("••" + s[-4:]) if len(s) >= 4 else "••"
