"""reconcile_expired_copier_authorizations — move time-expired ACTIVE authorizations to a terminal state
(ADR-0057 control-plane completion, 2026-11-10).

A ``CopierLiveAuthorization`` whose ``expires_at`` has passed is inert for admission (the live admission
re-checks ``expires_at > now`` at order time), but its ``status`` stays ACTIVE — there is no EXPIRED status
and nothing auto-transitions it. Because ``grant_copier_live_authorization`` refuses a second ACTIVE row for
a pair, a stale expired-ACTIVE row would permanently block future grants. This command reconciles them using
the model's EXISTING terminal semantics (status→REVOKED with an explicit expiry reason) — no new status, no
migration.

Safe by default: DRY-RUN unless ``--commit``. Only rows that are ACTIVE **and** already past ``expires_at``
are touched; each under its own lock with a re-check (idempotent). The kill switch is never engaged; no row
is deleted; only the revocation fields are written. Redacted output.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

User = get_user_model()

_REASON = "expired_window_reconciled (expires_at<now; terminalised to unblock future grants)"


class Command(BaseCommand):
    help = "Terminalise time-expired ACTIVE CopierLiveAuthorizations (dry-run by default; existing REVOKED state)."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", default=False,
                            help="Apply the reconciliation. Omit for a read-only dry-run preview.")
        parser.add_argument("--by", default="", help="Operator email recorded as revoked_by (optional).")
        parser.add_argument("--master-account-id", type=int, default=None,
                            help="Optional: restrict to this master (route-scoped reconciliation).")
        parser.add_argument("--follower-account-id", type=int, default=None,
                            help="Optional: restrict to this follower.")

    def handle(self, *args, **o):
        from trade_copier.models import CopierLiveAuthorization, CopierLiveAuthorizationStatus

        now = timezone.now()
        actor = User.objects.filter(email__iexact=o["by"]).first() if o["by"] else None

        qs = CopierLiveAuthorization.objects.filter(
            status=CopierLiveAuthorizationStatus.ACTIVE, expires_at__lt=now)
        if o["master_account_id"] is not None:
            qs = qs.filter(master_account_id=o["master_account_id"])
        if o["follower_account_id"] is not None:
            qs = qs.filter(follower_account_id=o["follower_account_id"])

        candidate_ids = list(qs.values_list("id", flat=True).order_by("id"))
        self.stdout.write("expired-ACTIVE candidates: %s (count=%s) commit=%s" % (
            candidate_ids, len(candidate_ids), bool(o["commit"])))
        if not o["commit"]:
            self.stdout.write("DRY-RUN: nothing changed. Re-run with --commit to terminalise the above.")
            return

        reconciled = []
        for aid in candidate_ids:
            with transaction.atomic():
                row = CopierLiveAuthorization.objects.select_for_update().filter(pk=aid).first()
                # Re-check under the lock — only an ACTIVE, still-expired row is touched (idempotent).
                if (row is None or row.status != CopierLiveAuthorizationStatus.ACTIVE
                        or row.expires_at >= timezone.now()):
                    continue
                row.status = CopierLiveAuthorizationStatus.REVOKED
                row.revoked_at = timezone.now()
                row.revoked_by = actor
                row.revoke_reason = _REASON
                row.save(update_fields=["status", "revoked_at", "revoked_by", "revoke_reason"])
                reconciled.append(row.id)

        self.stdout.write(self.style.SUCCESS(
            "reconciled %s expired-ACTIVE authorization(s) -> REVOKED: %s. "
            "Kill switch untouched; no row deleted; no job/order created." % (len(reconciled), reconciled)))
