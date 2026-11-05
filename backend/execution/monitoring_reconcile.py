"""execution.monitoring_reconcile — read-only monitoring-route reconciliation + governed alerting (ADR-0075).

GFX-MT5-BRIDGE-SCALE-001, Phase 7. Detects the fault classes that can leave a monitoring route wrong,
updates each route's durable readiness + reason code, and raises operator-visible alerts through the
EXISTING reliability mechanism. It is READ-ONLY with respect to bridges, ACLs, accounts and workspaces:
it writes only ``MonitoringRoute`` readiness fields and ``reliability.AlertEvent`` rows. It never mutates
a Tailscale policy, restarts a bridge, deletes an account, or takes any trading action, and it adds NO
production schedule (it runs only when an operator invokes the default-OFF command).

Alerting uses the DIRECT pattern (``AlertEvent.objects.create`` with a dedup pre-check) under a raw,
namespaced component string ``MONITORING_GATEWAY`` — so it needs NO edit to ``reliability/constants.py``
and NO reliability migration (the component field accepts any ≤32-char string and ``reconcile()``
tolerates unknown components). Reason codes are stable and secret-free; logins never appear.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from execution import monitoring_models as mm
from execution import monitoring_allocation as alloc

logger = logging.getLogger("guvfx.execution.monitoring.reconcile")

# Namespaced component for reliability alerts (NOT added to reliability.constants.Component — reconcile
# tolerates unknown component strings and the field accepts any ≤32 chars).
ALERT_COMPONENT = "MONITORING_GATEWAY"

_HEALTH_FOR_READINESS = {
    mm.MonitoringRoute.Readiness.MONITORING_READY: mm.MonitoringRoute.Health.OK,
    mm.MonitoringRoute.Readiness.MONITORING_PARTIAL: mm.MonitoringRoute.Health.DEGRADED,
    mm.MonitoringRoute.Readiness.MONITORING_UNAVAILABLE: mm.MonitoringRoute.Health.FAILED,
    mm.MonitoringRoute.Readiness.CONNECTED_NOT_READY: mm.MonitoringRoute.Health.DEGRADED,
    mm.MonitoringRoute.Readiness.UNKNOWN: mm.MonitoringRoute.Health.UNKNOWN,
    mm.MonitoringRoute.Readiness.UNSUPPORTED: mm.MonitoringRoute.Health.UNKNOWN,
}

# Reason codes that warrant an operator alert, with severity.
_ALERTABLE = {
    mm.RC_UNREACHABLE: "WARN",
    mm.RC_LISTENER_ABSENT: "WARN",
    mm.RC_IDENTITY_MISMATCH: "CRITICAL",
    mm.RC_TERMINAL_INIT_FAILED: "WARN",
    mm.RC_TERMINAL_DISCONNECTED: "WARN",
    mm.RC_GATEWAY_UNHEALTHY: "CRITICAL",
    mm.RC_DUPLICATE_ROUTE: "CRITICAL",
    "route_integrity_violation": "CRITICAL",
    mm.RC_FEATURE_DISABLED: "INFO",
    mm.RC_STALE_HEALTH: "WARN",
}


def _enabled() -> bool:
    try:
        from execution.monitoring_flags import monitoring_gateway_enabled, monitoring_route_reconcile_enabled
        return monitoring_gateway_enabled() and monitoring_route_reconcile_enabled()
    except Exception:  # noqa: BLE001
        return False


def reconcile_routes(*, probe_fn=None, now=None, emit_alerts: bool = True) -> dict:
    """Reconcile every active monitoring route. Returns a summary {scanned, ready, partial, unavailable,
    alerts_opened, alerts_resolved, by_reason:{...}, host_unreachable:[...], duplicates:[...]}.

    ``probe_fn(route) -> ProbeResult`` is injectable (default the real client) so CI never hits the
    network. Fail-open per route (one route's error never aborts the pass)."""
    from execution.monitoring_readiness import evaluate_route_readiness
    if probe_fn is None:
        from execution.monitoring_gateway_client import probe_route as probe_fn  # noqa: N806
    now = now or timezone.now()

    routes = list(mm.MonitoringRoute.objects.exclude(state=mm.MonitoringRoute.State.TOMBSTONED)
                  .select_related("gateway", "trading_account", "execution_endpoint"))
    # Duplicate detection (one active route per account) + host grouping for the ACL/host-wide signal.
    per_account: dict[int, int] = {}
    per_host_total: dict[str, int] = {}
    per_host_unreachable: dict[str, int] = {}
    for r in routes:
        per_account[r.trading_account_id] = per_account.get(r.trading_account_id, 0) + 1
        per_host_total[r.host_key] = per_host_total.get(r.host_key, 0) + 1

    summary = {"scanned": 0, "ready": 0, "partial": 0, "unavailable": 0,
               "alerts_opened": 0, "alerts_resolved": 0, "by_reason": {}, "host_unreachable": [],
               "duplicates": sorted([a for a, c in per_account.items() if c > 1])}

    for r in routes:
        summary["scanned"] += 1
        # The WHOLE per-route body is fail-open: a probe error, an evaluation error, or a
        # MonitoringIntegrityError from set_readiness (a drifted route quarantines then raises) must never
        # abort the pass for the remaining routes.
        try:
            probe = probe_fn(r)
            dup = per_account.get(r.trading_account_id, 1)
            result = evaluate_route_readiness(r, probe, now=now, duplicate_count=dup)
            health = _HEALTH_FOR_READINESS.get(result.readiness, mm.MonitoringRoute.Health.UNKNOWN)
            alloc.set_readiness(r, readiness=result.readiness, reason_code=result.reason_code,
                                health_state=health,
                                make_ready=(result.readiness ==
                                            mm.MonitoringRoute.Readiness.MONITORING_READY),
                                actor="monitoring_reconcile")
            summary["by_reason"][result.reason_code] = summary["by_reason"].get(result.reason_code, 0) + 1
            if result.readiness == mm.MonitoringRoute.Readiness.MONITORING_READY:
                summary["ready"] += 1
            elif result.readiness == mm.MonitoringRoute.Readiness.MONITORING_PARTIAL:
                summary["partial"] += 1
            else:
                summary["unavailable"] += 1
            if not probe.reachable:
                per_host_unreachable[r.host_key] = per_host_unreachable.get(r.host_key, 0) + 1

            if emit_alerts:
                keep = f"monroute:{result.reason_code}:{r.trading_account_id}"
                sev = _ALERTABLE.get(result.reason_code)
                if sev and sev != "INFO":
                    if _emit_alert(sev, result.reason_code, trading_account_id=r.trading_account_id,
                                   dedup=keep,
                                   title=f"Monitoring route {result.reason_code} "
                                         f"(acct {r.trading_account_id})",
                                   detail={"host": _host_hash(r.host_key), "local_port": r.local_port,
                                           "generation": r.allocation_generation,
                                           "readiness": result.readiness}):
                        summary["alerts_opened"] += 1
                    # Reason-code transition: resolve any OTHER open per-account alert (a stale cause).
                    summary["alerts_resolved"] += _resolve_other_route_alerts(r.trading_account_id, keep)
                else:
                    # Recovered / benign (INFO or READY) — resolve all open per-account route alerts.
                    summary["alerts_resolved"] += _resolve_alerts_for_account(r.trading_account_id)
        except alloc.MonitoringIntegrityError:
            logger.exception("monitoring reconcile integrity failure route=%s (quarantined)", r.pk)
            summary["unavailable"] += 1
            if emit_alerts:
                integ = f"monroute:route_integrity_violation:{r.trading_account_id}"
                if _emit_alert("CRITICAL", "route_integrity_violation",
                               trading_account_id=r.trading_account_id, dedup=integ,
                               title=f"Monitoring route integrity violation (acct {r.trading_account_id})",
                               detail={"route": r.pk}):
                    summary["alerts_opened"] += 1
                # The integrity path returns BEFORE the normal resolve block — resolve any stale
                # non-integrity per-account alert here so it does not leak OPEN behind the integrity one.
                summary["alerts_resolved"] += _resolve_other_route_alerts(r.trading_account_id, integ)
            continue
        except Exception:  # noqa: BLE001 — one route's failure never aborts the pass
            logger.exception("monitoring reconcile route failed route=%s", r.pk)
            continue

    # Host-wide reachability failure (ACL / gateway down affecting every route on a host) — one CRITICAL,
    # symmetrically resolved when the host recovers (not every route unreachable).
    for host, total in per_host_total.items():
        unreachable = per_host_unreachable.get(host, 0)
        if total > 0 and unreachable == total:
            summary["host_unreachable"].append(host)
            if emit_alerts and _emit_alert(
                    "CRITICAL", "host_boundary_unreachable", trading_account_id=None,
                    dedup=f"monhost:unreachable:{host}",
                    title=f"All {total} monitoring routes unreachable on host",
                    detail={"host_key_hash": _host_hash(host), "routes": total}):
                summary["alerts_opened"] += 1
        elif emit_alerts:
            summary["alerts_resolved"] += _resolve_host_alert(host)

    # Decommission sweep: resolve any OPEN monitoring alert whose account/host no longer has a
    # non-tombstoned route in this scan (so a stale CRITICAL/WARN does not linger after a route is retired).
    if emit_alerts:
        summary["alerts_resolved"] += _sweep_orphan_alerts(
            {r.trading_account_id for r in routes}, set(per_host_total))

    logger.info("monitoring reconcile summary=%s", {k: v for k, v in summary.items()
                                                    if k not in ("by_reason",)})
    return summary


def run() -> dict:
    """Gated entry point for the command (two-level darkness: master + reconcile flags)."""
    if not _enabled():
        return {"skipped": "disabled"}
    return reconcile_routes()


def _emit_alert(severity: str, reason_code: str, *, trading_account_id, dedup: str, title: str,
                detail: dict) -> bool:
    """Create an OPEN AlertEvent unless an identical OPEN one already exists (caller-enforced dedup, as
    every existing reliability caller does). Returns True if a new alert was opened. Never raises."""
    try:
        from reliability.models import AlertEvent
        if AlertEvent.objects.filter(dedup_key=dedup, status=AlertEvent.Status.OPEN).exists():
            return False
        AlertEvent.objects.create(
            severity=severity, component=ALERT_COMPONENT,
            trading_account_id=trading_account_id, title=title[:200], body="",
            dedup_key=dedup, status=AlertEvent.Status.OPEN, detail=detail or {})
        return True
    except Exception:  # noqa: BLE001 — alerting must never break reconciliation
        logger.exception("monitoring alert emit failed dedup=%s", dedup)
        return False


def _resolve_alerts_for_account(trading_account_id) -> int:
    try:
        from reliability.models import AlertEvent
        qs = AlertEvent.objects.filter(component=ALERT_COMPONENT, trading_account_id=trading_account_id,
                                       status=AlertEvent.Status.OPEN)
        return qs.update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now())
    except Exception:  # noqa: BLE001
        return 0


