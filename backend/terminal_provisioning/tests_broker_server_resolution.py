"""ADR-0025 — automated broker-server resolution.

The provisioning login path now consumes the server the customer ALREADY submitted through the frontend:
the normalised ``broker_server`` FK when present, else the free-text ``broker_name`` (the "Add Trading
Account" form's "Broker server name" field). So the beta journey — enter login/server/password once, then
GuvFX provisions automatically — needs no operator or customer re-entry. Missing / conflicting servers fail
closed. Credentials are never read, printed, logged, or persisted in plaintext by the resolver.

No real customer credentials appear here — ``SECRET`` is a synthetic test password.
"""
import io
import logging

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.models import BetaTester
from trading.crypto import encrypt_password
from trading.models import BrokerServer, TradingAccount

from terminal_provisioning import beta_capacity as cap
from terminal_provisioning.models import (
    ProvisioningJob, ProvisioningVerificationReport, RuntimeState)
from terminal_provisioning.provisioner import (
    FakeProvisioner, _expected_login_server, advance_provisioning_job, enqueue_op, resolve_broker_server)

U = get_user_model()
SECRET = "s3cret-broker-pw!never-real"   # synthetic — NEVER a real customer credential
REQUIRE_LOGIN = override_settings(BETA_RUNTIMES_ENABLED=True, PROVISIONING_REQUIRE_BROKER_LOGIN=True,
                                  BETA_MAX_TESTERS=1000)
_CTR = {"n": 0}


class _Rt:
    """Minimal runtime stand-in for the pure ``_expected_login_server`` (reads only ``trading_account``)."""
    def __init__(self, acct):
        self.trading_account = acct


def _account(*, server_name=None, broker_name="", account_number=None, is_demo=True, password=SECRET):
    _CTR["n"] += 1
    n = _CTR["n"]
    u = U.objects.create_user(username=f"bsr{n}", email=f"bsr{n}@x.invalid", password="x")
    BetaTester.objects.create(email=f"bsr{n}@x.invalid")
    fk = (BrokerServer.objects.create(broker_display_name=f"BD{n}", server_name=server_name)
          if server_name else None)
    return TradingAccount.objects.create(
        user=u, name=f"A{n}", account_number=account_number or str(90000 + n),
        broker_server=fk, broker_name=broker_name, is_demo=is_demo,
        password_enc=encrypt_password(password))


