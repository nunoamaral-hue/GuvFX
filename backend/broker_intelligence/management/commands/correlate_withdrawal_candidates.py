"""ADR-0092 — staff-gated manual withdrawal correlation (broker-neutral).

Lets a NAMED staff reviewer confirm that two or more unknown-sender discovery candidates are ONE withdrawal
lifecycle when the broker provides no stable transaction reference, and (with --apply) project exactly ONE
``REQUESTED`` / in-progress Withdrawal. DRY-RUN by default: without --apply it validates and prints the plan and
changes NOTHING. Fail-closed, idempotent, append-only, privacy-safe. Never money, never a broker, never a cursor,
never ``COMPLETED``.

    python manage.py correlate_withdrawal_candidates \
        --candidate-id <id> --candidate-id <id> \
        --broker-profile-id <id> \
        --approved-by <staff email-or-username> \
        --reason "<review reason>" \
        [--apply]
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ("Staff-gated manual correlation of unknown-sender discovery candidates into ONE REQUESTED withdrawal "
            "(ADR-0092). Dry-run by default; pass --apply to mutate.")

    def add_arguments(self, parser):
        parser.add_argument("--candidate-id", type=int, action="append", required=True, dest="candidate_ids",
                            help="A discovery-candidate id. Repeat for each member (>=2).")
        parser.add_argument("--broker-profile-id", type=int, required=True,
                            help="EXPLICIT BrokerProfile id (active + operator-verified). Never inferred.")
        parser.add_argument("--approved-by", type=str, required=True,
                            help="Email or username of the ACTIVE STAFF reviewer confirming the correlation.")
        parser.add_argument("--reason", type=str, required=True, help="Short review reason (sanitised, bounded).")
        parser.add_argument("--apply", action="store_true",
                            help="Create the withdrawal + decision. WITHOUT this flag the command changes nothing.")

    def handle(self, *args, **o):
        from broker_intelligence.staff_correlation import (plan_correlation, apply_correlation, CorrelationRefused)

        try:
            plan = plan_correlation(candidate_ids=o["candidate_ids"], broker_profile_id=o["broker_profile_id"],
                                    approved_by=o["approved_by"], reason=o["reason"])
        except CorrelationRefused as e:
            self.stderr.write(self.style.ERROR(f"REFUSED {e.code}: {e.detail}"))
            return

        self.stdout.write(self.style.SUCCESS("PLAN (admissible):"))
        self.stdout.write(f"  owner_id            = {plan.owner_id}")
        self.stdout.write(f"  broker_profile_id   = {plan.broker_profile.id} ({plan.broker_profile.slug})")
        self.stdout.write(f"  candidates (ordered)= {[c.id for c in plan.ordered_candidates]}  (by trusted timestamp)")
        self.stdout.write(f"  lifecycle_result    = REQUESTED (in progress; never COMPLETED)")
        self.stdout.write(f"  requested_at        = {plan.requested_at}")
        self.stdout.write(f"  amount/currency     = {plan.amount} {plan.currency!r}  (only if consistently evidenced)")
        self.stdout.write(f"  reviewer            = {plan.reviewer}")
        self.stdout.write(f"  idempotency_key     = {plan.idempotency_key[:16]}…")

        if not o["apply"]:
            self.stdout.write(self.style.WARNING("  DRY-RUN — nothing changed. Pass --apply to create one REQUESTED withdrawal."))
            return

        try:
            dec, created = apply_correlation(plan)
        except CorrelationRefused as e:
            # A genuine TOCTOU: a candidate's state changed (by a DIFFERENT operation) between plan and apply.
            self.stderr.write(self.style.ERROR(f"REFUSED (at apply) {e.code}: {e.detail}"))
            return
        if created:
            self.stdout.write(self.style.SUCCESS(
                f"APPLIED: decision #{dec.id} -> withdrawal #{dec.resulting_withdrawal_id} (REQUESTED / in progress)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"IDEMPOTENT no-op: decision #{dec.id} already exists -> withdrawal #{dec.resulting_withdrawal_id}."))
