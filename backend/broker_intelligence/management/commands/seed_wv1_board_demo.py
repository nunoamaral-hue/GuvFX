"""WV1 Board-demo seeder — labelled SYNTHETIC demonstration data for an ISOLATED demo environment.

Creates a demo member with ten broker accounts (including two at the SAME broker), a spread of synthetic withdrawals
across lifecycle states, and — through the REAL correlation engine — a reconstruction of the genuine pilot's
owner-attributed, account-UNRESOLVED external withdrawal (request + completion 33m01s apart, linked by one broker
reference). EVERYTHING is ``provenance=SYNTHETIC`` so it can never be mistaken for genuine data and is surfaced only
when the demo-only flag ``BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC`` is armed (which labels every row "DEMONSTRATION").

SAFETY: this command writes synthetic rows and MUST NEVER run against production. It refuses unless the target
database looks like a demo DB (name contains "demo", or DEBUG, or the demo flag is armed) AND ``--confirm-demo-db``
is passed. It is idempotent (stable references; get_or_create) so it is safe to re-run. It performs NO network / host
/ broker / credential / execution action — it only writes to the local demo database.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from broker_intelligence import correlation
from broker_intelligence.models import (BrokerEvent, ConnectedMailbox, Provenance, TransactionCategory,
                                        Withdrawal)
from trading.models import TradingAccount

User = get_user_model()

_DEMO_EMAIL = "demo-board@example.invalid"

# Ten demo accounts; "Pepperstone" appears TWICE (the same-broker case, WV1 §5 screen A). Login numbers are obvious
# placeholders. All is_demo=True.
_ACCOUNTS = [
    ("Pepperstone", "DEMO-9000001"), ("Pepperstone", "DEMO-9000002"),
    ("TradersWay", "DEMO-9000003"), ("IS6 Technologies", "DEMO-9000004"),
    ("IC Markets", "DEMO-9000005"), ("FXCM", "DEMO-9000006"),
    ("Exness", "DEMO-9000007"), ("OANDA", "DEMO-9000008"),
    ("XM", "DEMO-9000009"), ("Vantage", "DEMO-9000010"),
]

# Synthetic account-resolved withdrawals (broker idx into _ACCOUNTS, status, amount, currency, ref, age-hours,
# duration-hours-or-None). A truthful spread: completed (with durations), in-flight pending, and one failed.
_WDS = [
    (0, Withdrawal.Status.COMPLETED, "500.00", "USD", "DEMO-REF-1001", 72, 5),
    (0, Withdrawal.Status.COMPLETED, "1200.00", "USD", "DEMO-REF-1002", 50, 9),
    (2, Withdrawal.Status.COMPLETED, "300.00", "USD", "DEMO-REF-1003", 30, 2),
    (3, Withdrawal.Status.PENDING, None, "", "DEMO-REF-1004", 6, None),
    (4, Withdrawal.Status.COMPLETED, "875.50", "USD", "DEMO-REF-1005", 20, 4),
    (5, Withdrawal.Status.REJECTED, "200.00", "USD", "DEMO-REF-1006", 40, None),
    (6, Withdrawal.Status.COMPLETED, "150.00", "EUR", "DEMO-REF-1007", 12, 1),
]

# The genuine-pilot RECONSTRUCTION (owner-attributed, account-UNRESOLVED): a REDACTED, SYNTHETIC reproduction of the
# real pair's STRUCTURE only — a placeholder reference, no real token / address / account. 33m01s request→completion.
_GENUINE_REF = "DEMO-7654321"
_GENUINE_GAP = timedelta(minutes=33, seconds=1)


class Command(BaseCommand):
    help = "Seed labelled SYNTHETIC WV1 board-demo data into an ISOLATED demo database (never production)."

    def add_arguments(self, parser):
        parser.add_argument("--confirm-demo-db", action="store_true",
                            help="Required acknowledgement that the target DB is a disposable demo database.")

    def handle(self, *args, **opts):
        db_name = str(connection.settings_dict.get("NAME", ""))
        # Gate ONLY on durable DB-disposability signals — the database NAME marker (operator-controlled) or DEBUG.
        # Deliberately NOT the data-surfacing flag BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC: that flag governs whether
        # synthetic rows are SHOWN, not whether a database is safe to MUTATE; conflating them would let arming a
        # surfacing flag unlock writes against production. A hosted demo DB must therefore be named with "demo".
        looks_demo = ("demo" in db_name.lower()) or bool(settings.DEBUG)
        if not looks_demo:
            raise CommandError(
                f"Refusing to seed: target database {db_name!r} does not look like a demo database (its name does "
                "not contain 'demo' and DEBUG is off). This command writes SYNTHETIC data and must NEVER run against "
                "production; name the isolated demo database with 'demo'.")
        if not opts["confirm_demo_db"]:
            raise CommandError(f"Pass --confirm-demo-db to seed synthetic demo data into {db_name!r}.")

        now = timezone.now()
        member, _ = User.objects.get_or_create(
            username="demo-board", defaults={"email": _DEMO_EMAIL})
        if member.email != _DEMO_EMAIL:
            member.email = _DEMO_EMAIL
            member.save(update_fields=["email"])

        # A CONNECTED demo mailbox (screen B — connected email management). credential_ref stays empty (no token in
        # the DB, ever); primary_email is a clearly-demo placeholder address, masked in the UI.
        ConnectedMailbox.objects.get_or_create(
            user=member, provider_mailbox_id="demo-gmail-sub-0001",
            defaults=dict(provider=ConnectedMailbox.Provider.GMAIL, primary_email=_DEMO_EMAIL,
                          status=ConnectedMailbox.Status.CONNECTED, scopes="gmail.readonly",
                          last_successful_sync=now))

        accts = []
        for broker, number in _ACCOUNTS:
            a, _ = TradingAccount.objects.get_or_create(
                user=member, account_number=number,
                defaults=dict(name=f"DEMO {broker}", broker_name=broker, is_demo=True, is_active=True))
            accts.append(a)

        # Account-resolved synthetic withdrawals (idempotent by (account, reference)).
        for idx, status, amount, currency, ref, age_h, dur_h in _WDS:
            acct = accts[idx]
            req_at = now - timedelta(hours=age_h)
            comp_at = (req_at + timedelta(hours=dur_h)) if (dur_h is not None
                                                            and status == Withdrawal.Status.COMPLETED) else None
            Withdrawal.objects.get_or_create(
                trading_account=acct, broker_reference_id=ref,
                defaults=dict(owner=member, status=status,
                              amount=Decimal(amount) if amount is not None else None, currency=currency,
                              requested_at=req_at, completed_at=comp_at,
                              correlation_method=Withdrawal.CorrelationMethod.REFERENCE_ID,
                              provenance=Provenance.SYNTHETIC))

        # The genuine-pilot reconstruction — through the REAL correlation engine (owner-attributed, account-NULL).
        self._seed_genuine_reconstruction(member, now)

        from broker_intelligence.correlation import run_correlation
        summary = run_correlation()

        wd_total = Withdrawal.objects.filter(owner=member).count()
        unresolved = Withdrawal.objects.filter(owner=member, trading_account__isnull=True).count()
        self.stdout.write(self.style.SUCCESS(
            f"WV1 board-demo seeded into {db_name!r}: member={member.username}, accounts={len(accts)} "
            f"(2 same-broker), withdrawals={wd_total} (owner-attributed/account-UNRESOLVED={unresolved}); "
            f"correlation={summary}. All provenance=SYNTHETIC; arm BROKER_WITHDRAWAL_DEMO_INCLUDE_SYNTHETIC + "
            "BROKER_WITHDRAWAL_UX_ENABLED in the demo env to surface them (labelled DEMONSTRATION)."))

    def _seed_genuine_reconstruction(self, member, now):
        """Reproduce the genuine pilot's STRUCTURE (redacted, synthetic): an EXTERNAL_WITHDRAWAL request + a neutral
        COMPLETED event sharing one reference, 33m01s apart, owner-attributed with NO trading account — projected by
        the real engine into one account-UNRESOLVED withdrawal that advances REQUESTED→COMPLETED."""
        req_at = now - timedelta(hours=3)
        comp_at = req_at + _GENUINE_GAP
        BrokerEvent.objects.get_or_create(
            owner=member, broker="TradersWay", event_type="WITHDRAWAL_CONFIRMATION_REQUIRED",
            broker_reference_id=_GENUINE_REF, transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
            defaults=dict(amount=Decimal("250.00"), currency="USD", occurred_at=req_at, received_at=req_at,
                          provenance=Provenance.SYNTHETIC,
                          correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED))
        BrokerEvent.objects.get_or_create(
            owner=member, broker="TradersWay", event_type="WITHDRAWAL_COMPLETED",
            broker_reference_id=_GENUINE_REF, transaction_category=TransactionCategory.UNKNOWN,
            defaults=dict(occurred_at=comp_at, received_at=comp_at, provenance=Provenance.SYNTHETIC,
                          correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED))