@override_settings(BETA_MAX_TESTERS=1000)
class BrokerServerResolverUnitTests(TestCase):
    # Scenario 1
    def test_1_normalised_fk_present(self):
        self.assertEqual(resolve_broker_server(_account(server_name="TradersWay-Demo")),
                         ("TradersWay-Demo", None))

    # Scenario 2
    def test_2_fk_absent_free_text_present(self):
        acct = _account(broker_name="IS6Technologies-Demo")
        self.assertIsNone(acct.broker_server_id)
        self.assertEqual(resolve_broker_server(acct), ("IS6Technologies-Demo", None))

    # Scenario 3 — a PERSISTED account can never be both-absent (DB constraint ``brokeridentity_present``
    # + the create serializer both require one); this is the resolver's defense-in-depth layer, tested
    # in-memory so it stays correct even if an unconstrained object ever reaches it.
    def test_3_both_absent_fails_closed_missing(self):
        acct = TradingAccount(broker_server=None, broker_name="")
        self.assertEqual(resolve_broker_server(acct), (None, "broker_server_missing"))

    # Scenario 4 — whitespace-only broker_name is not a server; the resolver trims it to empty → missing.
    # (The model strips broker_name on save, so the DB constraint also rejects a persisted whitespace-only
    # account; tested in-memory so the resolver's own trimming is proven independent of that.)
    def test_4_whitespace_only_free_text_is_missing(self):
        acct = TradingAccount(broker_server=None, broker_name="   \t ")
        self.assertEqual(resolve_broker_server(acct), (None, "broker_server_missing"))

    # Scenario 5 — equal (case/space-insensitive) → canonical FK form, NOT a conflict
    def test_5_both_present_equal_returns_canonical_fk(self):
        acct = _account(server_name="IS6Technologies-Demo", broker_name="  is6technologies-demo ")
        self.assertEqual(resolve_broker_server(acct), ("IS6Technologies-Demo", None))

    # Scenario 6 — both present but DIFFERENT: the normalised FK wins deterministically (broker_name is
    # dual-use free text — often a broker DISPLAY name on a normalised account — so it is never compared
    # against the curated server; this preserves the prior FK-only behaviour and never false-blocks).
    def test_6_both_present_fk_wins_deterministically(self):
        acct = _account(server_name="TradersWay-Demo", broker_name="IS6 Technologies LTD")
        self.assertEqual(resolve_broker_server(acct), ("TradersWay-Demo", None))

    # Scenario 7 — the exact production Customer Zero shape
    def test_7_customer_zero_shaped_record(self):
        acct = _account(account_number="1302575", broker_name="IS6Technologies-Demo", is_demo=True)
        self.assertIsNone(acct.broker_server_id)      # FK null
        self.assertTrue(acct.password_enc)            # encrypted password present
        self.assertTrue(acct.is_demo)                 # demo
        self.assertEqual(resolve_broker_server(acct), ("IS6Technologies-Demo", None))
        self.assertEqual(_expected_login_server(_Rt(acct)), ("1302575", "IS6Technologies-Demo"))

    # Scenario 8 — no credential value is read, printed or logged by the resolver
    def test_8_resolver_never_touches_or_logs_credentials(self):
        acct = _account(account_number="1302575", broker_name="IS6Technologies-Demo")
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(h)
        try:
            server, reason = resolve_broker_server(acct)
        finally:
            root.removeHandler(h)
        self.assertEqual((server, reason), ("IS6Technologies-Demo", None))
        self.assertNotIn(SECRET, buf.getvalue())               # nothing logged
        self.assertNotIn(SECRET, str(server))                  # secret never in the output
        # the resolver reads ONLY non-secret server identifiers — prove it works on an object with NO
        # password/account_number attributes at all (would AttributeError if it reached for a credential).
        class _Bare:
            broker_server = None
            broker_name = "IS6Technologies-Demo"
        self.assertEqual(resolve_broker_server(_Bare()), ("IS6Technologies-Demo", None))

    # Scenario 9 — no plaintext password is persisted; the resolver is read-only
    def test_9_no_plaintext_password_and_resolver_is_read_only(self):
        acct = _account(broker_name="IS6Technologies-Demo", password=SECRET)
        self.assertEqual(acct.broker_password, "")             # plaintext field never populated
        self.assertNotIn(SECRET, acct.password_enc)            # stored value is encrypted, not plaintext
        before = (acct.broker_name, acct.broker_server_id, acct.password_enc, acct.account_number)
        resolve_broker_server(acct)
        acct.refresh_from_db()
        self.assertEqual((acct.broker_name, acct.broker_server_id, acct.password_enc, acct.account_number),
                         before)                                # resolver mutated nothing

    # Scenario 10 — a normalised (production-shaped) account is unchanged: FK wins, free-text ignored
    def test_10_production_shaped_account_unchanged(self):
        acct = _account(server_name="RealBroker-Live01", broker_name="")
        self.assertEqual(resolve_broker_server(acct), ("RealBroker-Live01", None))
        # even a stray free-text value that AGREES is fine and still resolves the canonical FK
        acct2 = _account(server_name="RealBroker-Live02", broker_name="RealBroker-Live02")
        self.assertEqual(resolve_broker_server(acct2), ("RealBroker-Live02", None))


@REQUIRE_LOGIN
class BrokerServerResolutionIntegrationTests(TestCase):
    """End-to-end under PROVISIONING_REQUIRE_BROKER_LOGIN=1: the customer-entered free-text server drives a
    genuinely verified broker login, with no re-entry — and missing/conflicting servers fail closed."""

    def _drive(self, acct, verify_result=None):
        rt = cap.reserve_beta_slot(acct)
        p = FakeProvisioner(verify_result=verify_result)
        job = enqueue_op(rt, ProvisioningJob.Op.PROVISION)
        job = advance_provisioning_job(job, p)
        rt.refresh_from_db()
        return rt, job, p

    def test_customer_zero_shape_reaches_running_via_free_text_server(self):
        acct = _account(account_number="1302575", broker_name="IS6Technologies-Demo", is_demo=True)
        rt, job, p = self._drive(acct)
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        cfg = next(c for c in p.calls if c[0] == "configure")   # (configure, login, server, bool(pw))
        self.assertEqual((cfg[1], cfg[2]), ("1302575", "IS6Technologies-Demo"))
        self.assertTrue(cfg[3])                                  # a password WAS passed to configure
        rep = ProvisioningVerificationReport.objects.get(runtime=rt)
        self.assertTrue(rep.broker_login_verified)

    def test_normalised_fk_wins_over_free_text_reaches_running(self):
        # both present + different: the normalised FK is authoritative — login uses it, broker_name ignored;
        # no regression to normalised accounts that also carry a display-style broker_name.
        acct = _account(server_name="TradersWay-Demo", broker_name="IS6 Technologies LTD")
        rt, job, p = self._drive(acct)
        self.assertEqual(rt.state, RuntimeState.RUNNING)
        self.assertEqual(job.status, ProvisioningJob.Status.DONE)
        cfg = next(c for c in p.calls if c[0] == "configure")
        self.assertEqual(cfg[2], "TradersWay-Demo")            # FK server used, not the free-text
        self.assertTrue(ProvisioningVerificationReport.objects.get(runtime=rt).broker_login_verified)
