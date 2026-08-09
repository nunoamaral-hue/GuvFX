"""core.preflight — the authoritative READ-ONLY Hosted Workspace pre-flight (ADR-0035).

One command's worth of "is everything that must be true before enabling Hosted Workspace actually
true?" — evaluated by reading the live system only. It **mutates nothing** (no allocation, no arming, no
flag change) and it is honest about the gates it cannot itself satisfy: the disposable-host RDS/RemoteApp
certification and the Sponsor flag enablement are reported as ``BLOCKED``/``INFO``, never silently passed.

Each check yields one of:
    PASS     a hard prerequisite is satisfied
    WARN     a soft prerequisite / recommended config is missing but not blocking
    FAIL     a hard prerequisite is missing — enabling would be unsafe
    BLOCKED  satisfied on our side but waiting on an external Sponsor/host gate
    INFO     informational posture (e.g. current flag values); never blocks

Overall verdict:
    NOT_READY            some FAIL — a real prerequisite is missing
    BLOCKED_ON_SPONSOR   no FAIL, but a BLOCKED external gate remains (the current true state)
    READY_WITH_WARNINGS  no FAIL/BLOCKED, some WARN
    READY                everything a repository can prove is in place
"""
from __future__ import annotations

from dataclasses import dataclass

PASS, WARN, FAIL, BLOCKED, INFO = "PASS", "WARN", "FAIL", "BLOCKED", "INFO"


@dataclass(frozen=True)
class Check:
    id: str
    category: str
    title: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "title": self.title,
                "status": self.status, "detail": self.detail}


def _safe(fn, cid, category, title):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a preflight check must never raise
        return Check(cid, category, title, FAIL, f"check raised: {type(exc).__name__}: {exc}")


def _check_database() -> Check:
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return Check("infra.database", "INFRA", "Primary database reachable", PASS,
                f"{connection.vendor} answered SELECT 1")


def _check_cache() -> Check:
    from django.conf import settings
    from django.core.cache import cache
    backend = settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]
    cache.get("__ops_preflight_probe__")
    if "LocMem" in backend or "Dummy" in backend:
        return Check("infra.cache", "INFRA", "Shared cache (Redis) configured", WARN,
                     f"{backend} — no shared Redis; multi-process coordination degraded")
    return Check("infra.cache", "INFRA", "Shared cache (Redis) configured", PASS, f"{backend} reachable")


def _check_active_nodes() -> Check:
    from execution.models import TerminalNode
    active = list(TerminalNode.objects.filter(status=TerminalNode.Status.ACTIVE)
                  .values("id", "hostname", "max_accounts"))
    if not active:
        return Check("capacity.active_nodes", "CAPACITY", "At least one ACTIVE execution node", FAIL,
                     "no ACTIVE TerminalNode — allocation would fail closed")
    total_cap = sum(int(n.get("max_accounts") or 0) for n in active)
    if total_cap <= 0:
        return Check("capacity.active_nodes", "CAPACITY", "At least one ACTIVE execution node", WARN,
                     f"{len(active)} active node(s) but total max_accounts=0")
    return Check("capacity.active_nodes", "CAPACITY", "At least one ACTIVE execution node", PASS,
                 f"{len(active)} active node(s); total capacity {total_cap}")


def _check_node_binding_agreement() -> Check:
    """Every bound workspace must agree: account.terminal_node == workspace.execution_node
    (the resolve_hosted_route invariant). A disagreement is a data-integrity fault."""
    from hosted_workspace.models import HostedMt5Workspace
    bound = HostedMt5Workspace.objects.exclude(execution_node__isnull=True).select_related("trading_account")
    mismatched = [ws.pk for ws in bound
                  if ws.trading_account and ws.trading_account.terminal_node_id != ws.execution_node_id]
    if mismatched:
        return Check("integrity.node_binding", "INTEGRITY",
                     "Workspace execution_node agrees with account.terminal_node", FAIL,
                     f"{len(mismatched)} workspace(s) with node disagreement: {mismatched[:10]}")
    return Check("integrity.node_binding", "INTEGRITY",
                 "Workspace execution_node agrees with account.terminal_node", PASS,
                 "all bound workspaces agree (or none bound)")


