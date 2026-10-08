"""WP5 — seed the Sponsor-listed broker-registration emails as PENDING BrokerEmailIdentity rows.

Idempotent (get_or_create on email+broker). Dry-run by default; pass --apply to write. Honours §4: identities are
created PENDING and UNBOUND — a GuvFX user is NOT attached merely because the address was listed (ownership is
verified later, via the mailbox connection). A BrokerAccount is attached ONLY when strong evidence exists (broker +
account number match a non-tombstoned account), and even then status stays PENDING until the receiving mailbox is
verified. Never guesses. DARK: creates data only; no ingestion, no trading effect.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

# The six IS6 broker-registration emails (Sponsor packet 2026-10-08 §3). gmail/googlemail string variants get
# SEPARATE identity rows; the underlying-mailbox dedup is ConnectedMailbox(provider, provider_mailbox_id), not these.
_IS6_EMAILS = [
    "guvfx02@gmail.com", "support@guvfx.com", "nrfda1111@googlemail.com",
    "nrfda1111@gmail.com", "approvals@guvfx.com", "guvfx01@gmail.com",
]
# The TradersWay primary pilot identity (§2): reg email + broker + known MT5 login (for account attribution).
_TRADERSWAY = {"email": "nrfda1111@googlemail.com", "broker": "TradersWay", "mt5_login": "55442"}


class Command(BaseCommand):
    help = "Seed Sponsor-listed broker-registration emails as PENDING BrokerEmailIdentity rows (dry-run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write rows (default: dry-run report only).")

    def handle(self, *args, **o):
        from broker_intelligence.models import BrokerEmailIdentity
        from trading.models import TradingAccount

        apply = o["apply"]
        plan = []   # (email, broker, trading_account_id_or_None, note)

        for email in _IS6_EMAILS:
            plan.append((email, "IS6 Technologies", None, "PENDING/unbound (ownership+routing unverified)"))

        # TradersWay: attribute to the account ONLY on a strong (broker + account-number) match to an active row.
        tw_acct = (TradingAccount.objects
                   .filter(broker_name__icontains="tradersway",
                           account_number=_TRADERSWAY["mt5_login"], disconnected_at__isnull=True)
                   .order_by("id").first())
        tw_acct_id = tw_acct.id if tw_acct is not None else None
        tw_note = (f"account {tw_acct_id} by broker+login={_TRADERSWAY['mt5_login']} match (routing still unverified)"
                   if tw_acct_id else f"UNRESOLVED: no active TradersWay account with login {_TRADERSWAY['mt5_login']}")
        plan.append((_TRADERSWAY["email"], _TRADERSWAY["broker"], tw_acct_id, tw_note))

        self.stdout.write(f"{'APPLYING' if apply else 'DRY-RUN'} — {len(plan)} broker-email identities:")
        created = existing = 0
        for email, broker, acct_id, note in plan:
            existing_row = BrokerEmailIdentity.objects.filter(email__iexact=email, broker_name=broker).first()
            if existing_row is not None:
                existing += 1
                self.stdout.write(f"  = EXISTS  {email} @ {broker} (status={existing_row.status})")
                continue
            self.stdout.write(f"  + CREATE  {email} @ {broker} -> {note}")
            if apply:
                with transaction.atomic():
                    BrokerEmailIdentity.objects.create(
                        email=email, broker_name=broker, trading_account_id=acct_id,
                        user=None,                        # §4: never bind a user merely because listed
                        status=BrokerEmailIdentity.Status.PENDING,
                        origin=BrokerEmailIdentity.Origin.BROKER_REGISTERED)
                created += 1
        self.stdout.write(self.style.SUCCESS(
            f"Done. created={created if apply else 0} existing={existing} planned={len(plan)} "
            f"(user=NULL for all — ownership verified later; TradersWay acct={tw_acct_id})"))
