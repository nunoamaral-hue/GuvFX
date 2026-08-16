"""hosted_workspace.delivery_observe_runner — BB#1 (Sponsor 2026-08-16): the delivery-CONNECTED producer.

The scheduled cycle's delivery edge. For every ELIGIBLE non-Customer-Zero hosted workspace it reads the TRUSTED,
tenant-unforgeable LocalSystem session signal (``live_observe.observe_remoteapp_session``) and drives the
EXISTING single delivery-state writer (``delivery_persistence.record_remoteapp_connected`` /
``record_remoteapp_disconnected``). It NEVER writes ``delivery_state`` directly (single-writer invariant), NEVER
trusts a client self-report or RemoteApp publication alone, and NEVER arms/logs-in/trades.

Transition-only + fail-closed: it calls the writer ONLY on an actual change (session up while not CONNECTED →
CONNECTED; session affirmatively down while CONNECTED → DISCONNECTED), so a steady session does not churn
telemetry, and it HOLDS state on any ambiguous/unavailable cycle (``observe_remoteapp_session`` → ``None``).

DARK: a no-op returning an empty summary unless ``hosted_delivery_lifecycle_enabled()``; and even armed, the
underlying observe transport is itself gated (cert/supervised + host-executor config), so it contacts no host
until the whole chain is deliberately enabled. Customer Zero is excluded THREE ways here: the explicit
``customer_zero_account_ids`` skip below, the host dispatcher's reserved-identity refusal, and the observer's
own supervised carve-out (CZ yields ``None``).
"""
from __future__ import annotations

import logging

from hosted_workspace.flags import hosted_delivery_lifecycle_enabled
from hosted_workspace.models import HostedMt5Workspace

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.delivery_observe_runner"

_CONNECTED = "CONNECTED"
_DISCONNECTED = "DISCONNECTED"


def _empty(enabled: bool) -> dict:
    return {"enabled": enabled, "polled": 0, "connected": 0, "disconnected": 0,
            "held": 0, "cz_skipped": 0, "errors": 0}


def run_hosted_delivery_observe(*, observe_session_fn=None, correlation_id: str = "", source: str = SOURCE) -> dict:
    """Poll every eligible non-CZ hosted workspace once, driving the delivery single writer on a TRANSITION.
    ``observe_session_fn(workspace) -> "CONNECTED" | "DISCONNECTED" | None`` (injected for tests; defaults to the
    live signal). Returns a secret-free summary. Never raises into the scheduler (fail-open per workspace)."""
    if not hosted_delivery_lifecycle_enabled():
        return _empty(False)

    from hosted_workspace.delivery_persistence import (
        record_remoteapp_connected, record_remoteapp_disconnected)
    from hosted_workspace.tenant_isolation import customer_zero_account_ids

    if observe_session_fn is None:
        from hosted_workspace.live_observe import observe_remoteapp_session as observe_session_fn

    cz_ids = customer_zero_account_ids()
    out = _empty(True)
    for ws in HostedMt5Workspace.objects.all().iterator():
        # CZ exclusion FIRST — never even attempt an observe for Customer Zero (byte-identical delivery_state).
        if ws.trading_account_id in cz_ids:
            out["cz_skipped"] += 1
            continue
        out["polled"] += 1
        try:
            sig = observe_session_fn(ws)
            cur = str(getattr(ws, "delivery_state", "") or "")
            # Monotonic per-workspace seq: stored + 1. The cycle is singleton-locked (advisory lock in the
            # command) so no concurrent producer races this, and the writer re-locks + rejects ``<= stored`` as
            # the backstop. Only allocated on an actual transition, so it never runs ahead needlessly.
            seq = int(getattr(ws, "delivery_event_seq", 0) or 0) + 1
            if sig == _CONNECTED and cur != _CONNECTED:
                record_remoteapp_connected(ws, event_seq=seq, correlation_id=correlation_id)
                out["connected"] += 1
            elif sig == _DISCONNECTED and cur == _CONNECTED:
                record_remoteapp_disconnected(ws, event_seq=seq, correlation_id=correlation_id)
                out["disconnected"] += 1
            else:
                out["held"] += 1
        except Exception:  # noqa: BLE001 — one workspace's failure must not stop the cycle
            out["errors"] += 1
            logger.exception("hosted delivery-observe failed for workspace=%s", getattr(ws, "pk", None))
    return out
