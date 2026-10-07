"""WP1 — BrokerEmailAlias identity foundation: opaque, per-instance, never-reused, flag-gated mint/retire."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models.sql.compiler import SQLUpdateCompiler
from django.test import TestCase, override_settings

from broker_intelligence.models import BrokerEmailAlias, alias_domain
from broker_intelligence.services import (ensure_broker_email_alias, mint_alias_for_account,
                                          retire_alias_for_account, retire_broker_email_alias)
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "90%04d" % _n


def _acct(**kw):
    login = _uniq()
    u = User.objects.create_user(username="bi%s" % login, email="%s@x.invalid" % login, password="x")
    defaults = dict(user=u, name="A", broker_name="B", account_number=login, is_demo=True, is_active=True)
    defaults.update(kw)
    return TradingAccount.objects.create(**defaults)


class AliasModelTests(TestCase):
    def test_mint_creates_active_bound_opaque_alias(self):
        a = _acct()
        al = mint_alias_for_account(a)
        self.assertEqual(al.status, BrokerEmailAlias.Status.ACTIVE)
        self.assertEqual(al.trading_account_id, a.id)
        self.assertIsNotNone(al.bound_at)
        self.assertEqual(al.address(), f"{al.alias_local}@{alias_domain()}")
        # Opaque: a CSPRNG lowercase-hex token ('ba' + 128-bit) — NOT derived from the account/user (so it carries
        # no PII and is non-enumerable). We assert the exact opaque format + rely on distinctness (separate test)
        # and the use of secrets.token_hex for non-derivation; a substring check on a tiny id would be coincidental.
        self.assertRegex(al.alias_local, r"^ba[0-9a-f]{32}$")

    def test_mint_is_idempotent(self):
        a = _acct()
        first = mint_alias_for_account(a)
        again = mint_alias_for_account(a)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(BrokerEmailAlias.objects.filter(trading_account=a).count(), 1)

    def test_distinct_accounts_get_distinct_aliases(self):
        al1 = mint_alias_for_account(_acct())
        al2 = mint_alias_for_account(_acct())
        self.assertNotEqual(al1.alias_local, al2.alias_local)

    def test_retire_sets_retired_and_is_idempotent(self):
        a = _acct()
        mint_alias_for_account(a)
        self.assertEqual(retire_alias_for_account(a), 1)
        al = BrokerEmailAlias.objects.get(trading_account=a)
        self.assertEqual(al.status, BrokerEmailAlias.Status.RETIRED)
        self.assertIsNotNone(al.retired_at)
        self.assertEqual(retire_alias_for_account(a), 0)   # idempotent

    def test_token_and_bind_are_immutable(self):
        a = _acct()
        al = mint_alias_for_account(a)
        al.alias_local = "ba" + "f" * 32
        with self.assertRaises(ValidationError):
            al.save()
        al.refresh_from_db()
        al.trading_account = _acct()      # re-point attempt
        with self.assertRaises(ValidationError):
            al.save()

    def test_retired_is_terminal(self):
        a = _acct()
        mint_alias_for_account(a)
        retire_alias_for_account(a)
        al = BrokerEmailAlias.objects.get(trading_account=a)
        al.status = BrokerEmailAlias.Status.ACTIVE
        with self.assertRaises(ValidationError):
            al.save()

    def test_retired_token_never_reused(self):
        # A retired alias's local-part stays globally reserved (all-rows unique) — a fresh mint can't reuse it.
        a = _acct()
        al = mint_alias_for_account(a)
        retire_alias_for_account(a)
        with self.assertRaises(Exception):
            BrokerEmailAlias.objects.create(
                alias_local=al.alias_local, domain_at_creation=alias_domain(),
                user=a.user, status=BrokerEmailAlias.Status.PENDING)


class AliasRetireSavepointTests(TestCase):
    """Regression (WP1 review HIGH): a DatabaseError on the retire UPDATE must NOT poison the enclosing
    tombstone transaction. Without the inner savepoint, ``QuerySet.update()``'s mark_for_rollback_on_error sets
    ``connection.needs_rollback`` on the outer atomic; the swallowed exception can't clear it, so the whole
    tombstone silently rolls back while removal reports success — leaving the account LIVE with credentials."""

    def test_retire_db_error_does_not_poison_enclosing_tombstone_txn(self):
        a = _acct()
        mint_alias_for_account(a)
        # Stand in for remove_account's tombstone: an outer atomic with a sibling write that MUST survive the
        # alias retire's DB error. Patching SQLUpdateCompiler.execute_sql to raise drives the error through the
        # exact mark_for_rollback_on_error path a real lock_timeout / missing-relation would.
        with transaction.atomic():
            TradingAccount.objects.filter(pk=a.pk).update(name="TOMBSTONE_MARKER")   # sibling tombstone mutation
            with mock.patch.object(SQLUpdateCompiler, "execute_sql",
                                   side_effect=DatabaseError("simulated failure on alias retire UPDATE")):
                rc = retire_alias_for_account(a)        # swallows + returns 0, WITHOUT marking the outer txn
            self.assertEqual(rc, 0)
            # The outer transaction must still be usable (needs_rollback cleared by the savepoint): this write,
            # run AFTER the error and OUTSIDE the mock, would raise TransactionManagementError if poisoned.
            TradingAccount.objects.filter(pk=a.pk).update(is_active=False)
        # Committed: both sibling writes persisted -> the tombstone was NOT silently reverted.
        a.refresh_from_db()
        self.assertEqual(a.name, "TOMBSTONE_MARKER")
        self.assertFalse(a.is_active)
        # The alias itself stayed un-retired (its UPDATE was rolled back to the savepoint) — the retire is a
        # best-effort no-op on DB error, never a partial write.
        self.assertEqual(BrokerEmailAlias.objects.get(trading_account=a).status, BrokerEmailAlias.Status.ACTIVE)


class AliasFlagGateTests(TestCase):
    def test_ensure_is_noop_when_flag_off(self):
        a = _acct()
        ensure_broker_email_alias(a)                       # BROKER_EMAIL_IDENTITY_ENABLED default OFF
        self.assertFalse(BrokerEmailAlias.objects.filter(trading_account=a).exists())
        retire_broker_email_alias(a)                       # also a no-op, never raises

    @override_settings(BROKER_EMAIL_IDENTITY_ENABLED=True)
    def test_ensure_mints_when_flag_on(self):
        a = _acct()
        ensure_broker_email_alias(a)
        al = BrokerEmailAlias.objects.get(trading_account=a)
        self.assertEqual(al.status, BrokerEmailAlias.Status.ACTIVE)
        ensure_broker_email_alias(a)                       # idempotent
        self.assertEqual(BrokerEmailAlias.objects.filter(trading_account=a).count(), 1)

    @override_settings(BROKER_EMAIL_IDENTITY_ENABLED=True, BROKER_EMAIL_ALIAS_DOMAIN="pilot.accounts.guvfx.com")
    def test_domain_is_config_driven_and_pinned(self):
        a = _acct()
        al = mint_alias_for_account(a)
        self.assertEqual(al.domain_at_creation, "pilot.accounts.guvfx.com")
        self.assertTrue(al.address().endswith("@pilot.accounts.guvfx.com"))
