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

        # ADR-0097: also terminalise EXPIRED ENGAGED arms for audit. An expired arm is ALREADY inert for admission
        # (``trade_copier.arm`` evaluates ``expires_at`` live), so this changes NO runtime behaviour — it only
        # records the DISENGAGED state. Idempotent; route-scoped when requested; no broker/host contact.
        from trade_copier.models import CopierLiveArm, CopierLiveArmState
        arm_qs = CopierLiveArm.objects.filter(state=CopierLiveArmState.ENGAGED, expires_at__lt=now)
        if o["master_account_id"] is not None:
            arm_qs = arm_qs.filter(master_account_id=o["master_account_id"])
        if o["follower_account_id"] is not None:
            arm_qs = arm_qs.filter(follower_account_id=o["follower_account_id"])
        arm_disengaged = []
        for arm_id in list(arm_qs.values_list("id", flat=True).order_by("id")):
            with transaction.atomic():
                arm = CopierLiveArm.objects.select_for_update().filter(pk=arm_id).first()
                if arm is None or arm.state != CopierLiveArmState.ENGAGED or arm.expires_at >= timezone.now():
                    continue
                arm.state = CopierLiveArmState.DISENGAGED
                arm.disengaged_at = timezone.now()
                arm.disengaged_by = actor
                arm.disengage_reason = "expired_arm_reconciled (inert since expires_at<now; audit only)"
                arm.save(update_fields=["state", "disengaged_at", "disengaged_by", "disengage_reason", "updated_at"])
                arm_disengaged.append(arm.id)

        self.stdout.write(self.style.SUCCESS(
            "reconciled %s expired-ACTIVE authorization(s) -> REVOKED: %s; %s expired arm(s) -> DISENGAGED: %s. "
            "Kill switch untouched; no row deleted; no job/order created." % (
                len(reconciled), reconciled, len(arm_disengaged), arm_disengaged)))
