"""
E3-MONITOR-SCHEDULING — run the post-trade monitor chain once, in dependency order.

Orchestration ONLY. This command adds no execution logic: it invokes the three existing,
already-shipped monitor functions in the fixed pipeline order so a newly-closed trade can flow
all the way through in a single pass::

    process_closed_trades()  # closed Trade  -> internal TradeOutcomeRecord (WIN/LOSS/BE)
    route_outcomes()         # WIN record    -> internal PENDING NotificationCandidate
    dispatch_pending()       # PENDING cand. -> transport (no-op unless flag ON; dry-run by default)

HARD BOUNDARY — inherited from the three functions it calls, none of which this command widens:
it creates NO order (no order_send / order_check / ExecutionJob) and publishes NOTHING to WIMS (no
ConsumptionContract). It sends NO Telegram message by default: dispatch is behind
``NOTIFICATION_DISPATCH_ENABLED`` (default OFF) and the transport defaults to dry-run — a real send
happens only if an operator ALSO selects the real transport (``NOTIFICATION_DISPATCH_TRANSPORT``).
It only processes rows that already exist (closed trades / outcome records / candidates) and
creates internal records. Every step is idempotent, so the chain is safe to run every minute.

Resilient by default: a step that raises is logged and the chain continues to the next step (each
step is independent + idempotent, so a transient failure in one must not block the others). Pass
``--fail-fast`` to re-raise instead (for manual debugging). Exit code is 0 on a clean or
partially-failed resilient run; ``--fail-fast`` surfaces the error as a non-zero CommandError.

Usage::

    python manage.py run_monitor_chain
    python manage.py run_monitor_chain --limit 100
    python manage.py run_monitor_chain --fail-fast        # debug: stop on first error
"""
import logging

from django.core.management.base import BaseCommand, CommandError

from execution.breakeven import sweep_breakeven
from execution.close_monitor import DEFAULT_LIMIT, process_closed_trades, resolve_completed_plans
from execution.execution_health import sweep_execution_health
from execution.provider_commands_engine import apply_provider_commands
from execution.notifications.dispatcher import dispatch_enabled, dispatch_pending
from execution.notifications.reconcile import check_notification_health, reconcile_notifications
from execution.outcome_router import route_outcomes

logger = logging.getLogger("guvfx.execution.monitor_chain")


