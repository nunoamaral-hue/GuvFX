"""
TX-1E — dedicated session DELIVERY (PILOT, NOT wired to live customer traffic).

Builds the Guacamole→RDP→guvfx_u_<id>→kiosk-MT5 descriptor, the dual-path
delivery decision (reusing the TX-1D live-readiness/fail-closed brain), and the
session-hygiene policy. Like TX-1C/1D this is dormant: no live customer launch
path imports it. Delivering customer access is blocked behind D-2/D-6/D-7.

Execution isolation: this concerns the dedicated VIEWER session only (a separate
RDP session as a non-admin user). It never references the Administrator console,
the bridge, or the execution MT5.
"""
from core.models import AuditEvent

from .models import AccountProvisioning
from . import session as routing, viewer

# ── Session hygiene policy (scoped to guvfx_u_* sessions only) ──
IDLE_TIMEOUT_MINUTES = 30
MAX_SESSION_MINUTES = 240   # 4h hard cap
HYGIENE_SCOPE_PREFIX = "guvfx_u_"


def _audit(event_type, account_id, actor=None, **meta):
    try:
        AuditEvent.objects.create(
            event_type=event_type, severity=AuditEvent.Severity.INFO,
            entity_type="terminal_provisioning", entity_id=str(account_id),
            user=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            metadata=meta,
        )
    except Exception:
        pass


def build_dedicated_session_descriptor(account_or_id) -> dict | None:
    """The Guacamole RDP + kiosk descriptor a DEDICATED session would launch.

    Returns None unless the account decisively routes DEDICATED (live readiness,
    kill-switch, enabled, populated). The password is NOT embedded — the live
    launcher injects it from AccountProvisioning.password_enc at connect time.
    """
    decision = viewer.decide_viewer_path(account_or_id)
    if decision["path"] != viewer.Path.DEDICATED:
        return None
    account = routing._resolve_account(account_or_id)
    prov = AccountProvisioning.objects.get(trading_account=account)
    from execution.models import TerminalNode
    node = TerminalNode.objects.filter(status="active").first()
    mt5_path = rf"{prov.runtime_root}\terminal\terminal64.exe"
    return {
        "transport_type": "rdp",
        "scope": "dedicated_viewer_session",
        "host": node.hostname if node else "",
        "port": "3389",
        "username": prov.windows_username,        # non-admin guvfx_u_<id>
        "domain": "",
        "security": "any",
        "ignore_cert": True,
        "credential_ref": "AccountProvisioning.password_enc",  # injected at connect; never inlined
        "kiosk": True,                            # shell = MT5, no explorer/desktop/start
        "kiosk_shell": f"{mt5_path} /portable",
        "view_only": True,                        # AllowLiveTrading=0, no EAs, Login=0
        "remote_app": False,                      # RDS RemoteApp NOT used (D-7 / RDS not installed)
        "idle_timeout_minutes": IDLE_TIMEOUT_MINUTES,
        "max_session_minutes": MAX_SESSION_MINUTES,
        "note": "pilot descriptor — NOT wired to live customer launch path",
    }


def deliver_session(account_or_id, actor=None) -> dict:
    """Dual-path delivery decision + descriptor. DEDICATED only when live-ready;
    otherwise LEGACY (fail-closed). Audited. Does not actually open a tunnel."""
    decision = viewer.decide_viewer_path(account_or_id, actor=actor)
    aid = decision["account_id"]
    if decision["path"] == viewer.Path.DEDICATED:
        desc = build_dedicated_session_descriptor(account_or_id)
        _audit(AuditEvent.EventType.TX_DEDICATED_SESSION_CREATED, aid, actor,
               windows_username=desc["username"], kiosk=True, view_only=True)
        return {"path": "DEDICATED", "reason": decision["reason"], "descriptor": desc}
    _audit(AuditEvent.EventType.TX_DEDICATED_SESSION_FALLBACK, aid, actor, reason=decision["reason"])
    return {"path": "LEGACY", "reason": decision["reason"], "descriptor": None}


def record_kiosk_enabled(account_or_id, actor=None):
    account = routing._resolve_account(account_or_id)
    prov = AccountProvisioning.objects.get(trading_account=account)
    _audit(AuditEvent.EventType.TX_KIOSK_ENABLED, account.id, actor,
           windows_username=prov.windows_username,
           kiosk_shell=rf"{prov.runtime_root}\terminal\terminal64.exe", scoped="guvfx_u_*")
    return prov


def record_session_launched(account_or_id, session_id=None, actor=None):
    account = routing._resolve_account(account_or_id)
    _audit(AuditEvent.EventType.TX_DEDICATED_SESSION_LAUNCHED, account.id, actor,
           rdp_session_id=session_id)


def hygiene_policy() -> dict:
    return {
        "idle_timeout_minutes": IDLE_TIMEOUT_MINUTES,
        "max_session_minutes": MAX_SESSION_MINUTES,
        "scope": f"{HYGIENE_SCOPE_PREFIX}* RDP sessions only",
        "excludes": ["Administrator", "console session", "bridge", "execution MT5", "services"],
        "enforcement": "Cleanup-GuvfxSessions.ps1 (Windows, scoped logoff)",
    }
