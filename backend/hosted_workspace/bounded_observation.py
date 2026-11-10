"""hosted_workspace.bounded_observation — P0 bounded, tenant-isolated, de-duplicated observation cycle.

Replaces the legacy SERIAL two-pass cycle (``run_hosted_observations`` + ``run_hosted_delivery_observe``, each
making its own ``executor.observe()`` per workspace) when ``HOSTED_BOUNDED_OBSERVATION_ENABLED`` is on. It:

  * observes every workspace ONCE, concurrently, in a SMALL BOUNDED worker pool (``observe_workspace_combined``
    → a single host ``OBSERVE_WORKSPACE`` per workspace, deriving BOTH the canonical observation and the delivery
    signal from that one raw result — de-duplication), so one slow/busy/unavailable tenant can never serialize
    the cycle or starve another healthy tenant's detection; each observe is bounded by the host executor's
    (flag-shortened) OBSERVE read timeout;
  * then, SERIALLY on the caller thread (the observe threads never touch the DB), ingests the canonical
    observation through the certified single writer and drives the delivery single writer on a transition.

It launches nothing, mutates no canonical state directly, arms nothing, weakens no identity pin, and never
raises into the scheduler. DARK unless the master ``hosted_persistent_mt5_enabled()`` is on (same as the legacy
path). Concurrency is bounded by :func:`observation_budget` (ADR-0090): at most ``capacity - reserved`` observes
run at once, so the fan-out can never occupy every host signed-executor permit and starve a time-critical signed
control-plane op (ACTIVATE / RESTORE) — the packet-046 failure — and never outgrows Node-2 host capacity.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings

from hosted_workspace.consumer import ingest_observation
from hosted_workspace.flags import hosted_delivery_lifecycle_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.poll_diagnostics import record_poll_unavailable
from hosted_workspace.transport_errors import HOST_OBSERVATION_CAPACITY_DEFERRED

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.bounded_observation"
_CONNECTED = "CONNECTED"
_DISCONNECTED = "DISCONNECTED"
_DEFAULT_OBSERVATION_CONCURRENCY = 4   # conservative default, safely below the executor connection cap (ADR-0090)
_DEFAULT_EXECUTOR_CAPACITY = 8         # MUST mirror the host daemon's HOSTED_EXECUTOR_MAX_CONNECTIONS (default 8)
_DEFAULT_RESERVED_SLOTS = 2            # executor connections kept free for time-critical signed control-plane ops
_HARD_MAX_WORKERS = 12                 # Node-2 host capacity ceiling — the observe fan-out cannot outgrow it


def _int_setting(name: str, default: int) -> int:
    raw = getattr(settings, name, None)
    if raw is None:
        raw = os.getenv(name, str(default))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def observation_budget() -> dict:
    """Single source of truth for hosted-observation concurrency (ADR-0090).

    The host signed-executor daemon admits at most ``capacity`` concurrent connections (its
    ``HOSTED_EXECUTOR_MAX_CONNECTIONS``, default 8) and REFUSES the rest with a bare pre-response close. If the
    observe fan-out is allowed to fill every permit, a one-shot signed control-plane op (ACTIVATE / RESTORE) is
    starved — exactly the packet-046 failure. So the backend reserves ``reserved`` permits for the control plane
    and bounds observation to at most ``capacity - reserved`` in-flight observes.

    Documented invariant:  ``1 <= effective <= capacity - reserved``  and  ``reserved >= 1``.

    Fails CLOSED against saturation: an invalid, zero, negative, or ``>= capacity`` observation setting — or a
    reserved value that would leave no headroom — never raises ``effective`` above ``capacity - reserved`` and
    never below 1. A read-only observation cycle must keep running (freezing login→CONNECTED detection would be
    the worse failure), but it may never occupy the reserved control-plane headroom. ``notes`` records every
    clamp applied, for operator evidence.

    NOTE: ``capacity`` is the backend's mirror of the host cap. If an operator raises the daemon's
    ``HOSTED_EXECUTOR_MAX_CONNECTIONS`` they MUST raise ``HOSTED_EXECUTOR_CONNECTION_CAPACITY`` to match, or the
    backend will keep observation unnecessarily low (safe, but under-utilised).
    """
    notes: list = []
    capacity = _int_setting("HOSTED_EXECUTOR_CONNECTION_CAPACITY", _DEFAULT_EXECUTOR_CAPACITY)
    reserved = _int_setting("HOSTED_CONTROL_PLANE_RESERVED_SLOTS", _DEFAULT_RESERVED_SLOTS)
    configured = _int_setting("HOSTED_OBSERVATION_MAX_WORKERS", _DEFAULT_OBSERVATION_CONCURRENCY)

    if capacity < 1:
        notes.append("capacity_invalid"); capacity = _DEFAULT_EXECUTOR_CAPACITY
    if reserved < 1:
        notes.append("reserved_invalid"); reserved = _DEFAULT_RESERVED_SLOTS
    if reserved >= capacity:                        # reserved must leave at least one observe permit
        notes.append("reserved_ge_capacity"); reserved = max(1, capacity - 1)
    max_safe = capacity - reserved                  # >= 1 by construction

    effective = configured
    if configured < 1 or configured >= capacity:    # zero / negative / >= capacity are all invalid → fail closed
        notes.append("observation_concurrency_invalid")
        effective = min(_DEFAULT_OBSERVATION_CONCURRENCY, max_safe)
    if effective > max_safe:                         # would eat into the reserved control-plane headroom
        notes.append("observation_concurrency_exceeds_headroom")
        effective = max_safe
    effective = max(1, min(effective, _HARD_MAX_WORKERS))
    return {"effective": effective, "capacity": capacity, "reserved": reserved,
            "configured": configured, "max_safe": max_safe, "notes": notes}


def _max_workers() -> int:
    """Effective hosted-observation worker count — see :func:`observation_budget`."""
    return observation_budget()["effective"]


# Reasons that mean a terminal IS present for the account but observation cannot proceed because the terminal is
# not cleanly attachable, so an onboarding account's observation is FROZEN (never advances to CONNECTED). This must
# be VISIBLE, not hidden in the generic "unavailable" bucket - a stuck onboarding account is exactly the account-37
# forensic. Only ``duplicate_terminal`` is listed because it is the ONLY such reason the live pipeline actually
# surfaces here: observe_workspace_combined derives ``reason`` from the LocalSystem host result
# (live_observe.observe_workspace_combined -> result["reason"]), and the LocalSystem corroboration emits
# ``duplicate_terminal`` when it sees >1 per-account terminal (the persistent-stray / self-fork case). The tenant
# observer's own ``guarded_attach_would_launch`` hold lives in the tenant snapshot and is NOT surfaced as the
# top-level host reason today, so listing it here would be dead code tested only by an injected token (a review
# fake). Add it only once the pipeline genuinely surfaces it.
FROZEN_REASONS = frozenset({"duplicate_terminal"})


def _empty(enabled: bool) -> dict:
    return {"enabled": enabled, "polled": 0, "applied": 0, "unavailable": 0, "errors": 0,
            "workers": 0, "reasons": {}, "frozen": 0, "deferred": 0, "deferred_reason": "",
            "delivery": {"connected": 0, "disconnected": 0, "held": 0, "cz_skipped": 0}}


def _log_frozen(ws, reason: str) -> None:
    """Emit a stable, greppable, secret-free WARNING for a frozen onboarding observation so operators/alerting can
    see a stuck account instead of it hiding in the 'unavailable' bucket. Never raises."""
    try:
        from django.utils import timezone
        last = getattr(ws, "last_decision_at", None)
        stale = int((timezone.now() - last).total_seconds()) if last is not None else -1
        logger.warning(
            "hosted_observation_frozen account=%s state=%s reason=%s stale_seconds=%s observation_version=%s",
            getattr(ws, "trading_account_id", None), getattr(ws, "canonical_state", None), reason, stale,
            getattr(ws, "observation_version", None))
    except Exception:  # noqa: BLE001 - visibility must never break the cycle
        pass


def _safe_combined(combined_fn, ws):
    """Run the combined observe for one workspace off the caller thread. Pure host-IO (the ws relations it reads
    are pre-loaded, so it does no DB query in the normal path). Any error is a sanitised (None, None, 'error') —
    never raises out of the pool. ALWAYS closes this worker thread's DB connection at the end: Django connections
    are thread-local, so any query a thread does (defensively, or a real one via an un-preloaded relation) would
    otherwise leak a connection for the pool thread's lifetime."""
    from django.db import connection
    try:
        return combined_fn(ws)   # (canonical_obs|None, delivery_sig|None, reason)
    except Exception:  # noqa: BLE001 — one workspace's observe failure must not break the cycle
        logger.warning("bounded observe failed workspace=%s", getattr(ws, "pk", None))
        return (None, None, "error")
    finally:
        connection.close()