class Command(BaseCommand):
    help = (
        "Run the post-trade monitor chain once (close-monitor -> outcome-router -> dispatch). "
        "Internal records only; no order, no Telegram transmission, no WIMS. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                            help="Max rows each step processes (default: %(default)s).")
        parser.add_argument("--fail-fast", action="store_true",
                            help="Re-raise the first step error instead of continuing (debug).")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        fail_fast = opts["fail_fast"]
        results = {}
        failures = []

        # Fixed dependency order: close-monitor feeds the outcome-router, which feeds the
        # dispatcher. Resolved here (not at import) so each name is looked up at call time.
        steps = (
            # WS-C: always-on reliability sweep — reclaim dead SYNC orphans + alert on order-opening
            # jobs stuck PENDING (never claimed → plan promoted but no order placed). Runs first so a
            # reclaimed slot / surfaced defect is visible to the rest of the chain this tick.
            ("execution_health", sweep_execution_health),
            ("resolve_plans", resolve_completed_plans),
            # WS-B AUTO-BREAKEVEN — after TP1 closes, move remaining legs' SL to entry. Inert
            # unless BREAKEVEN_ENABLED. Enqueue-only + idempotent, so safe to run every minute.
            ("breakeven", sweep_breakeven),
            # WS-E: apply recorded provider trade-management commands (move-SL/close/cancel). Inert
            # unless PROVIDER_COMMANDS_ENABLED + the source opts in. Enqueue-only, source-isolated.
            ("provider_commands", apply_provider_commands),
            ("close_monitor", process_closed_trades),
            ("outcome_router", route_outcomes),
            # WS-C exactly-once safety net: backfill missing candidates, revive SENT-but-never-
            # transmitted (real transport only), stamp delivered, alert on stuck winners. Runs
            # BEFORE dispatch so anything it revives/backfills is sent the same tick.
            ("reconcile", reconcile_notifications),
            ("dispatch", dispatch_pending),
            # WS-B4: roll up notification-transport health into one auto-resolving alert.
            ("notify_health", check_notification_health),
        )
        for name, fn in steps:
            try:
                results[name] = fn(limit=limit)
            except Exception as exc:  # resilient: one step's failure must not block the others
                if fail_fast:
                    raise CommandError(f"monitor-chain step '{name}' failed: {exc}") from exc
                logger.exception("monitor_chain: step %s failed", name)
                self.stderr.write(f"monitor-chain: STEP FAILED name={name} error={exc!r}")
                failures.append(name)

        eh = results.get("execution_health", {})
        rp = results.get("resolve_plans", {})
        be = results.get("breakeven", {})
        pc = results.get("provider_commands", {})
        cm = results.get("close_monitor", {})
        outr = results.get("outcome_router", {})
        rec = results.get("reconcile", {})
        disp = results.get("dispatch", {})
        nh = results.get("notify_health", {})
        # One grep-friendly summary line for the cron log. ``dispatch_enabled`` is echoed so the
        # log itself proves the dry-run posture; ``transmitted`` is never anything but zero here.
        self.stdout.write(
            "monitor-chain: "
            "exec_health[reclaimed={ehr} reclaimed_modify={ehrm} stuck_alerted={ehs} unplanned={ehu} unplanned_resolved={ehur}] "
            "resolve[scanned={rs} closed={rc} still_open={ro}] "
            "breakeven[enabled={ben} synced={bsy} enqueued={beq} applied={bap} tp2_locked={btl} inflight={binf} skipped={bsk} deferred={bdf} noop_closed={bnc} alerted={bal} overdue={bov}] "
            "provider_cmds[enabled={pce} applied={pca} rejected={pcr} ambiguous={pcm}] "
            "close[processed={cp} win={cw} loss={cl} be={cb} skipped={cs}] "
            "outcome[routed={orr} candidates={oc} internal_only={oi}] "
            "reconcile[delivered={rmd} backfilled={rbf} revived={rrv} alerted={ral}] "
            "dispatch[enabled={de} claimed={dcl} sent={dsent} failed={df} skipped={dsk}] "
            "notify_health[issues={nhi} alerted={nha} resolved={nhr}] "
            "failures={failed} "
            "(internal records + no order/WIMS; dispatch OFF/dry-run by default)".format(
                ehr=eh.get("reclaimed", 0), ehrm=eh.get("reclaimed_modify", 0),
                ehs=eh.get("stuck_alerted", 0), ehu=eh.get("unplanned_alerted", 0),
                ehur=eh.get("unplanned_resolved", 0),
                rs=rp.get("scanned", 0), rc=rp.get("closed", 0), ro=rp.get("still_open", 0),
                ben=be.get("enabled", False), bsy=be.get("synced", 0), beq=be.get("enqueued", 0),
                bap=be.get("applied", 0), binf=be.get("inflight", 0), bsk=be.get("skipped", 0),
                bal=be.get("alerted", 0), btl=be.get("tp2_locked", 0), bov=be.get("overdue", 0),
                bdf=be.get("deferred", 0), bnc=be.get("noop_closed", 0),
                pce=pc.get("enabled", False), pca=pc.get("applied", 0), pcr=pc.get("rejected", 0),
                pcm=pc.get("ambiguous", 0),
                cp=cm.get("processed", 0), cw=cm.get("win", 0), cl=cm.get("loss", 0),
                cb=cm.get("breakeven", 0), cs=cm.get("skipped", 0),
                orr=outr.get("routed", 0), oc=outr.get("candidates", 0),
                oi=outr.get("internal_only", 0),
                rmd=rec.get("marked_delivered", 0), rbf=rec.get("backfilled", 0),
                rrv=rec.get("revived", 0), ral=rec.get("alerted", 0),
                de=disp.get("enabled", dispatch_enabled()), dcl=disp.get("claimed", 0),
                dsent=disp.get("sent", 0), df=disp.get("failed", 0), dsk=disp.get("skipped", 0),
                nhi=nh.get("issues", 0), nha=nh.get("alerted", 0), nhr=nh.get("resolved", 0),
                failed=(",".join(failures) if failures else "none"),
            )
        )

        # Freshness heartbeat so the reliability system can ALERT if the chain stops running — the
        # cron silently failed for weeks on a log-permission error with no signal. Fail-open: a
        # heartbeat failure must never break the run. Evaluated by ``reliability_tick``.
        try:
            from reliability.services.heartbeat import record_beat
            record_beat("monitor_chain", interval_s=90, detail={
                "resolved": rp.get("closed", 0), "processed": cm.get("processed", 0),
                "candidates": outr.get("candidates", 0),
                "breakeven_enqueued": be.get("enqueued", 0), "breakeven_applied": be.get("applied", 0),
                "failures": failures,
            })
        except Exception:  # pragma: no cover - defensive; heartbeat is best-effort
            pass
