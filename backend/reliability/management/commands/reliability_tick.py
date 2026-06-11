"""
reliability_tick — RX-2 Reliability Core periodic check (cron, every minute).

Phase 1: Detection + Visibility + Alerting + Recovery *Recommendations* only.
Performs NO automatic recovery (no re-login, no restart, no force-fail).

Gated by RELIABILITY_CORE_ENABLED (env). With the flag off it is a dormant
no-op (validation requirement). Use --force to run the body for controlled
validation without flipping the global flag.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from reliability.constants import RELIABILITY_CORE_ENABLED, Component, HealthStatus
from reliability.services import (
    heartbeat, mt5_supervision, execution_supervisor, trading_health, alerting, health_store,
)


class Command(BaseCommand):
    help = "RX-2 Reliability Core tick: detect, surface, alert, recommend (no auto-recovery)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Run even if RELIABILITY_CORE_ENABLED is false.")

    def handle(self, *args, **opts):
        now = timezone.now()
        if not RELIABILITY_CORE_ENABLED and not opts["force"]:
            self.stdout.write(f"[reliability_tick] dormant (RELIABILITY_CORE_ENABLED=false) at {now.isoformat()}")
            return

        from execution.models import TerminalNode
        from trading.models import TradingAccount

        # BACKEND_DB: reaching here means the DB is reachable.
        health_store.upsert(Component.BACKEND_DB, HealthStatus.OK, detail={"reached": True})

        # RX-2C heartbeats (schedulers + workers).
        heartbeat.evaluate()

        # RX-2B MT5 supervision per active terminal (+ its active account/instance).
        for node in TerminalNode.objects.filter(status="active"):
            acc = (TradingAccount.objects.filter(terminal_node=node, is_active=True).first()
                   or TradingAccount.objects.filter(terminal_node=node).first())
            inst = getattr(acc, "mt5_instance", None) if acc else None
            mt5_supervision.evaluate(terminal_node=node, trading_account=acc, mt5_instance=inst)

        # RX-2E execution supervisor (orphaned RUNNING jobs — detect only).
        orphans = execution_supervisor.evaluate()

        # RX-2A aggregate scoped trading health.
        snapshots = trading_health.aggregate(now)

        # Alert lifecycle + advisory recommendations.
        counts = alerting.reconcile(orphan_jobs=orphans)

        # RX-2G Phase 0: SHADOW recovery engine — records what would happen,
        # executes nothing. Honors AUTO_RECOVERY_FROZEN > circuit > market > policy.
        rx2g = {}
        try:
            from reliability.recovery import run_shadow
            rx2g = run_shadow(now)
        except Exception:  # noqa: BLE001 — recovery shadow must never break the tick
            import logging
            logging.getLogger(__name__).exception("rx2g run_shadow failed")

        glob = next((s for s in snapshots if s.scope == "GLOBAL"), None)
        self.stdout.write(
            f"[reliability_tick] {now.isoformat()} global={glob.state if glob else '?'} "
            f"can_trade={glob.can_trade if glob else '?'} snapshots={len(snapshots)} "
            f"orphans={len(orphans)} alerts_opened={counts['opened']} resolved={counts['resolved']} "
            f"recommendations={counts['recommended']} | rx2g market={rx2g.get('market_state')} "
            f"frozen={rx2g.get('frozen')} circuit={rx2g.get('circuit')} "
            f"shadow_planned={rx2g.get('shadow_planned')} suppressed={rx2g.get('suppressed')}"
        )