def _resolve_other_route_alerts(trading_account_id, keep_dedup: str) -> int:
    """Resolve open per-account route alerts whose cause has CHANGED (dedup_key != the current one), so a
    route that moves between two distinct alertable reasons never leaves the stale alert OPEN."""
    try:
        from reliability.models import AlertEvent
        qs = (AlertEvent.objects.filter(component=ALERT_COMPONENT, trading_account_id=trading_account_id,
                                        status=AlertEvent.Status.OPEN, dedup_key__startswith="monroute:")
              .exclude(dedup_key=keep_dedup))
        return qs.update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now())
    except Exception:  # noqa: BLE001
        return 0


def _resolve_host_alert(host) -> int:
    try:
        from reliability.models import AlertEvent
        qs = AlertEvent.objects.filter(component=ALERT_COMPONENT, status=AlertEvent.Status.OPEN,
                                       dedup_key=f"monhost:unreachable:{host}")
        return qs.update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now())
    except Exception:  # noqa: BLE001
        return 0


def _sweep_orphan_alerts(active_accounts, active_hosts) -> int:
    """Resolve OPEN monitoring alerts for accounts/hosts that no longer have a non-tombstoned route in the
    current scan (decommissioned), so a truthful-when-opened alert does not linger forever after retirement."""
    try:
        from reliability.models import AlertEvent
        now = timezone.now()
        resolved = (AlertEvent.objects.filter(component=ALERT_COMPONENT, status=AlertEvent.Status.OPEN,
                                              dedup_key__startswith="monroute:")
                    .exclude(trading_account_id__in=active_accounts)
                    .update(status=AlertEvent.Status.RESOLVED, resolved_at=now))
        prefix = "monhost:unreachable:"
        for a in AlertEvent.objects.filter(component=ALERT_COMPONENT, status=AlertEvent.Status.OPEN,
                                           dedup_key__startswith=prefix):
            if a.dedup_key[len(prefix):] not in active_hosts:
                a.status = AlertEvent.Status.RESOLVED
                a.resolved_at = now
                a.save(update_fields=["status", "resolved_at"])
                resolved += 1
        return resolved
    except Exception:  # noqa: BLE001
        return 0


def _host_hash(host_key: str) -> str:
    """Short, non-reversible tag for a host in alert detail (keeps infra topology out of the alert body
    per the evidence/security redaction rule, while still letting an operator correlate)."""
    import hashlib
    return hashlib.sha256(str(host_key).encode("utf-8")).hexdigest()[:12]
