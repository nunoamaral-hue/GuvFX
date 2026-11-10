"""revoke_copier_live_authorization — the supported, audited terminal revoke of ONE CopierLiveAuthorization
(ADR-0057 control-plane completion, 2026-11-10).

This is the governed counterpart to ``grant_copier_live_authorization``: a human operator terminally revokes
a single, exactly-identified authorization so it can no longer admit a live mutation and no longer blocks a
fresh grant for the pair. It is NOT the global kill switch (``copier_live_disarm``): it touches exactly one
row and leaves every other authorization and the kill switch untouched.

Fail-closed contract:
- Requires the exact ``--authorization-id`` AND the exact ``--master-account-id`` / ``--follower-account-id``
  it is bound to; a route mismatch refuses (never revokes a different pair by id alone).
- Transaction-locks (``select_for_update``) and RE-READS the row inside the lock before any write.
- Requires a valid STAFF ``--revoked-by`` and a non-empty ``--reason`` (recorded for audit).
- Writes ONLY the model's revocation fields (status→REVOKED, revoked_at, revoked_by, revoke_reason). Never
  alters route/scope/expiry/owner/accounts/symbols; never deletes the row; never engages the kill switch.
- Idempotent: an already-terminal authorization (REVOKED or CONSUMED) is a no-op success.
- Redacted output only (the login is masked; no secret is read or printed).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

User = get_user_model()

_TERMINAL = None  # resolved lazily from the model's status choices


def _mask(v) -> str:
    s = str(v or "")
    return ("…" + s[-4:]) if len(s) >= 4 else "****"


class Command(BaseCommand):
    help = "Terminally revoke ONE exactly-identified CopierLiveAuthorization (audited; not the kill switch)."

    def add_arguments(self, parser):
        parser.add_argument("--authorization-id", required=True, type=int)
        parser.add_argument("--owner-email", required=True,
                            help="Must own the authorization (owner pin; mismatch refuses).")
        parser.add_argument("--master-account-id", required=True, type=int,
                            help="Must match the authorization's master (route pin; mismatch refuses).")
        parser.add_argument("--follower-account-id", required=True, type=int,
                            help="Must match the authorization's follower (route pin; mismatch refuses).")
        parser.add_argument("--revoked-by", required=True, help="STAFF user email recording the revocation.")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **o):
        from trade_copier.models import CopierLiveAuthorization, CopierLiveAuthorizationStatus

        reason = (o["reason"] or "").strip()
        if not reason:
            raise CommandError("--reason must be a non-empty audit reason")

        actor = User.objects.filter(email__iexact=o["revoked_by"]).first()
        if actor is None or not actor.is_staff:
            raise CommandError("--revoked-by must be an existing STAFF user (human approval is mandatory)")
        owner = User.objects.filter(email__iexact=o["owner_email"]).first()
        if owner is None:
            raise CommandError(f"--owner-email {o['owner_email']} not found")

        terminal = {CopierLiveAuthorizationStatus.REVOKED, CopierLiveAuthorizationStatus.CONSUMED}

        with transaction.atomic():
            row = (CopierLiveAuthorization.objects.select_for_update()
                   .filter(pk=o["authorization_id"]).first())
            if row is None:
                raise CommandError(f"authorization id={o['authorization_id']} not found")
            # Owner pin — refuse to revoke another owner's authorization (fail-closed).
            if row.user_id != owner.id:
                raise CommandError(
                    "owner mismatch: authorization id=%s is owned by user %s, not %s"
                    % (row.id, row.user_id, owner.id))
            # Route pin — refuse to revoke a different pair by id alone (fail-closed).
            if row.master_account_id != o["master_account_id"] or row.follower_account_id != o["follower_account_id"]:
                raise CommandError(
                    "route mismatch: authorization id=%s is master=%s->follower=%s, not %s->%s"
                    % (row.id, row.master_account_id, row.follower_account_id,
                       o["master_account_id"], o["follower_account_id"]))

            if row.status in terminal:
                # Idempotent: already terminal — no write, success.
                self.stdout.write(self.style.SUCCESS(
                    "CopierLiveAuthorization id=%s already terminal (status=%s); no-op. "
                    "master=%s follower=%s owner=%s" % (
                        row.id, row.status, row.master_account_id, row.follower_account_id, row.user_id)))
                return
            if row.status != CopierLiveAuthorizationStatus.ACTIVE:
                raise CommandError(f"unexpected status {row.status} (expected ACTIVE or a terminal state)")

            now = timezone.now()
            row.status = CopierLiveAuthorizationStatus.REVOKED
            row.revoked_at = now
            row.revoked_by = actor
            row.revoke_reason = reason[:256]
            row.save(update_fields=["status", "revoked_at", "revoked_by", "revoke_reason"])

        self.stdout.write(self.style.SUCCESS(
            "CopierLiveAuthorization id=%s REVOKED: master=%s(%s) follower=%s(%s) owner=%s "
            "revoked_by=%s at %s reason=%s" % (
                row.id, row.master_account_id, _mask(row.master_login),
                row.follower_account_id, _mask(row.follower_login), row.user_id,
                actor.email, now.isoformat(), reason[:256])))
        self.stdout.write("NOTE: single-row terminal revoke only. Kill switch untouched; no job/order created; "
                          "scope/expiry/owner/accounts/symbols unchanged; row retained for audit.")
