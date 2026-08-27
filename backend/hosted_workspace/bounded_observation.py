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
path). Concurrency is bounded to at most Node-2 capacity so it never fans out unboundedly against the host.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings

from hosted_workspace.consumer import ingest_observation
from hosted_workspace.flags import hosted_delivery_lifecycle_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.bounded_observation"
_CONNECTED = "CONNECTED"
_DISCONNECTED = "DISCONNECTED"
_DEFAULT_MAX_WORKERS = 8
_HARD_MAX_WORKERS = 12   # never exceed Node-2 max_accounts — the observe fan-out cannot outgrow host capacity


def _max_workers() -> int:
    raw = getattr(settings, "HOSTED_OBSERVATION_MAX_WORKERS", None)
    if raw is None:
        raw = os.getenv("HOSTED_OBSERVATION_MAX_WORKERS", str(_DEFAULT_MAX_WORKERS))
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        n = _DEFAULT_MAX_WORKERS
    return max(1, min(n, _HARD_MAX_WORKERS))


def _empty(enabled: bool) -> dict:
    return {"enabled": enabled, "polled": 0, "applied": 0, "unavailable": 0, "errors": 0,
            "workers": 0, "reasons": {},
            "delivery": {"connected": 0, "disconnected": 0, "held": 0, "cz_skipped": 0}}


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
    if only_states is not None:
        qs = qs.filter(canonical_state__in=list(only_states))
    workspaces = list(qs.all())
    workers = _max_workers()

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
