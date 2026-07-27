"""Re-encrypt every trading.crypto-encrypted column onto the CURRENT primary key.

Phase 3 / ADR-0019 (SEC-CRYPTO-001). After an operator sets an explicit GUVFX_FERNET_KEY (or a
GUVFX_FERNET_KEYS rotation list), stored ciphertext is still readable via MultiFernet but was written
under the old (possibly DJANGO_SECRET_KEY-derived) key. This command reads each value and re-writes it
under the current primary, so a later DJANGO_SECRET_KEY rotation — or retirement of an old key — can
never render a credential undecryptable.

It is idempotent in effect (every row ends up under the current primary), fail-closed per row (a value
that cannot be decrypted is left UNCHANGED and counted as failed, never dropped), and never logs or
prints a secret. Covers all three trading.crypto columns:
  * trading.TradingAccount.password_enc            (Customer Secret — broker password)
  * terminal_provisioning.AccountProvisioning.password_enc  (Runtime Secret — generated Windows password)
  * onboarding.TwoFactorSecret.secret_enc          (TOTP secret)
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from trading.crypto import decrypt_password, encrypt_password, encryption_key_status

# (app_label, model_name, field) — resolved lazily so the command never hard-imports across apps.
_TARGETS = [
    ("trading", "TradingAccount", "password_enc"),
    ("terminal_provisioning", "AccountProvisioning", "password_enc"),
    ("onboarding", "TwoFactorSecret", "secret_enc"),
]


class Command(BaseCommand):
    help = (
        "Re-encrypt stored broker/runtime/TOTP credentials onto the current GUVFX_FERNET_KEY primary "
        "(closes SEC-CRYPTO-001). Reads via MultiFernet, writes the current primary. Idempotent; "
        "fail-closed per row; never prints a secret."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="report what would change; write nothing")

    def handle(self, *args, **opts):
        from django.apps import apps

        dry = opts["dry_run"]
        status = encryption_key_status()
        if not status["explicit_key_configured"]:
            # Re-encrypting under a DJANGO_SECRET_KEY-derived key achieves nothing (still coupled).
            # CommandError → non-zero exit so an evidence/automation wrapper cannot record a refused
            # rotation as success. Note: `explicit_key_configured` is the single source of truth in
            # trading.crypto, so a separator-only GUVFX_FERNET_KEYS (" , ") refuses here too.
            raise CommandError(
                "no explicit GUVFX_FERNET_KEY/GUVFX_FERNET_KEYS configured — re-encryption would "
                "still derive from DJANGO_SECRET_KEY (SEC-CRYPTO-001). Set an explicit key first.")

        grand_total = grand_reenc = grand_failed = grand_skipped = 0
        for app_label, model_name, field in _TARGETS:
            try:
                model = apps.get_model(app_label, model_name)
            except LookupError:
                self.stdout.write(f"{app_label}.{model_name}: model not installed — skipped")
                continue

            total = reenc = failed = skipped = 0
            qs = model.objects.exclude(**{field: ""}).exclude(**{f"{field}__isnull": True})
            for row in qs.iterator():
                total += 1
                ciphertext = getattr(row, field) or ""
                try:
                    # Classification (T8 boundary): this decrypt-then-immediately-re-encrypt is a key
                    # ROTATION (lifecycle stage 8), not a decrypt-for-use ACCESS (stage 6). The
                    # plaintext never leaves this process, so it is audited once as the aggregate
                    # CREDENTIAL_ROTATED below, not per-account ACCESSED.
                    plaintext = decrypt_password(ciphertext)
                except Exception:
                    failed += 1
                    self.stderr.write(
                        f"{app_label}.{model_name}(pk={row.pk}).{field}: decrypt FAILED — left unchanged")
                    continue
                if not plaintext:
                    skipped += 1
                    continue
                if not dry:
                    new_ct = encrypt_password(plaintext)
                    with transaction.atomic():
                        model.objects.filter(pk=row.pk).update(**{field: new_ct})
                reenc += 1

            self.stdout.write(
                f"{app_label}.{model_name}.{field}: total={total} reencrypted={reenc} "
                f"skipped={skipped} failed={failed}")
            grand_total += total
            grand_reenc += reenc
            grand_failed += failed
            grand_skipped += skipped

        self.stdout.write(
            f"DONE: total={grand_total} reencrypted={grand_reenc} skipped={grand_skipped} "
            f"failed={grand_failed} dry_run={dry} read_keys={status['read_key_count']}")

        if not dry:
            # Append-only audit that a key rotation ran (counts only, never a secret). Emitted even on
            # partial failure so the audit reflects reality before the non-zero exit below.
            from core.audit import log_credential_event
            log_credential_event(
                "ROTATED", entity_type="CustomerCredentialKeyMaterial",
                entity_id="reencrypt_customer_credentials", actor="reencrypt_customer_credentials",
                reencrypted=grand_reenc, failed=grand_failed, targets=len(_TARGETS))

        if grand_failed:
            # Non-zero exit: the rotation is NOT clean. Successfully-processed rows are already
            # committed under the primary; the failed rows were left byte-for-byte unchanged (never
            # dropped). Re-run after investigating the failed rows.
            raise CommandError(
                f"{grand_failed} credential(s) could not be re-encrypted and were left unchanged — "
                f"rotation incomplete; investigate and re-run")
