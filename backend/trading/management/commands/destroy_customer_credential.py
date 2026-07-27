"""Operator tool (Phase 3 / P3-D) — securely destroy one customer's stored broker credential.

Offboarding / password-eviction entry point: clears TradingAccount.password_enc (+ legacy
broker_password) and records a redacted CREDENTIAL_DESTROYED audit. Targets exactly ONE account by id
(no bulk switch — mass credential destruction is a footgun and must be deliberate). Never prints a
secret. Honest scope: secure clear, not per-customer crypto-shred (see credential_lifecycle.py).
"""
from django.core.management.base import BaseCommand, CommandError

from trading.credential_lifecycle import destroy_customer_credential
from trading.models import TradingAccount


class Command(BaseCommand):
    help = ("Securely destroy (clear + audit) the stored broker credential for ONE TradingAccount, "
            "by id. Idempotent; never prints a secret. This is the clear-WITHOUT-delete path — the "
            "correct way to offboard a fully provisioned account, whose row cannot be DELETEd while "
            "its AccountProvisioning (PROTECT) exists.")

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, required=True,
                            help="TradingAccount id whose stored broker credential to destroy")
        parser.add_argument("--actor", default="operator",
                            help="audit actor label (default: operator)")

    def handle(self, *args, **opts):
        try:
            account = TradingAccount.objects.get(pk=opts["account_id"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"TradingAccount id={opts['account_id']} not found")

        evidence = destroy_customer_credential(account, actor=opts["actor"])
        self.stdout.write(
            f"destroy_customer_credential: account_id={account.pk} "
            f"had_credential={evidence['had_credential']} cleared={evidence['cleared_fields']} "
            f"method={evidence['method']}")
