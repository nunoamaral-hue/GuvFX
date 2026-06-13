"""
TX-1D — dedicated viewer session routing (PILOT INFRASTRUCTURE, NOT wired live).

This module is the dual-path routing brain + runtime-population recorder. It is
exercised by the `viewer_session` management command and evidence only. NOTHING
in the live Guacamole/VNC customer launch path imports it — routing live
customer traffic is the cutover, gated behind a separate authorization (D-2/D-6).

Safety properties:
  - FAIL-CLOSED: any error, kill-switch off, not-ready, not-populated, or not
    enabled → LEGACY path. A customer can never be routed to a broken viewer.
  - LIVE readiness: the decision re-evaluates readiness at call time (F-1 fix),
    never trusting a cached value.
  - Execution untouched: this concerns the VIEWER only; the bridge / execution
    MT5 on the Administrator console is a separate concern and is not referenced.
"""
import os

from django.utils import timezone

from core.models import AuditEvent

from .models import AccountProvisioning, SessionAssignment
from . import services, session as routing

KILL_SWITCH_ENV = "TX1D_DEDICATED_VIEWER_ENABLED"
GOLDEN_MT5_BUILD = "5.0.0.5833"


class Path:
    DEDICATED = "DEDICATED"
    LEGACY = "LEGACY"


def dedicated_path_enabled() -> bool:
    """Global kill-switch. Default OFF → every decision is LEGACY."""
    return os.getenv(KILL_SWITCH_ENV, "false").strip().lower() in ("1", "true", "yes", "on")


def _audit(event_type, account_id, actor=None, **meta):
    try:
        AuditEvent.objects.create(
            event_type=event_type, severity=AuditEvent.Severity.INFO,
            entity_type="terminal_provisioning", entity_id=str(account_id),
            user=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            metadata=meta,
        )
    except Exception:  # fail-open audit
        pass


def mark_runtime_populated(account_or_id, version=GOLDEN_MT5_BUILD, actor=None) -> AccountProvisioning:
    """Record that the per-account viewer MT5 runtime was populated on the host
    (the actual file copy is performed by Populate-GuvfxViewerRuntime.ps1)."""
    account = routing._resolve_account(account_or_id)
    prov = AccountProvisioning.objects.get(trading_account=account)
    prov.runtime_populated = True
    prov.runtime_version = version
    prov.save(update_fields=["runtime_populated", "runtime_version", "updated_at"])
    _audit(AuditEvent.EventType.TX_VIEWER_RUNTIME_POPULATED, account.id, actor,
           windows_username=prov.windows_username, runtime_root=prov.runtime_root,
           runtime_version=version, view_only=True)
    return prov


def decide_viewer_path(account_or_id, actor=None) -> dict:
    """LIVE dual-path decision. Returns {path, reason, account_id, checks}.

    DEDICATED requires: kill-switch ON, assignment exists + enabled, runtime
    populated, and a LIVE readiness verdict of READY. Anything else → LEGACY.
    Fail-closed on any exception.
    """
    account = routing._resolve_account(account_or_id)
    aid = account.id if account else account_or_id
    try:
        ks = dedicated_path_enabled()
        verdict = routing.evaluate_readiness(account) if account else {"state": "INVALID", "reason": "account_not_found"}
        prov = AccountProvisioning.objects.filter(trading_account=account).first() if account else None
        sa = SessionAssignment.objects.filter(trading_account=account).first() if account else None
        checks = {
            "kill_switch_on": ks,
            "assignment_exists": sa is not None,
            "assignment_enabled": bool(sa and sa.enabled),
            "runtime_populated": bool(prov and prov.runtime_populated),
            "live_readiness": verdict["state"],
        }
        dedicated = (ks and checks["assignment_enabled"]
                     and checks["runtime_populated"]
                     and verdict["state"] == SessionAssignment.Readiness.READY)
        if dedicated:
            path, reason = Path.DEDICATED, "ready"
        elif not ks:
            path, reason = Path.LEGACY, "kill_switch_off"
        elif not checks["assignment_enabled"]:
            path, reason = Path.LEGACY, "assignment_not_enabled"
        elif not checks["runtime_populated"]:
            path, reason = Path.LEGACY, "runtime_not_populated"
        else:
            path, reason = Path.LEGACY, f"not_ready:{verdict['reason']}"
    except Exception as e:  # FAIL-CLOSED
        path, reason, checks = Path.LEGACY, f"error_failclosed:{type(e).__name__}", {}

    _audit(AuditEvent.EventType.TX_LIVE_READINESS_EVALUATED, aid, actor, path=path, reason=reason, checks=checks)
    if path == Path.LEGACY:
        _audit(AuditEvent.EventType.TX_FALLBACK_TO_LEGACY, aid, actor, reason=reason)
    return {"path": path, "reason": reason, "account_id": aid, "checks": checks}


def build_dedicated_descriptor(account_or_id) -> dict | None:
    """Generate the descriptor a DEDICATED viewer session WOULD launch — RDP to
    the per-account non-admin identity + its viewer runtime. Generated for
    pilot prep/evidence only; NOT handed to live customer traffic. Returns None
    unless the account decisively routes DEDICATED."""
    decision = decide_viewer_path(account_or_id)
    if decision["path"] != Path.DEDICATED:
        return None
    account = routing._resolve_account(account_or_id)
    prov = AccountProvisioning.objects.get(trading_account=account)
    from execution.models import TerminalNode
    node = TerminalNode.objects.filter(status="active").first()
    return {
        "transport_type": "rdp",
        "scope": "dedicated_viewer",
        "host": node.hostname if node else "",
        "windows_username": prov.windows_username,        # non-admin identity
        "runtime_root": prov.runtime_root,
        "mt5_path": rf"{prov.runtime_root}\terminal\terminal64.exe",
        "view_only": True,                                 # no autotrade / no EAs
        "remote_app_capable": False,                       # RDS RemoteApp = later (RDS not installed)
        "note": "pilot descriptor — not wired to live customer launch path",
    }


def set_kill_switch_audit(enabled: bool, actor=None):
    """Audit a kill-switch state change (the actual flag is an env var set at
    deploy time; this records intent/observation)."""
    _audit(
        AuditEvent.EventType.TX_DEDICATED_PATH_ENABLED if enabled
        else AuditEvent.EventType.TX_DEDICATED_PATH_DISABLED,
        0, actor, kill_switch=enabled, ts=timezone.now().isoformat(),
    )


def demonstrate_rollback(account_or_id, actor=None) -> dict:
    """Demonstrate dedicated→legacy rollback for a test account by disabling its
    assignment and showing the decision flips to LEGACY. Audited. Re-enables
    afterward so the pilot fixture is left ready."""
    account = routing._resolve_account(account_or_id)
    before = decide_viewer_path(account)
    routing.set_enabled(account, False)            # rollback action
    after = decide_viewer_path(account)
    restored = None
    if before["path"] == Path.DEDICATED:
        routing.set_enabled(account, True)         # restore pilot fixture
        restored = decide_viewer_path(account)["path"]
    result = {"account_id": account.id, "before": before["path"],
              "after_disable": after["path"], "restored": restored}
    _audit(AuditEvent.EventType.TX_ROLLBACK_DEMONSTRATED, account.id, actor,
           before=result["before"], after_disable=result["after_disable"], restored=restored)
    return result