# Onboarding-sensitive canonical states: while a workspace sits here the customer is actively waiting for the
# login→CONNECTED detection, so the scheduler re-polls ONLY these quickly within a cron invocation (fast cadence)
# without re-observing stable operational tenants every few seconds. Deliberately excludes CONNECTED and above:
# once detected, advancing further is either a manual confirm or the slower execution-readiness path.
ONBOARDING_STATES = frozenset({"PROVISIONING", "WAITING_FOR_LOGIN"})


def run_bounded_observation_cycle(*, combined_fn=None, correlation_id: str = "", source: str = SOURCE,
                                  only_states=None) -> dict:
    """One bounded, concurrent, de-duplicated observation pass. When ``only_states`` is given, observe ONLY
    workspaces whose ``canonical_state`` is in that set (used by the fast onboarding re-poll so stable tenants are
    not re-observed every few seconds). Returns a secret-free summary incl. typed reason counts and the delivery
    breakdown. Never raises into the scheduler."""
    if not hosted_persistent_mt5_enabled():
        return _empty(False)
    if combined_fn is None:
        from hosted_workspace.live_observe import observe_workspace_combined as combined_fn

    from hosted_workspace.tenant_isolation import customer_zero_account_ids
    delivery_on = hosted_delivery_lifecycle_enabled()
    cz_ids = customer_zero_account_ids() if delivery_on else frozenset()

    # Pre-load with the relations the observe reads (trading_account / execution_node) so the pooled observe
    # threads never touch the DB (they do pure signed HTTP); all DB writes stay on the caller thread below.
    qs = HostedMt5Workspace.objects.select_related("trading_account", "execution_node")
    # Skip removed/decommissioned accounts (tombstoned via disconnected_at) — a removed account must consume
    # no ongoing observation/polling. History is retained; it is simply no longer observed.
    qs = qs.filter(trading_account__disconnected_at__isnull=True)
    if only_states is not None:
        qs = qs.filter(canonical_state__in=list(only_states))
    workspaces = list(qs.all())
    budget = observation_budget()
    workers = budget["effective"]
    if budget["notes"]:
        logger.warning(
            "hosted observation budget clamped effective=%s capacity=%s reserved=%s configured=%s notes=%s",
            workers, budget["capacity"], budget["reserved"], budget["configured"], ",".join(budget["notes"]))
    # Excess observations are QUEUED behind the budget (the pool runs `workers` at a time), never dropped: every
    # workspace is still observed within this cycle. `deferred` is the peak queued behind the budget — the
    # operator-facing HOST_OBSERVATION_CAPACITY_DEFERRED condition (ADR-0090), computed from real counts only.
    deferred = max(0, len(workspaces) - workers)

    results = []   # list of (ws, canonical, delivery, reason)
    if workspaces:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hosted-obs") as pool:
            futs = {pool.submit(_safe_combined, combined_fn, ws): ws for ws in workspaces}
            for fut in as_completed(futs):
                ws = futs[fut]
                try:
                    canonical, delivery, reason = fut.result()
                except Exception:  # noqa: BLE001 — defensive; _safe_combined already sanitises
                    canonical, delivery, reason = None, None, "error"
                results.append((ws, canonical, delivery, reason))

    out = _empty(True)
    out["workers"] = workers
    out["budget"] = {k: budget[k] for k in ("capacity", "reserved", "effective", "configured", "max_safe")}
    out["deferred"] = deferred
    out["deferred_reason"] = HOST_OBSERVATION_CAPACITY_DEFERRED if deferred else ""
    reasons: dict = {}
    if delivery_on:
        from hosted_workspace.delivery_persistence import (
            record_remoteapp_connected, record_remoteapp_disconnected)

    for ws, canonical, delivery, reason in results:
        out["polled"] += 1
        reasons[reason] = reasons.get(reason, 0) + 1
        try:
            # ---- canonical single-writer ingest (monotonic version; writer rejects a stale/duplicate) ----
            if canonical is None:
                out["unavailable"] += 1
                # Per-account breadcrumb: record WHY this poll produced no observation (e.g. host_unavailable =
                # bridge unreachable on the backend->host path) on the row, so a stuck account is self-diagnosing
                # instead of looking identical to one merely waiting for login. Add-only; touches no canonical/
                # delivery/proj state and never advances observation_version. Cleared by the next success.
                record_poll_unavailable(ws, reason)
                # Frozen-observation visibility: a present-but-unattachable terminal on an onboarding account is a
                # stuck state, not routine unavailability - surface it distinctly so it can be alerted on.
                if reason in FROZEN_REASONS and str(getattr(ws, "canonical_state", "") or "") in ONBOARDING_STATES:
                    out["frozen"] += 1
                    _log_frozen(ws, reason)
            else:
                next_version = int(ws.observation_version or 0) + 1
                res = ingest_observation(ws, canonical, observation_version=next_version,
                                         correlation_id=correlation_id, source=source)
                if res is not None and res.status in ("APPLIED", "IDEMPOTENT"):
                    out["applied"] += 1
            # ---- delivery single-writer on a TRANSITION only (CZ excluded, byte-identical to the serial edge) ----
            if delivery_on:
                if ws.trading_account_id in cz_ids:
                    out["delivery"]["cz_skipped"] += 1
                else:
                    cur = str(getattr(ws, "delivery_state", "") or "")
                    seq = int(getattr(ws, "delivery_event_seq", 0) or 0) + 1
                    if delivery == _CONNECTED and cur != _CONNECTED:
                        record_remoteapp_connected(ws, event_seq=seq, correlation_id=correlation_id)
                        out["delivery"]["connected"] += 1
                    elif delivery == _DISCONNECTED and cur == _CONNECTED:
                        record_remoteapp_disconnected(ws, event_seq=seq, correlation_id=correlation_id)
                        out["delivery"]["disconnected"] += 1
                    else:
                        out["delivery"]["held"] += 1
        except Exception:  # noqa: BLE001 — one workspace's ingest must not stop the pass
            out["errors"] += 1
            logger.exception("bounded observation ingest failed workspace=%s", getattr(ws, "pk", None))
    out["reasons"] = reasons
    return out
