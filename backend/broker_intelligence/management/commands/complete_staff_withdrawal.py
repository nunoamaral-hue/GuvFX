"""ADR-0093 — staff-gated completion of ONE broker-neutral MANUAL_STAFF withdrawal (dry-run by default).

Thin command over ``broker_intelligence.staff_completion`` (plan -> apply). It advances a single ADR-0092
``MANUAL_STAFF`` withdrawal (which the autonomous engine will never touch) to ``COMPLETED`` from evidence that is
ALREADY retained and ALREADY linked to it, after fail-closed validation. DRY-RUN by default; ``--apply`` mutates ONLY
that one mutable projection row. It NEVER scans a mailbox, advances a cursor, moves money, contacts a broker, creates a
BrokerEvent, approves a broker, publishes statistics, or touches another withdrawal / Copy Trading. No raw email,
name, full account number or other PII is read into output (only masked suffixes, 12-char hashes and enums).

Production execution is governed separately (Sponsor-gated); this command is the reviewable mechanism, not an
authorisation to run it against production.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = ("Staff-gated completion of one MANUAL_STAFF (reference-less) withdrawal from its already-linked "
            "completion evidence. Dry-run by default; --apply to write.")

    def add_arguments(self, parser):
        parser.add_argument("--owner", required=True, help="Owner email (owner-scoped; must own the withdrawal).")
        parser.add_argument("--withdrawal-id", required=True, type=int, help="Target Withdrawal id (MANUAL_STAFF/REQUESTED).")
        parser.add_argument("--broker-profile-id", required=True, type=int, help="Explicit BrokerProfile id (never inferred).")
        parser.add_argument("--completion-candidate-id", required=True, type=int,
                            help="UnknownSenderDiscoveryCandidate id of the COMPLETION email (must be linked to the withdrawal).")
        parser.add_argument("--expected-account-suffix", required=True,
                            help="Trailing digits the resolved owner-scoped account must end with (e.g. 5017).")
        parser.add_argument("--expected-amount", required=True, help="Expected completion amount (e.g. 30).")
        parser.add_argument("--expected-currency", default="",
                            help="Optional (ADR-0095): expected currency (e.g. GBP). When given, the currency the "
                                 "correlated submission evidence states MUST match; it also enables a currency-BACKFILL "
                                 "of an already-completed row whose currency is blank.")
        parser.add_argument("--approved-by", required=True, help="Named active staff reviewer (email or username).")
        parser.add_argument("--completion-sha", default="",
                            help="Optional: the completion candidate's evidence sha256 prefix (extra fail-closed check).")
        parser.add_argument("--apply", action="store_true", help="Apply the completion (default: dry-run).")

    def handle(self, *args, **opts):
        from django.contrib.auth import get_user_model
        from ...staff_completion import plan_staff_completion, apply_staff_completion, CompletionRefused

        def emit(msg):
            self.stdout.write(msg)

        def mask_email(e):
            if not e or "@" not in e:
                return "***"
            loc, _, dom = e.partition("@")
            return f"{(loc[:2] if len(loc) > 2 else '*')}…@{dom}"

        owner_email = opts["owner"].strip().lower()
        suffix = opts["expected_account_suffix"].strip()
        User = get_user_model()
        owner = User.objects.filter(email__iexact=owner_email).first()
        if owner is None:
            raise CommandError(f"owner not found: {owner_email}")

        try:
            plan = plan_staff_completion(
                withdrawal_id=opts["withdrawal_id"], owner=owner, broker_profile_id=opts["broker_profile_id"],
                completion_candidate_id=opts["completion_candidate_id"], expected_account_suffix=suffix,
                expected_amount=opts["expected_amount"], approved_by=opts["approved_by"],
                expected_currency=opts.get("expected_currency"))
        except CompletionRefused as exc:
            raise CommandError(f"REFUSED {exc.code}: {exc.detail}")

        want_sha = (opts.get("completion_sha") or "").strip().lower()
        if want_sha and not plan.already_completed and not plan.completion_sha12.startswith(want_sha[:12]):
            raise CommandError(f"REFUSED COMPLETION_SHA_MISMATCH: candidate sha {plan.completion_sha12}… != --completion-sha")

        # Mask from the RESOLVED account's real last-4 (never the operator-supplied suffix), so the display is
        # truthful even if the two ever differed.
        completed_row = plan.already_completed or plan.currency_backfill_only
        masked_acct = ("••••" + plan.resolved_account_last4) if plan.resolved_account_last4 else "••••"
        prior_amount = str(plan.prior_amount) if plan.prior_amount is not None else None
        before = {
            "withdrawal_id": plan.withdrawal_id, "owner_id": plan.owner_id,
            "status": ("COMPLETED" if completed_row else "REQUESTED"), "correlation_method": "MANUAL_STAFF",
            "trading_account_id": (plan.resolved_account_id if completed_row else None),
            "amount": prior_amount, "currency": plan.prior_currency,
            "completed_at": (plan.completion_at.isoformat() if (completed_row and plan.completion_at) else None),
        }
        after = {
            "withdrawal_id": plan.withdrawal_id, "owner_id": plan.owner_id,
            "status": "COMPLETED", "correlation_method": "MANUAL_STAFF",
            "trading_account_id": plan.resolved_account_id, "trading_account_masked": masked_acct,
            "broker": plan.broker_display, "amount": str(plan.amount) if plan.amount is not None else None,
            "currency": plan.currency or "(unavailable)",
            "requested_at": plan.requested_at.isoformat() if plan.requested_at else None,
            "completed_at": plan.completion_at.isoformat() if plan.completion_at else None,
            "duration_seconds": plan.duration_seconds,
        }
        # A truthful rollback recipe: list ONLY the fields apply() changes.
        if plan.currency_backfill_only:
            rollback = (f"set withdrawal id={plan.withdrawal_id} currency={plan.prior_currency!r} "
                        f"(restores the blank currency; no other field changes)")
        else:
            amount_clause = " amount=NULL," if plan.prior_amount is None else ""
            curr_clause = (f" currency={plan.prior_currency!r}," if (not plan.prior_currency and plan.currency) else "")
            rollback = (f"set withdrawal id={plan.withdrawal_id} status=REQUESTED, trading_account=NULL, completed_at=NULL,"
                        f"{amount_clause}{curr_clause} last_status_change_at=NULL — restores the BEFORE state above")

        title = "currency backfill" if plan.currency_backfill_only else "completion"
        emit(f"=== Staff-gated MANUAL_STAFF withdrawal {title} (ADR-0093/0094) ===")
        emit(f"  reviewer:          {plan.reviewer}")
        emit(f"  owner:             {mask_email(owner_email)} (id={plan.owner_id})")
        emit(f"  withdrawal:        id={plan.withdrawal_id}")
        emit(f"  broker:            {plan.broker_display or '(as recorded)'} (profile id={plan.broker_profile_id})")
        if not completed_row:
            emit(f"  completion cand:   id={plan.completion_candidate_id} sha={plan.completion_sha12}… ts={after['completed_at']}")
        emit(f"  resolved account:  id={plan.resolved_account_id} {masked_acct} (owner-scoped, single exact match)")
        emit(f"  amount/currency:   {after['amount']} / {after['currency']}")
        emit(f"  BEFORE: {json.dumps(before, sort_keys=True)}")
        emit(f"  AFTER:  {json.dumps(after, sort_keys=True)}")

        if plan.already_completed:
            emit(f"  IDEMPOTENT no-op — withdrawal is already COMPLETED (currency {plan.prior_currency or '(blank)'}). "
                 f"No change.")
            return
        if not opts["apply"]:
            verb = "backfill the currency" if plan.currency_backfill_only else "complete the withdrawal"
            emit(f"  DRY-RUN — no changes written. Re-run with --apply to {verb}.")
            emit(f"  ROLLBACK (if needed after apply): {rollback}.")
            return

        try:
            wd, changed = apply_staff_completion(plan)
        except CompletionRefused as exc:
            raise CommandError(f"REFUSED {exc.code}: {exc.detail}")
        wd.refresh_from_db()
        emit("  APPLIED:" if changed else "  IDEMPOTENT no-op (already applied):")
        emit(f"    withdrawal id={wd.id} status={wd.status} trading_account_id={wd.trading_account_id} "
             f"amount={wd.amount} currency={wd.currency!r} requested_at={wd.requested_at} "
             f"completed_at={wd.completed_at} duration_seconds={wd.duration_seconds()}")
        emit(f"  ROLLBACK (to undo): {rollback}.")
