"""hosted_workspace.poll_diagnostics — per-account observation-unavailability breadcrumb (ADD-ONLY, fail-open).

Why this exists. When an observation cycle cannot reach a workspace's bridge, the host executor returns
``{"ok": False, "reason": "host_unavailable"}`` and the canonical M3c writer (``persist_workspace_decision``)
NEVER runs — it only writes on a successful, non-``None`` observation. So, without this, a brand-new account whose
bridge port is reachable on the host but blocked on the backend→host network path leaves NO per-row trace of WHY:
``observation_version`` stays at 1, ``last_reason`` stays "", ``last_observed_at`` stays null — byte-identical to an
account that is simply waiting for the member to log in. That ambiguity is exactly what forced manual hand-tracing of
account #52 (and 50/51 before it): the UI falls back to "Ready — log in in MT5" while the real cause is an
unreachable bridge.

This records the unavailability ``reason`` on the row as a pure diagnostic breadcrumb so operators/alerting can tell a
backend→bridge network block apart from a genuine "waiting for login", WITHOUT touching any canonical/delivery/proj
state, the ``observation_version`` monotonic sequence, the first-launch latch, or ANY order/identity gate. A single
``UPDATE`` of ONLY ``last_reason`` via a filtered queryset — it can neither advance a state machine nor clobber a
concurrent canonical write (it writes one column the canonical writer clears on its next success). The next SUCCESSFUL
observation clears it (see ``persistence.persist_workspace_decision``), so the breadcrumb self-heals on recovery.

Security: ``last_reason`` is a short, stable, secret-free code (already surfaced in ``HostedMt5Workspace.to_dict``).
No credential, username, path, port or host is written.
"""
from __future__ import annotations

import logging

from hosted_workspace.models import HostedMt5Workspace

logger = logging.getLogger("guvfx.hosted_workspace")

_MAXLEN = 64  # matches HostedMt5Workspace.last_reason max_length


def record_poll_unavailable(workspace, reason: str) -> bool:
    """Persist ``reason`` on ``workspace.last_reason`` as an observation-unavailable breadcrumb.

    Scoped, atomic, idempotent and fail-open:
      * writes ONLY ``last_reason`` (a filtered queryset ``.update()`` — takes no row lock, bumps no ``updated_at``,
        touches no other column), so it can never race or clobber the canonical/delivery single writers;
      * writes only when the stored reason actually CHANGES (no per-minute churn for a persistently-stuck account);
      * never advances ``observation_version``/``canonical_state``/``delivery_state``/``proj_*``/``broker_ever_matched``
        and is never an order or identity gate;
      * never raises into the scheduler.
    Returns whether a row was written.
    """
    try:
        pk = getattr(workspace, "pk", None)
        if pk is None:
            return False
        value = (str(reason or "").strip() or "host_unavailable")[:_MAXLEN]
        if str(getattr(workspace, "last_reason", "") or "") == value:
            return False
        updated = HostedMt5Workspace.objects.filter(pk=pk).update(last_reason=value)
        if updated:
            # Keep the in-memory row consistent so the rest of this cycle sees the recorded value.
            workspace.last_reason = value
        return bool(updated)
    except Exception:  # noqa: BLE001 — a diagnostic breadcrumb must never break the observation cycle
        logger.warning("record_poll_unavailable failed workspace=%s", getattr(workspace, "pk", None))
        return False
