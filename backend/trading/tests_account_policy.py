"""trading.account_policy unit tests (Stream D1) — the authoritative DEMO/LIVE policy + fail-closed consistency."""
from django.test import SimpleTestCase

from trading.account_policy import (
    AccountEnvironmentIntegrityError, Environment, account_environment,
    demo_accounts_q, is_demo_environment, is_live_environment)
from trading.models import BrokerServer


class _Acct:
    """Lightweight stand-in (policy reads only .is_demo + .broker_server)."""
    def __init__(self, is_demo, broker_server=None):
        self.is_demo = is_demo
        self.broker_server = broker_server


def _srv(env):
    return BrokerServer(server_name=f"S-{env}", environment=env)


class AccountPolicyTests(SimpleTestCase):
    def test_demo_no_server(self):
        a = _Acct(True, None)
        self.assertEqual(account_environment(a), Environment.DEMO)
        self.assertTrue(is_demo_environment(a))
        self.assertFalse(is_live_environment(a))

    def test_live_no_server(self):
        a = _Acct(False, None)
        self.assertEqual(account_environment(a), Environment.LIVE)
        self.assertFalse(is_demo_environment(a))
        self.assertTrue(is_live_environment(a))

    def test_demo_consistent_server(self):
        self.assertEqual(account_environment(_Acct(True, _srv(BrokerServer.DEMO))), Environment.DEMO)

    def test_live_consistent_server(self):
        self.assertEqual(account_environment(_Acct(False, _srv(BrokerServer.LIVE))), Environment.LIVE)

    def test_mismatch_demo_account_live_server_fails_closed(self):
        a = _Acct(True, _srv(BrokerServer.LIVE))          # account says demo, server says live
        with self.assertRaises(AccountEnvironmentIntegrityError):
            account_environment(a)
        self.assertFalse(is_demo_environment(a))          # fail-closed: NOT treated as demo
        self.assertFalse(is_live_environment(a))          # fail-closed: NOT treated as a clean live either

    def test_mismatch_live_account_demo_server_fails_closed(self):
        a = _Acct(False, _srv(BrokerServer.DEMO))          # account says live, server says demo
        self.assertFalse(is_demo_environment(a))
        self.assertFalse(is_live_environment(a))

    def test_unrecognised_server_env_fails_closed(self):
        a = _Acct(True, _srv("weird"))
        self.assertFalse(is_demo_environment(a))           # never silently treat an unknown env as demo

    def test_stale_broker_server_fk_fails_closed(self):
        # A bound broker_server_id whose BrokerServer row is gone (stale/orphaned FK) makes the FK descriptor
        # raise DoesNotExist, NOT AttributeError. The policy must fail CLOSED (sanitised integrity error) and
        # the gate helpers must return False — never propagate DoesNotExist and crash a DEMO-lifecycle caller.
        class _StaleAcct:
            is_demo = True

            @property
            def broker_server(self):
                raise BrokerServer.DoesNotExist("orphaned FK")

        a = _StaleAcct()
        with self.assertRaises(AccountEnvironmentIntegrityError):
            account_environment(a)
        self.assertFalse(is_demo_environment(a))
        self.assertFalse(is_live_environment(a))

    def test_demo_accounts_q(self):
        from django.db.models import Q
        self.assertEqual(demo_accounts_q(), Q(is_demo=True))
        self.assertEqual(demo_accounts_q("account__"), Q(account__is_demo=True))
