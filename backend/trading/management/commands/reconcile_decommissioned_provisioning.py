"""Reconcile tombstoned accounts whose hosted-runtime provisioning record still carries live credential material.

Model-A decommission correctness (2026-10-01). ``remove_account`` now synchronously destroys BOTH credential
stores (the customer ``TradingAccount`` credential via ``disconnect_account`` AND the hosted-runtime
``AccountProvisioning.password_enc`` via ``destroy_runtime_provisioning_credential``) and retires the provisioning
record. This command brings HISTORICAL tombstones — removed BEFORE that fix (e.g. Account 37, Account 40) — into the
same truthful post-decommission state: ``AccountProvisioning.password_enc=''`` + ``status=RETIRED``.

It is the governed reconciliation mechanism requested by the Sponsor (NOT an ad-hoc DB edit): it only ever touches
TOMBSTONED (``disconnected_at IS NOT NULL``) accounts, only clears credential material + sets status, NEVER deletes
history, NEVER touches an active account, NEVER recreates host resources, and NEVER prints/decrypts a credential.
Dry-run by default; ``--apply`` to execute.

Usage:
  manage.py reconcile_decommissioned_provisioning                 # dry-run, all tombstoned accounts
  manage.py reconcile_decommissioned_provisioning --apply         # apply to all tombstoned accounts
  manage.py reconcile_decommissioned_provisioning --account-id 37 --account-id 40 --apply
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retire hosted-runtime provisioning credential/status for tombstoned accounts (Model-A decommission correctness)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Apply the changes. Without this flag the command only reports (dry-run).")
        parser.add_argument("--account-id", action="append", type=int, default=[],
                            help="Limit to these tombstoned account id(s). Repeatable. Default: all tombstoned.")

    def handle(self, *args, **opts):
        from terminal_provisioning.models import AccountProvisioning
        from trading.account_removal import destroy_runtime_provisioning_credential
        from trading.models import TradingAccount

        apply = bool(opts["apply"])
        ids = list(opts["account_id"] or [])

        # SAFETY: only tombstoned accounts are ever considered. An active account is never touched.
        qs = TradingAccount.objects.filter(disconnected_at__isnull=False)
        if ids:
            qs = qs.filter(id__in=ids)

        considered = 0
        needs_fix = []     # (account_id) that still carry credential material OR a non-RETIRED provisioning status
        for acct in qs.order_by("id"):
            considered += 1
            provs = list(AccountProvisioning.objects.filter(trading_account=acct))
            has_cred = any(bool(p.password_enc) for p in provs)
            not_retired = any(p.status != AccountProvisioning.Status.RETIRED for p in provs)
            if provs and (has_cred or not_retired):
                needs_fix.append((acct.id, has_cred, not_retired))

        self.stdout.write(f"tombstoned_considered={considered} needs_reconcile={len(needs_fix)} apply={apply}")
        for aid, has_cred, not_retired in needs_fix:
            # NEVER print the credential; only a boolean presence flag.
            self.stdout.write(f"  account={aid} had_credential={has_cred} status_not_retired={not_retired}")

        if not apply:
            self.stdout.write("DRY-RUN: no changes written. Re-run with --apply to reconcile.")
            return

        fixed = 0
        for aid, _has_cred, _not_retired in needs_fix:
            acct = TradingAccount.objects.get(id=aid)
            # Defence in depth: refuse to touch a non-tombstoned account even if the query above raced.
            if acct.disconnected_at is None:
                self.stdout.write(f"  SKIP account={aid}: not tombstoned (refusing)")
                continue
            res = destroy_runtime_provisioning_credential(acct, actor="reconcile_decommissioned_provisioning")
            fixed += 1
            self.stdout.write(f"  RECONCILED account={aid} rows={res.get('rows')} had_credential={res.get('had_credential')}")
        self.stdout.write(f"APPLIED: reconciled={fixed}")
