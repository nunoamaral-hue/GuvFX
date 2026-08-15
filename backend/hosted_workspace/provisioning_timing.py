"""hosted_workspace.provisioning_timing — Beta UX Correction (Sponsor 2026-08-15).

Fail-open per-stage timing of a hosted-workspace provisioning run + a read-only duration helper. The Sponsor
wants the FIRST real provisioning run measured (total + per-stage duration) to decide the provisioning-page UX
(keep the user on a live page vs allow-leave vs async "workspace ready" email). This is deliberately a tiny,
self-contained table + recorder + reader: it never touches the certified single state writer, never emits an
operational event, and NEVER blocks provisioning (``record_stage_timing`` swallows every error). No credential
is ever recorded — only a stable stage label + the completion timestamp.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("guvfx.hosted_workspace")

# Canonical stage labels — the measured points of the first real run. The four mid-stages mirror the
# ``slot_preparation`` ``ST_*`` completion points; the outer three are the request/allocate/awaiting-login edges.
STAGE_REQUEST_RECEIVED = "request_received"
STAGE_NODE_ALLOCATED = "node_allocated"
STAGE_IDENTITY_CREATED = "identity_created"
STAGE_RUNTIME_MATERIALISED = "runtime_materialised"
STAGE_ACL_COMPLETE = "acl_complete"
STAGE_REMOTEAPP_PUBLISHED = "remoteapp_published"
STAGE_ORDER_BRIDGE_ACTIVATED = "order_bridge_activated"
STAGE_WAITING_FOR_LOGIN = "waiting_for_login"


def record_stage_timing(workspace, stage) -> None:
    """Append ONE timing row for ``stage`` on ``workspace``. FAIL-OPEN: never raises — a timing failure must not
    perturb provisioning. Idempotent per (workspace, stage): the first completion wins (a retried provisioning
    cycle does not double-count), enforced by the model's unique constraint via ``get_or_create``."""
    try:
        from hosted_workspace.models import ProvisioningStageTiming
        wid = getattr(workspace, "pk", None)
        if wid is None:
            return
        ProvisioningStageTiming.objects.get_or_create(workspace_id=wid, stage=str(stage)[:48])
    except Exception:  # noqa: BLE001 — timing is best-effort; a failure NEVER blocks provisioning
        logger.debug("hosted provisioning timing: record failed for stage=%s", stage, exc_info=False)


def stage_timings_for(workspace) -> dict:
    """Read-only timing summary for one workspace: ordered stage rows with per-stage deltas (seconds since the
    previous recorded stage) + the total (last − first). Returns an empty summary on any error / no rows."""
    try:
        from hosted_workspace.models import ProvisioningStageTiming
        wid = getattr(workspace, "pk", workspace)
        rows = list(ProvisioningStageTiming.objects.filter(workspace_id=wid)
                    .order_by("recorded_at", "id").values("stage", "recorded_at"))
        if not rows:
            return {"stages": [], "total_seconds": None}
        stages = []
        prev = None
        for r in rows:
            delta = (r["recorded_at"] - prev).total_seconds() if prev is not None else 0.0
            stages.append({"stage": r["stage"], "recorded_at": r["recorded_at"].isoformat(),
                           "delta_seconds": round(delta, 3)})
            prev = r["recorded_at"]
        total = (rows[-1]["recorded_at"] - rows[0]["recorded_at"]).total_seconds()
        return {"stages": stages, "total_seconds": round(total, 3)}
    except Exception:  # noqa: BLE001
        return {"stages": [], "total_seconds": None}