def _check_delivery_config() -> Check:
    from django.conf import settings
    from core.credentials import is_placeholder
    base = str(getattr(settings, "GUAC_BASE_URL", "") or "")
    secret = str(getattr(settings, "GUAC_JSON_SECRET_KEY_HEX", "") or "")
    missing = []
    if not base or is_placeholder(base):
        missing.append("GUAC_BASE_URL")
    if not secret or is_placeholder(secret):
        missing.append("GUAC_JSON_SECRET_KEY_HEX")
    if missing:
        return Check("delivery.guac_config", "DELIVERY", "Guacamole delivery config present", WARN,
                     f"missing/placeholder: {missing} — delivery fails closed with DA_GUAC_UNCONFIGURED")
    return Check("delivery.guac_config", "DELIVERY", "Guacamole delivery config present", PASS,
                 "GUAC_BASE_URL + GUAC_JSON_SECRET_KEY_HEX set")


def _check_flag_posture() -> Check:
    from hosted_workspace.flags import (
        hosted_mt5_execution_enabled, hosted_mt5_remoteapp_enabled,
        hosted_persistent_mt5_enabled, hosted_workspace_onboarding_enabled)
    posture = {
        "HOSTED_PERSISTENT_MT5_ENABLED": hosted_persistent_mt5_enabled(),
        "HOSTED_WORKSPACE_ONBOARDING_ENABLED": hosted_workspace_onboarding_enabled(),
        "HOSTED_MT5_REMOTEAPP_ENABLED": hosted_mt5_remoteapp_enabled(),
        "HOSTED_MT5_EXECUTION_ENABLED": hosted_mt5_execution_enabled(),
    }
    on = [k for k, v in posture.items() if v]
    return Check("flags.posture", "FLAGS", "Hosted Workspace feature-flag posture", INFO,
                 f"ON={on or 'none (all DARK by default)'}")


def _check_host_certification() -> Check:
    """The disposable-host RDS/RemoteApp certification is a Sponsor/host gate that no repository check can
    satisfy. It is always BLOCKED here until an out-of-band certification record says otherwise."""
    return Check("host.certification", "HOST", "RDS/RemoteApp host certification complete", BLOCKED,
                 "disposable Windows/RDS host + RemoteApp publication + licensing not certified "
                 "(see Hosted Workspace Host Certification Record)")


def _check_execution_authority() -> Check:
    """Reassert the invariant, not a live order check: the live bridge gate remains the sole order-time
    authority; nothing here (or any read model) authorises an order."""
    return Check("execution.authority", "EXECUTION", "Order authority remains the live bridge gate", INFO,
                 "repository readiness never authorises an order; the live bridge gate is authoritative")


_CHECKS = (
    (_check_database, "infra.database", "INFRA", "Primary database reachable"),
    (_check_cache, "infra.cache", "INFRA", "Shared cache (Redis) configured"),
    (_check_active_nodes, "capacity.active_nodes", "CAPACITY", "At least one ACTIVE execution node"),
    (_check_node_binding_agreement, "integrity.node_binding", "INTEGRITY", "Node binding agreement"),
    (_check_delivery_config, "delivery.guac_config", "DELIVERY", "Guacamole delivery config present"),
    (_check_flag_posture, "flags.posture", "FLAGS", "Hosted Workspace feature-flag posture"),
    (_check_execution_authority, "execution.authority", "EXECUTION", "Order authority remains live gate"),
    (_check_host_certification, "host.certification", "HOST", "RDS/RemoteApp host certification"),
)


def run_preflight() -> dict:
    """Evaluate every pre-flight check read-only and compute the honest overall verdict. MUTATES NOTHING."""
    checks = [_safe(fn, cid, cat, title) for (fn, cid, cat, title) in _CHECKS]
    by_status: dict = {}
    for c in checks:
        by_status[c.status] = by_status.get(c.status, 0) + 1

    if by_status.get(FAIL):
        verdict = "NOT_READY"
    elif by_status.get(BLOCKED):
        verdict = "BLOCKED_ON_SPONSOR"
    elif by_status.get(WARN):
        verdict = "READY_WITH_WARNINGS"
    else:
        verdict = "READY"

    blocking = [c.as_dict() for c in checks if c.status in (FAIL, BLOCKED)]
    return {
        "verdict": verdict,
        "ready": verdict in ("READY", "READY_WITH_WARNINGS"),
        "counts": by_status,
        "blocking": blocking,
        "checks": [c.as_dict() for c in checks],
    }
