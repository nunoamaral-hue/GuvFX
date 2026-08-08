"""ADR-0034 Execution Engine (G2) — scheduled observation → decision → persistence driver (DARK).

A THIN driver over the certified chain (agent → snapshot → observation → M3a manager → M3c writer). It
selects eligible Hosted Workspaces, obtains an observation via the CERTIFIED agent path (injected, so the
driver itself never attaches/launches/logs in), and ingests it through the SINGLE authoritative consumer
(``consumer.ingest_observation``) — which derives the decision and persists it, advancing
``last_decision_at`` so readiness freshness holds. The driver derives no state, executes no trades, switches
no account, arms nothing, and touches no Windows/MT5. Concurrency + staleness are handled by Workspace
Core's ``select_for_update`` + monotonic ``observation_version`` (a second/duplicate poll is rejected there).

DARK: a no-op returning an empty summary while ``hosted_persistent_mt5_enabled()`` is OFF.
"""
from __future__ import annotations

import logging

from hosted_workspace.consumer import ingest_observation
from hosted_workspace.flags import hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.observation_runner"


def run_hosted_observations(*, observe_fn, correlation_id: str = "", source: str = SOURCE) -> dict:
    """Poll every Hosted Workspace once. ``observe_fn(workspace) -> WorkspaceObservation | None`` obtains a
    fresh observation via the certified agent path (None ⇒ observation unavailable this cycle → fail-closed:
    nothing is ingested, so the workspace's freshness lapses naturally and readiness stops execution).
    Returns a secret-free summary. Never raises into the scheduler (fail-open per workspace)."""
    if not hosted_persistent_mt5_enabled():
        return {"enabled": False, "polled": 0, "applied": 0, "unavailable": 0, "errors": 0}

    polled = applied = unavailable = errors = 0
    for ws in HostedMt5Workspace.objects.all().iterator():
        polled += 1
        try:
            obs = observe_fn(ws)
            if obs is None:
                unavailable += 1
                continue
            # Monotonic per-workspace observation version; the writer rejects an out-of-order/duplicate one.
            next_version = int(ws.observation_version or 0) + 1
            result = ingest_observation(ws, obs, observation_version=next_version,
                                        correlation_id=correlation_id, source=source)
            if result is not None and result.status in ("APPLIED", "IDEMPOTENT"):
                applied += 1
        except Exception:  # noqa: BLE001 — one workspace's failure must not stop the poll cycle
            errors += 1
            logger.exception("hosted observation poll failed for workspace=%s", getattr(ws, "pk", None))
    return {"enabled": True, "polled": polled, "applied": applied,
            "unavailable": unavailable, "errors": errors}
