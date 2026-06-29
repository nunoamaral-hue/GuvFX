"""
TX-1C — session assignment & runtime routing (DORMANT).

Deterministic readiness evaluation + the routing record that future customer
session routing will resolve through. Nothing here is wired into the live
Guacamole/VNC launch path — customer traffic stays on the legacy Administrator
path. Additive only. Reuses core.AuditEvent and the TX-1B provisioning record.
"""
from django.db import transaction
from django.utils import timezone

from core.models import AuditEvent
from trading.models import TradingAccount

from .models import AccountProvisioning, SessionAssignment
from . import services


class SessionAssignmentError(Exception):
    """Controlled routing failure (no provisioning, invalid account, etc.)."""


def _resolve_account(account_or_id):
    if isinstance(account_or_id, TradingAccount):
        return account_or_id
    return TradingAccount.objects.filter(pk=account_or_id).first()


def evaluate_readiness(account_or_id) -> dict:
    """Deterministic, side-effect-free readiness verdict.

    Returns {state, assignable, checks{}, reason}. Never raises — an unknown
    account yields INVALID so readiness is always observable.
    """
    account = _resolve_account(account_or_id)
    if account is None:
        return {"state": SessionAssignment.Readiness.INVALID, "assignable": False,
                "checks": {"account_exists": False}, "reason": "account_not_found"}

    prov = AccountProvisioning.objects.filter(trading_account=account).first()
    checks = {
        "account_exists": True,
        "mapping_exists": prov is not None,
        "identity_present": bool(prov and prov.windows_username),
        "runtime_present": bool(prov and prov.runtime_root),
        "identity_materialized": bool(prov and prov.identity_materialized),
        "runtime_materialized": bool(prov and prov.runtime_materialized),
        "status_provisioned": bool(prov and prov.status == AccountProvisioning.Status.PROVISIONED),
        "non_admin": bool(prov and not prov.is_admin),
        "mapping_integrity": bool(
            prov
            and prov.windows_username == services.username_for(account.id)
            and prov.runtime_root == services.runtime_root_for(account.id)
        ),
    }

    if not checks["mapping_exists"]:
        return {"state": SessionAssignment.Readiness.INVALID, "assignable": False,
                "checks": checks, "reason": "no_provisioning_mapping"}
    if not checks["mapping_integrity"]:
        return {"state": SessionAssignment.Readiness.INVALID, "assignable": False,
                "checks": checks, "reason": "mapping_integrity_violation"}

    operational = ["identity_present", "runtime_present", "identity_materialized",
                   "runtime_materialized", "status_provisioned", "non_admin"]
    failed = [k for k in operational if not checks[k]]
    if failed:
        return {"state": SessionAssignment.Readiness.NOT_READY, "assignable": False,
                "checks": checks, "reason": "not_ready:" + ",".join(failed)}

    return {"state": SessionAssignment.Readiness.READY, "assignable": True,
            "checks": checks, "reason": "ready"}


def _audit(event_type, account, sa=None, actor=None, **extra):
    meta = {"account_id": account.id}
    if sa is not None:
        meta.update({
            "windows_username": sa.windows_username,
            "runtime_root": sa.runtime_root,
            "readiness": sa.readiness,
            "eligible": sa.eligible,
            "enabled": sa.enabled,
        })
    meta.update(extra)
    try:
        AuditEvent.objects.create(
            event_type=event_type, severity=AuditEvent.Severity.INFO,
            entity_type="terminal_provisioning", entity_id=str(account.id),
            user=actor if (actor and getattr(actor, "is_authenticated", False)) else None,
            metadata=meta,
        )
    except Exception:  # fail-open
        pass


@transaction.atomic
def assign(account_or_id, actor=None) -> SessionAssignment:
    """Idempotently create/refresh the routing record for an account.

    Requires a TX-1B provisioning mapping (else controlled failure). Does NOT
    enable routing — eligibility reflects readiness; enabling is explicit.
    """
    account = _resolve_account(account_or_id)
    if account is None:
        raise SessionAssignmentError("account_not_found")
    prov = AccountProvisioning.objects.filter(trading_account=account).first()
    if prov is None:
        raise SessionAssignmentError(f"no provisioning mapping for account {account.id}; run TX-1B first")

    verdict = evaluate_readiness(account)
    sa = SessionAssignment.objects.select_for_update().filter(trading_account=account).first()
    created = sa is None
    if created:
        sa = SessionAssignment(trading_account=account, provisioning=prov)

    sa.provisioning = prov
    sa.windows_username = prov.windows_username
    sa.runtime_root = prov.runtime_root
    prev_readiness = None if created else sa.readiness
    sa.readiness = verdict["state"]
    sa.readiness_detail = verdict["checks"] | {"reason": verdict["reason"]}
    sa.eligible = verdict["assignable"]
    sa.last_readiness_at = timezone.now()
    sa.save()

    if created:
        _audit(AuditEvent.EventType.TX_SESSION_MAPPING_CREATED, account, sa, actor)
    else:
        _audit(AuditEvent.EventType.TX_SESSION_MAPPING_UPDATED, account, sa, actor)
    if prev_readiness != sa.readiness:
        _audit(AuditEvent.EventType.TX_SESSION_READINESS_CHANGED, account, sa, actor,
               previous=prev_readiness, current=sa.readiness)
    return sa


@transaction.atomic
def refresh_readiness(account_or_id, actor=None) -> SessionAssignment:
    account = _resolve_account(account_or_id)
    sa = SessionAssignment.objects.select_for_update().get(trading_account=account)
    verdict = evaluate_readiness(account)
    prev = sa.readiness
    sa.readiness = verdict["state"]
    sa.readiness_detail = verdict["checks"] | {"reason": verdict["reason"]}
    sa.eligible = verdict["assignable"]
    sa.last_readiness_at = timezone.now()
    sa.save(update_fields=["readiness", "readiness_detail", "eligible", "last_readiness_at", "updated_at"])
    if prev != sa.readiness:
        _audit(AuditEvent.EventType.TX_SESSION_READINESS_CHANGED, account, sa, actor,
               previous=prev, current=sa.readiness)
    return sa


@transaction.atomic
def set_enabled(account_or_id, enabled: bool, actor=None) -> SessionAssignment:
    account = _resolve_account(account_or_id)
    sa = SessionAssignment.objects.select_for_update().get(trading_account=account)
    if enabled and sa.readiness != SessionAssignment.Readiness.READY:
        raise SessionAssignmentError(
            f"cannot enable routing for account {account.id}: readiness={sa.readiness}"
        )
    sa.enabled = enabled
    sa.save(update_fields=["enabled", "updated_at"])
    _audit(
        AuditEvent.EventType.TX_SESSION_ASSIGNMENT_ENABLED if enabled
        else AuditEvent.EventType.TX_SESSION_ASSIGNMENT_DISABLED,
        account, sa, actor,
    )
    return sa


def resolve_route(account_or_id) -> dict | None:
    """Queryable resolver — returns the routing target IF enabled & READY, else
    None. DORMANT: the live launch path does not call this yet."""
    account = _resolve_account(account_or_id)
    if account is None:
        return None
    sa = SessionAssignment.objects.filter(trading_account=account).first()
    if sa is None or not sa.enabled or sa.readiness != SessionAssignment.Readiness.READY:
        return None
    return {
        "account_id": account.id,
        "windows_username": sa.windows_username,
        "runtime_root": sa.runtime_root,
        "readiness": sa.readiness,
    }
