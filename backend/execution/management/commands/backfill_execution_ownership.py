"""Phase A §13 — backfill durable strategy ownership onto historical Plans + Trades.

This is the EXPLICIT, human-invoked full-history ownership tool. It complements the
bounded monitor-chain sweep (``sweep_trade_ownership`` / ``STRATEGY_OWNERSHIP_SWEEP_
WINDOW_HOURS``): the sweep only attributes trades ingested within its rolling window, so
full-history attribution older than that window is an operator DECISION run here — never
an implicit side effect of enabling a flag. This command deliberately scans ALL un-owned
rows (no window) and is idempotent.

Backfills ONLY deterministically-resolvable provenance; leaves everything else NULL
(LEGACY_UNATTRIBUTED — never fabricated). DRY-RUN by default (prints counts, writes
nothing); ``--commit`` persists; ``--account <id>`` scopes to one account.

  * Plans: strategy_assignment derived via the auto-router (account, source) rule
    (exactly one active match), else left NULL (ambiguous/none).
  * Trades: resolved via magic (registry) then WAY comment (§9 matrix). STRONG/LEGACY
    are stamped; CONFLICT/UNATTRIBUTED are left NULL and counted.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = ("Explicit full-history backfill of durable strategy ownership on Plans + Trades where "
            "deterministic (DRY-RUN by default). Complements the bounded monitor-chain sweep; run this "
            "for attribution older than STRATEGY_OWNERSHIP_SWEEP_WINDOW_HOURS.")

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Persist (default: dry-run).")
        parser.add_argument("--account", type=int, default=None, help="Limit to one account id.")

    def handle(self, *args, **opts):
        commit = opts["commit"]
        account = opts["account"]

        from execution.models import SignalExecutionPlan
        from execution.signal_promotion import _resolve_plan_assignment
        from execution.ownership_stamp import (
            resolve_owner_for_trade, STRONG, LEGACY, CONFLICT, UNATTRIBUTED)
        from trading.models import Trade

        # --- Plans ---
        plan_qs = SignalExecutionPlan.objects.filter(strategy_assignment__isnull=True)
        if account:
            plan_qs = plan_qs.filter(account_id=account)
        plan_updates, plan_ambiguous = [], 0
        for plan in plan_qs.iterator():
            owner = _resolve_plan_assignment(plan)
            if owner is not None:
                plan_updates.append((plan.id, owner))
            else:
                plan_ambiguous += 1

        # --- Trades ---
        trade_qs = Trade.objects.filter(strategy_assignment__isnull=True)
        if account:
            trade_qs = trade_qs.filter(account_id=account)
        trade_counts = {STRONG: 0, LEGACY: 0, CONFLICT: 0, UNATTRIBUTED: 0}
        trade_updates = []
        for t in trade_qs.iterator():
            owner, code = resolve_owner_for_trade(t)
            trade_counts[code] = trade_counts.get(code, 0) + 1
            if owner is not None and code in (STRONG, LEGACY):
                trade_updates.append((t.id, owner.id))

        self.stdout.write("=== Plans ===")
        self.stdout.write(f"  backfillable: {len(plan_updates)}  ambiguous/unattributed: {plan_ambiguous}")
        self.stdout.write("=== Trades ===")
        self.stdout.write(f"  strong: {trade_counts[STRONG]}  legacy: {trade_counts[LEGACY]}  "
                          f"conflict: {trade_counts[CONFLICT]}  unattributed: {trade_counts[UNATTRIBUTED]}")

        if not commit:
            self.stdout.write(self.style.WARNING("DRY-RUN — nothing written. Re-run with --commit to persist."))
            return

        from strategies.models import StrategyAssignment
        owners = {a.id: a for a in StrategyAssignment.objects.all()}
        with transaction.atomic():
            for plan_id, owner in plan_updates:
                SignalExecutionPlan.objects.filter(id=plan_id).update(strategy_assignment=owner)
            for trade_id, owner_id in trade_updates:
                Trade.objects.filter(id=trade_id).update(strategy_assignment=owners[owner_id])
        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {len(plan_updates)} plans and {len(trade_updates)} trades."))
