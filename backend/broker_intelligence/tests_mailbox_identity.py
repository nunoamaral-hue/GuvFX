"""WP5 — multi-mailbox foundation: ConnectedMailbox, BrokerEmailIdentity, identity-aware resolver, backfill.

Covers: mailbox dedup by provider-stable id (gmail/googlemail cannot double-connect), identity nullable bindings +
PENDING default + (email, broker) uniqueness, resolver resolving a VERIFIED identity to an account (and PENDING to
none), and the idempotent backfill that never binds a user merely because an address was listed. All DARK — no
ingestion/OAuth/trading effect.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from broker_intelligence import resolver
from broker_intelligence.models import BrokerEmailAlias, BrokerEmailIdentity, ConnectedMailbox
from broker_intelligence.services import mint_alias_for_account
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "94%04d" % _n


def _user():
    login = _uniq()
    return User.objects.create_user(username="mm%s" % login, email="%s@x.invalid" % login, password="x")


def _acct(**kw):
    u = kw.pop("user", None) or _user()
    login = _uniq()
    d = dict(user=u, name="A", broker_name="TradersWay", account_number=login, is_demo=True, is_active=True)
    d.update(kw)
    return TradingAccount.objects.create(**d)


class ConnectedMailboxTests(TestCase):
    def test_same_underlying_mailbox_cannot_double_connect(self):
        u = _user()
        ConnectedMailbox.objects.create(user=u, provider=ConnectedMailbox.Provider.GMAIL,
                                        provider_mailbox_id="google-acct-123", primary_email="nrfda1111@gmail.com")
        # Same Google account id, different email STRING (googlemail vs gmail) -> must be refused (dedup by id, §5).
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ConnectedMailbox.objects.create(user=u, provider=ConnectedMailbox.Provider.GMAIL,
                                                provider_mailbox_id="google-acct-123",
                                                primary_email="nrfda1111@googlemail.com")

    def test_credential_ref_is_pointer_not_token(self):
        u = _user()
        mb = ConnectedMailbox.objects.create(user=u, provider_mailbox_id="g1", primary_email="x@gmail.com",
                                             credential_ref="vault://ingestion/mbx/g1")
        self.assertNotIn("@", mb.credential_ref.split("//")[-1])   # a pointer, not an email/token value
        self.assertEqual(mb.status, ConnectedMailbox.Status.PENDING)   # not connected until consent


class BrokerEmailIdentityTests(TestCase):
    def test_defaults_pending_and_unbound(self):
        i = BrokerEmailIdentity.objects.create(email="guvfx02@gmail.com", broker_name="IS6 Technologies")
        self.assertEqual(i.status, BrokerEmailIdentity.Status.PENDING)
        self.assertIsNone(i.user_id)
        self.assertIsNone(i.trading_account_id)
        self.assertIsNone(i.connected_mailbox_id)

    def test_email_plus_broker_unique_but_same_email_different_broker_ok(self):
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="IS6 Technologies")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay")  # OK
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="IS6 Technologies")


class IdentityResolverTests(TestCase):
    def test_verified_identity_resolves_to_account(self):
        a = _acct()
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           trading_account=a, status=BrokerEmailIdentity.Status.VERIFIED)
        alias, account = resolver.resolve(["nrfda1111@googlemail.com"])
        self.assertIsNone(alias)
        self.assertEqual(account.pk, a.pk)

    def test_pending_identity_resolves_no_account(self):
        a = _acct()
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           trading_account=a, status=BrokerEmailIdentity.Status.PENDING)
        alias, account = resolver.resolve(["nrfda1111@googlemail.com"])
        self.assertIsNone(account)                                  # routing unverified -> no live account

    def test_display_name_header_matches_bare_address(self):
        a = _acct()
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           trading_account=a, status=BrokerEmailIdentity.Status.VERIFIED)
        _, account = resolver.resolve(["Nuno <NRFDA1111@googlemail.com>"])   # display name + case
        self.assertEqual(account.pk, a.pk)

    def test_alias_takes_precedence_over_identity(self):
        a = _acct()
        al = mint_alias_for_account(a)
        alias, account = resolver.resolve([al.address()])
        self.assertEqual(alias.pk, al.pk)                           # GuvFX alias wins
        self.assertEqual(account.pk, a.pk)

    def test_unknown_address_resolves_nothing(self):
        alias, account = resolver.resolve(["stranger@nowhere.invalid"])
        self.assertIsNone(alias)
        self.assertIsNone(account)

    # --- Regressions for the WP5 review MEDIUM: broker-blind resolution (shadowing + cross-user). ---
    def test_pending_other_broker_row_does_not_shadow_verified(self):
        # The shipped-data TradersWay case: same address at IS6 (PENDING, no acct, created FIRST -> lower pk) AND
        # TradersWay (VERIFIED, bound). Resolution must pick the VERIFIED account, not the lower-pk PENDING row.
        acct = _acct()
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="IS6 Technologies",
                                           status=BrokerEmailIdentity.Status.PENDING)            # lower pk, no account
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="TradersWay",
                                           trading_account=acct, status=BrokerEmailIdentity.Status.VERIFIED)
        _, account = resolver.resolve(["nrfda1111@googlemail.com"])
        self.assertEqual(account.pk, acct.pk)        # the verified account, not shadowed/dropped

    def test_two_verified_accounts_for_one_address_fail_closed(self):
        # Cross-user / ambiguous: the SAME address verified at two brokers to DIFFERENT accounts -> never guess.
        a1 = _acct(user=_user())
        a2 = _acct(user=_user())
        BrokerEmailIdentity.objects.create(email="shared@x.invalid", broker_name="IS6 Technologies",
                                           trading_account=a1, status=BrokerEmailIdentity.Status.VERIFIED)
        BrokerEmailIdentity.objects.create(email="shared@x.invalid", broker_name="TradersWay",
                                           trading_account=a2, status=BrokerEmailIdentity.Status.VERIFIED)
        _, account = resolver.resolve(["shared@x.invalid"])
        self.assertIsNone(account)                   # ambiguous -> UNRESOLVED, no cross-user leak

    def test_multiple_verified_same_account_resolves(self):
        acct = _acct()
        BrokerEmailIdentity.objects.create(email="dup@x.invalid", broker_name="IS6 Technologies",
                                           trading_account=acct, status=BrokerEmailIdentity.Status.VERIFIED)
        BrokerEmailIdentity.objects.create(email="dup@x.invalid", broker_name="TradersWay",
                                           trading_account=acct, status=BrokerEmailIdentity.Status.VERIFIED)
        _, account = resolver.resolve(["dup@x.invalid"])
        self.assertEqual(account.pk, acct.pk)        # all verified rows agree on one account -> safe


class BackfillCommandTests(TestCase):
    def test_dry_run_creates_nothing(self):
        out = StringIO()
        call_command("backfill_broker_email_identities", stdout=out)
        self.assertEqual(BrokerEmailIdentity.objects.count(), 0)
        self.assertIn("DRY-RUN", out.getvalue())

    def test_apply_is_idempotent_and_never_binds_user(self):
        _acct(account_number="55442", broker_name="TradersWay", is_demo=False)   # the TradersWay 55442 account
        call_command("backfill_broker_email_identities", "--apply", stdout=StringIO())
        n1 = BrokerEmailIdentity.objects.count()
        self.assertGreaterEqual(n1, 7)                              # 6 IS6 + 1 TradersWay
        self.assertFalse(BrokerEmailIdentity.objects.exclude(user__isnull=True).exists())  # never bound a user
        # TradersWay identity attributed to the 55442 account by broker+login match, still PENDING.
        tw = BrokerEmailIdentity.objects.get(email="nrfda1111@googlemail.com", broker_name="TradersWay")
        self.assertIsNotNone(tw.trading_account_id)
        self.assertEqual(tw.status, BrokerEmailIdentity.Status.PENDING)
        call_command("backfill_broker_email_identities", "--apply", stdout=StringIO())   # re-run
        self.assertEqual(BrokerEmailIdentity.objects.count(), n1)  # idempotent
