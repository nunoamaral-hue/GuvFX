"""core.operational_evidence — repeatable Operational Readiness evidence collection (ADR-0035).

Turns the three read-only Operational Readiness views (health rollup, pre-flight, rollback plan) into a
schema-conformant **evidence manifest** (``evidence/schema/evidence-manifest.schema.json``) so the
readiness posture is captured as machine-readable evidence, not prose, and can be re-collected verbatim
on demand. Deterministic: the caller supplies the git/time facts (so the pure builder never shells out or
reads the wall-clock); everything else is derived from the live read-only checks. MUTATES NOTHING.

The manifest ``status`` is honest about the standing gate:
  * ``FAIL``    — pre-flight found a hard prerequisite missing (verdict NOT_READY)
  * ``PARTIAL`` — repository readiness holds but an external Sponsor/host gate remains (BLOCKED_ON_SPONSOR)
  * ``PASS``    — everything a repository can prove is in place (READY / READY_WITH_WARNINGS)
"""
from __future__ import annotations

from core.operational_health import build_operational_health
from core.preflight import run_preflight
from core.rollback_planner import plan_rollback

SCHEMA_VERSION = "1.0.0"

_STATUS_FROM_VERDICT = {
    "NOT_READY": "FAIL",
    "BLOCKED_ON_SPONSOR": "PARTIAL",
    "READY_WITH_WARNINGS": "PASS",
    "READY": "PASS",
}


def build_operational_evidence(*, packet_id: str, handoff_id: str, created_at_utc: str,
                               branch: str, base_commit: str, head_commit=None,
                               reviewer=None) -> dict:
    """Build a schema-conformant Operational Readiness evidence manifest. Pure/read-only — all runtime
    facts (packet_id/handoff_id/time/git) are injected by the caller; the checks themselves read the live
    system without mutating it."""
    health = build_operational_health()
    preflight = run_preflight()
    rollback = plan_rollback()

    verdict = preflight["verdict"]
    status = _STATUS_FROM_VERDICT.get(verdict, "PARTIAL")

    commands = [
        "python manage.py operational_health --json",
        "python manage.py hosted_workspace_preflight --json",
        "python manage.py rollback_plan --json",
    ]
    expected_results = [
        "operational health rollup with no unexpected fault subsystems (dark subsystems AWAITING_SPONSOR)",
        "pre-flight verdict is at worst BLOCKED_ON_SPONSOR (no hard NOT_READY prerequisite missing)",
        "rollback plan lists only flag-disable steps (no destructive DB rollback)",
    ]
    actual_results = [
        f"overall={health['overall']}; faults={health['fault_count']}; "
        f"awaiting_sponsor={len(health['awaiting_sponsor'])}; counts={health['counts_by_state']}",
        f"verdict={verdict}; blocking={[b['id'] for b in preflight['blocking']]}",
        f"posture={rollback['posture']}; armed_flags={rollback['armed_flags']}; "
        f"destructive_steps={any(s['destructive'] for s in rollback['rollback_steps'])}",
    ]
    limitations = [
        "Read-only repository posture only: no live Windows/RDS/RemoteApp/guacd host was probed.",
        "The disposable-host RDS/RemoteApp certification and the Sponsor flag enablement are external "
        "gates this manifest cannot satisfy (reported BLOCKED/AWAITING_SPONSOR, never PASS).",
        "Component-level health (workers/bridge/MT5) reflects recorded ComponentHealth rows only; absent "
        "rows read as DEGRADED/unobserved, never HEALTHY.",
    ]
    artefact_locations = [
        "backend/core/operational_health.py",
        "backend/core/preflight.py",
        "backend/core/rollback_planner.py",
        "docs/operations/operational-readiness/README.md",
        "docs/ADRs/0035-operational-readiness.md",
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "packet_id": packet_id,
        "created_at_utc": created_at_utc,
        "branch": branch,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "commands": commands,
        "expected_results": expected_results,
        "actual_results": actual_results,
        "status": status,
        "limitations": limitations,
        "artefact_locations": artefact_locations,
        "checksums": {},
        "reviewer": reviewer,
        # Additional (schema allows extra keys) — the full read-only payloads for auditability.
        "operational_health": health,
        "preflight": preflight,
        "rollback_plan": rollback,
    }
