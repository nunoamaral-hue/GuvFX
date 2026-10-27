"""Broker-Server Environment Governance Correction (Scope D certification, 2026-10-12).

Proves the fix for the first-registration environment-poisoning defect: a broker server's demo/live classification
is AUTHORITATIVE from the verified ``BrokerServerCatalogue`` — never established from the first registering
account's ``account_type``, never guessed from a server-name suffix. Covers every Scope-D scenario:

  * LIVE-on-verified-live and DEMO-on-verified-demo succeed and stamp ``environment_source='catalogue'``.
  * demo/live mismatch against the verified environment fails closed (REQ_ENV_MISMATCH), no account created.
  * a genuinely-new (uncatalogued, no-row) server: STRICT flag ON -> fail closed (REQ_SERVER_UNVERIFIED);
    STRICT flag OFF (default) -> legacy auto-create marked ``inferred`` (compat preserved), never defaulting DEMO.
  * an EXISTING row that disagrees with the verified catalogue (the IS6 poisoning reproduction) fails closed with
    REQ_SERVER_METADATA_INCONSISTENT and is left UNTOUCHED; after a governed correction, the LIVE account onboards.
  * existing-account compat (legacy inferred path), cross-user isolation, case-insensitive collisions, concurrent
    registration, duplicate prevention, workspace/classification compat, and the resolver/normalisation units.

No monitoring/execution/ingestion is enabled; no order is placed; no production record is touched.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from hosted_workspace import provisioning as P
from trading.account_policy import Environment, account_environment
from trading.models import BrokerServer, BrokerServerCatalogue, TradingAccount
from trading.server_environment import (CATALOGUE, UNKNOWN, normalise_environment, normalise_server_name,
                                        resolve_verified_environment, server_name_collisions, server_name_key,
                                        server_name_suffix_hint)

User = get_user_model()

# Subsystem visible + admission open (mirrors tests_d5). LIVE onboarding + STRICT applied per-test (default OFF).
BASE = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
            CLOSED_BETA_OPEN_ACCESS_ENABLED=True)
LIVE_ON = dict(BASE, HOSTED_LIVE_ONBOARDING_ENABLED=True)
STRICT = dict(BASE, BROKER_SERVER_CATALOGUE_STRICT_ENABLED=True)
STRICT_LIVE = dict(LIVE_ON, BROKER_SERVER_CATALOGUE_STRICT_ENABLED=True)

ADD_URL = "/api/hosted-workspace/accounts/add/"

# The migration seeds these verified pilot servers into the catalogue; tests rely on that real topology.
IS6_LIVE = "IS6Technologies-Live"
IS6_DEMO = "IS6Technologies-Demo"
TW_LIVE = "TradersWay-Live"
VANTAGE_LIVE = "VantageMarkets-Live 10"  # added additively by migration 0021 (operator-verified live)


def _u(n):
    return User.objects.create_user(username=f"bsg_{n}", email=f"bsg_{n}@x.invalid", password="x")


def _req(user, login, server, is_demo):
    return P.request_hosted_workspace(user, expected_login=login, expected_server=server,
                                      broker_name="B", is_demo=is_demo)


# ---------------------------------------------------------------------------
# Resolver / normalisation units (Scope A primitives) — pure, read-only.
# ---------------------------------------------------------------------------
class ResolverUnitTests(TestCase):
    def test_seeded_catalogue_is_authoritative(self):
        self.assertEqual(resolve_verified_environment(IS6_LIVE), (BrokerServer.LIVE, CATALOGUE))
        self.assertEqual(resolve_verified_environment(IS6_DEMO), (BrokerServer.DEMO, CATALOGUE))
        self.assertEqual(resolve_verified_environment(TW_LIVE), (BrokerServer.LIVE, CATALOGUE))

    def test_resolution_is_case_insensitive(self):
        self.assertEqual(resolve_verified_environment("is6technologies-live"), (BrokerServer.LIVE, CATALOGUE))
        self.assertEqual(resolve_verified_environment("  IS6TECHNOLOGIES-LIVE  "), (BrokerServer.LIVE, CATALOGUE))

    def test_unknown_server_fails_closed(self):
        self.assertEqual(resolve_verified_environment("Totally-New-Srv"), (None, UNKNOWN))
        self.assertEqual(resolve_verified_environment(""), (None, UNKNOWN))
        self.assertEqual(resolve_verified_environment(None), (None, UNKNOWN))

    def test_unverified_catalogue_entry_is_not_authoritative(self):
        BrokerServerCatalogue.objects.create(server_name="Pending-Live", environment=BrokerServer.LIVE,
                                             is_verified=False)
        self.assertEqual(resolve_verified_environment("Pending-Live"), (None, UNKNOWN))

    def test_normalise_environment_canonicalises_or_fails_closed(self):
        self.assertEqual(normalise_environment("DEMO"), BrokerServer.DEMO)
        self.assertEqual(normalise_environment(" Live "), BrokerServer.LIVE)
        self.assertIsNone(normalise_environment("paper"))
        self.assertIsNone(normalise_environment(""))

    def test_name_key_and_normalise(self):
        self.assertEqual(normalise_server_name("  A   B  "), "A B")
        self.assertEqual(server_name_key("  IS6Technologies-Live "), "is6technologies-live")

    def test_suffix_hint_is_advisory_only(self):
        # The hint exists for operator review; it is NEVER consulted by provisioning (asserted in SuffixTests).
        self.assertEqual(server_name_suffix_hint("Foo-Live"), BrokerServer.LIVE)
        self.assertEqual(server_name_suffix_hint("Foo-Demo"), BrokerServer.DEMO)
        self.assertEqual(server_name_suffix_hint("Foo-Trade"), BrokerServer.LIVE)
        self.assertIsNone(server_name_suffix_hint("Foo"))


class CatalogueModelTests(TestCase):
    def test_save_normalises_name_and_environment(self):
        c = BrokerServerCatalogue.objects.create(server_name="  New-Live  ", environment="LIVE")
        self.assertEqual(c.server_name, "New-Live")
        self.assertEqual(c.name_key, "new-live")
        self.assertEqual(c.environment, BrokerServer.LIVE)

    def test_save_rejects_unrecognised_environment(self):
        with self.assertRaises(ValidationError):
            BrokerServerCatalogue.objects.create(server_name="Bad", environment="paper")


# ---------------------------------------------------------------------------
# Provisioning: catalogue-authoritative classification (the core correction).
# ---------------------------------------------------------------------------
@override_settings(**LIVE_ON)
class CatalogueAuthoritativeTests(TestCase):
    def test_live_on_verified_live_creates_live_from_catalogue(self):
        res = _req(_u("vl"), "100", IS6_LIVE, is_demo=False)
        self.assertTrue(res.ok, res.reason)
        acct = res.workspace.trading_account
        self.assertFalse(acct.is_demo)
        self.assertEqual(acct.broker_server.environment, BrokerServer.LIVE)
        self.assertEqual(acct.broker_server.environment_source, BrokerServer.CATALOGUE)
        self.assertEqual(account_environment(acct), Environment.LIVE)   # classifies cleanly as LIVE

    def test_demo_on_verified_demo_creates_demo_from_catalogue(self):
        res = _req(_u("vd"), "101", IS6_DEMO, is_demo=True)
        self.assertTrue(res.ok, res.reason)
        acct = res.workspace.trading_account
        self.assertTrue(acct.is_demo)
        self.assertEqual(acct.broker_server.environment, BrokerServer.DEMO)
        self.assertEqual(acct.broker_server.environment_source, BrokerServer.CATALOGUE)
        self.assertEqual(account_environment(acct), Environment.DEMO)

    def test_demo_account_on_verified_live_fails_closed(self):
        before = TradingAccount.objects.count()
        res = _req(_u("dvl"), "102", IS6_LIVE, is_demo=True)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_ENV_MISMATCH)
        self.assertEqual(TradingAccount.objects.count(), before)        # never created
        self.assertFalse(BrokerServer.objects.filter(server_name=IS6_LIVE).exists())  # no row materialised

    def test_live_account_on_verified_demo_fails_closed(self):
        before = TradingAccount.objects.count()
        res = _req(_u("lvd"), "103", IS6_DEMO, is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_ENV_MISMATCH)
        self.assertEqual(TradingAccount.objects.count(), before)

    def test_registration_never_mutates_verified_metadata(self):
        # Decision #6: an existing row that already mirrors the catalogue is reused, not rewritten; the catalogue
        # row is never touched by an account registration.
        BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.LIVE,
                                    environment_source=BrokerServer.CATALOGUE)
        cat_before = BrokerServerCatalogue.objects.get(name_key=server_name_key(IS6_LIVE)).updated_at
        res = _req(_u("nomut"), "104", IS6_LIVE, is_demo=False)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(BrokerServer.objects.filter(server_name=IS6_LIVE).count(), 1)
        self.assertEqual(BrokerServer.objects.get(server_name=IS6_LIVE).environment, BrokerServer.LIVE)
        self.assertEqual(BrokerServerCatalogue.objects.get(name_key=server_name_key(IS6_LIVE)).updated_at,
                         cat_before)


# ---------------------------------------------------------------------------
# The IS6 poisoning reproduction + governed correction.
# ---------------------------------------------------------------------------
@override_settings(**LIVE_ON)
class PoisonedRowReproductionTests(TestCase):
    def test_existing_demo_poisoned_row_blocks_live_with_clear_reason(self):
        # Reproduce production: IS6Technologies-Live (a LIVE server) was stamped DEMO by a demo beta account.
        poisoned = BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.DEMO,
                                               environment_source=BrokerServer.INFERRED)
        before = TradingAccount.objects.count()
        res = _req(_u("poison"), "51763830", IS6_LIVE, is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_SERVER_METADATA_INCONSISTENT)   # actionable, not a bare mismatch
        self.assertEqual(TradingAccount.objects.count(), before)
        poisoned.refresh_from_db()
        self.assertEqual(poisoned.environment, BrokerServer.DEMO)          # row left UNTOUCHED (no silent rewrite)

    def test_after_governed_correction_live_onboards_and_demo_is_refused(self):
        # A governed operator correction re-points the row to the verified value (source=operator).
        BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        row = BrokerServer.objects.get(server_name=IS6_LIVE)
        row.environment = BrokerServer.LIVE
        row.environment_source = BrokerServer.OPERATOR
        row.save(update_fields=["environment", "environment_source"])
        # Now the LIVE account onboards cleanly...
        res = _req(_u("fixed"), "51763830", IS6_LIVE, is_demo=False)
        self.assertTrue(res.ok, res.reason)
        self.assertFalse(res.workspace.trading_account.is_demo)
        # ...and a DEMO account on the (now correctly LIVE) server is refused.
        res2 = _req(_u("fixeddemo"), "999", IS6_LIVE, is_demo=True)
        self.assertFalse(res2.ok)
        self.assertEqual(res2.reason, P.REQ_ENV_MISMATCH)


# ---------------------------------------------------------------------------
# Genuinely-new (uncatalogued, no-row) server: strict vs legacy-compat.
# ---------------------------------------------------------------------------
class UnknownServerTests(TestCase):
    @override_settings(**STRICT)
    def test_strict_mode_fails_closed_on_unknown(self):
        before = TradingAccount.objects.count()
        res = _req(_u("strict"), "200", "Brand-New-Demo", is_demo=True)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_SERVER_UNVERIFIED)
        self.assertEqual(TradingAccount.objects.count(), before)
        self.assertFalse(BrokerServer.objects.filter(server_name="Brand-New-Demo").exists())

    @override_settings(**STRICT_LIVE)
    def test_strict_mode_fails_closed_on_unknown_live(self):
        res = _req(_u("strictlive"), "201", "Brand-New-Live", is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_SERVER_UNVERIFIED)

    @override_settings(**BASE)
    def test_legacy_mode_auto_creates_marked_inferred(self):
        res = _req(_u("legacy"), "202", "Brand-New-Demo", is_demo=True)
        self.assertTrue(res.ok, res.reason)
        srv = res.workspace.trading_account.broker_server
        self.assertEqual(srv.environment, BrokerServer.DEMO)
        self.assertEqual(srv.environment_source, BrokerServer.INFERRED)   # unverified, never authoritative

    @override_settings(**STRICT)
    def test_strict_mode_still_serves_a_catalogued_server(self):
        # Strict only tightens the UNKNOWN branch; a verified server still onboards.
        res = _req(_u("strictcat"), "203", IS6_DEMO, is_demo=True)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.workspace.trading_account.broker_server.environment_source, BrokerServer.CATALOGUE)


@override_settings(**STRICT_LIVE)
class SuffixNotAuthoritativeTests(TestCase):
    def test_live_suffix_on_unknown_server_is_not_trusted(self):
        # Decision #2: a "-Live" suffix is a hint, never proof. In strict mode an uncatalogued "-Live" server is
        # refused (not auto-classified LIVE from its name).
        res = _req(_u("suffix"), "300", "MysteryBroker-Live", is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_SERVER_UNVERIFIED)

    @override_settings(BROKER_SERVER_CATALOGUE_STRICT_ENABLED=False)
    def test_demo_account_on_live_suffixed_unknown_creates_demo_not_live(self):
        # Legacy mode: the account's EXPLICIT demo type wins over the "-Live" suffix — no suffix classification.
        res = _req(_u("suffix2"), "301", "MysteryBroker-Live", is_demo=True)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.workspace.trading_account.broker_server.environment, BrokerServer.DEMO)


# ---------------------------------------------------------------------------
# Existing-account compat (legacy inferred row) + cross-user isolation.
# ---------------------------------------------------------------------------
@override_settings(**LIVE_ON)
class LegacyRowCompatTests(TestCase):
    def test_existing_inferred_row_consistent_account_proceeds(self):
        BrokerServer.objects.create(server_name="Legacy-Demo-Srv", environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        res = _req(_u("lc1"), "400", "Legacy-Demo-Srv", is_demo=True)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.workspace.trading_account.broker_server.environment_source, BrokerServer.INFERRED)

    def test_existing_inferred_row_mismatch_fails_closed(self):
        BrokerServer.objects.create(server_name="Legacy-Demo-Srv", environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        res = _req(_u("lc2"), "401", "Legacy-Demo-Srv", is_demo=False)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.REQ_ENV_MISMATCH)

    def test_cross_user_registration_does_not_leak_or_mutate(self):
        a = _req(_u("iso_a"), "500", IS6_DEMO, is_demo=True)
        b = _req(_u("iso_b"), "501", IS6_DEMO, is_demo=True)
        self.assertTrue(a.ok and b.ok)
        # distinct accounts, distinct workspaces, same shared server row — unchanged env
        self.assertNotEqual(a.workspace.trading_account.user_id, b.workspace.trading_account.user_id)
        self.assertNotEqual(a.workspace.pk, b.workspace.pk)
        self.assertEqual(BrokerServer.objects.filter(server_name=IS6_DEMO).count(), 1)
        self.assertEqual(BrokerServer.objects.get(server_name=IS6_DEMO).environment, BrokerServer.DEMO)


# ---------------------------------------------------------------------------
# Case-insensitive collisions + duplicate prevention.
# ---------------------------------------------------------------------------
class CollisionAndDuplicateTests(TestCase):
    def test_server_name_collisions_detects_case_variants(self):
        BrokerServer.objects.create(server_name="TradersWay-Live", environment=BrokerServer.LIVE)
        BrokerServer.objects.create(server_name="Tradersway-Live", environment=BrokerServer.LIVE)
        BrokerServer.objects.create(server_name="Unique-Demo", environment=BrokerServer.DEMO)
        collisions = server_name_collisions()
        self.assertIn("tradersway-live", collisions)
        self.assertEqual(collisions["tradersway-live"], sorted(["Tradersway-Live", "TradersWay-Live"]))
        self.assertNotIn("unique-demo", collisions)

    def test_catalogue_name_key_is_unique_case_insensitively(self):
        from django.db import IntegrityError, transaction
        BrokerServerCatalogue.objects.create(server_name="Dup-Live", environment=BrokerServer.LIVE)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BrokerServerCatalogue.objects.create(server_name="dup-live", environment=BrokerServer.LIVE)


# ---------------------------------------------------------------------------
# Concurrent registration of the SAME new verified server by two users.
# ---------------------------------------------------------------------------
@override_settings(**dict(LIVE_ON, CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True))
class ConcurrentCatalogueCreateTests(TransactionTestCase):
    reset_sequences = True

    def test_two_users_race_same_verified_server_one_row(self):
        import threading
        from django.db import connection
        # Seed the catalogue in this TransactionTestCase (migration seed is present, but assert via create to be
        # explicit and resilient to ordering): IS6_LIVE is already verified-live from the migration.
        u1 = _u("race1")
        u2 = _u("race2")
        barrier = threading.Barrier(2)
        results = {}

        def worker(name, user, login):
            barrier.wait()
            try:
                res = _req(user, login, IS6_LIVE, is_demo=False)
                results[name] = "ok" if res.ok else res.reason
            except Exception as e:  # noqa: BLE001
                results[name] = f"err:{e}"
            finally:
                connection.close()

        t1 = threading.Thread(target=worker, args=("t1", u1, "600"))
        t2 = threading.Thread(target=worker, args=("t2", u2, "601"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results, {"t1": "ok", "t2": "ok"}, results)      # no IntegrityError, both succeed
        self.assertEqual(BrokerServer.objects.filter(server_name=IS6_LIVE).count(), 1)  # exactly one row
        self.assertEqual(BrokerServer.objects.get(server_name=IS6_LIVE).environment, BrokerServer.LIVE)


# ---------------------------------------------------------------------------
# View-level: the 409 responses carry clear, actionable reasons.
# ---------------------------------------------------------------------------
class ViewMessageTests(TestCase):
    @override_settings(**STRICT)
    def test_unverified_server_returns_409(self):
        u = _u("vw1"); c = APIClient(); c.force_authenticate(u)
        r = c.post(ADD_URL, {"broker_name": "B", "expected_login": "700",
                             "expected_server": "No-Such-Srv", "account_type": "demo"}, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["reason"], P.REQ_SERVER_UNVERIFIED)

    @override_settings(**LIVE_ON)
    def test_metadata_inconsistent_returns_409(self):
        BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        u = _u("vw2"); c = APIClient(); c.force_authenticate(u)
        r = c.post(ADD_URL, {"broker_name": "B", "expected_login": "701",
                             "expected_server": IS6_LIVE, "account_type": "live"}, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(r.data["reason"], P.REQ_SERVER_METADATA_INCONSISTENT)


# ---------------------------------------------------------------------------
# Migration data outcome: seed present + existing env casing normalised.
# ---------------------------------------------------------------------------
class MigrationSeedTests(TestCase):
    def test_pilot_catalogue_seeded_verified(self):
        for name, env in [(IS6_DEMO, BrokerServer.DEMO), (IS6_LIVE, BrokerServer.LIVE),
                          ("TradersWay-Demo", BrokerServer.DEMO), (TW_LIVE, BrokerServer.LIVE)]:
            e = BrokerServerCatalogue.objects.get(name_key=server_name_key(name))
            self.assertEqual(e.environment, env)
            self.assertTrue(e.is_verified)

    def test_vantage_seed_additive_and_preserves_existing(self):
        """Migration 0021 adds Vantage (verified live) WITHOUT removing any 0020-seeded broker (additivity)."""
        v = BrokerServerCatalogue.objects.get(name_key=server_name_key(VANTAGE_LIVE))
        self.assertEqual(v.environment, BrokerServer.LIVE)
        self.assertTrue(v.is_verified)
        # The four original pilot entries must all survive, byte-for-byte verified — adding Vantage removes none.
        for name, env in [(IS6_DEMO, BrokerServer.DEMO), (IS6_LIVE, BrokerServer.LIVE),
                          ("TradersWay-Demo", BrokerServer.DEMO), (TW_LIVE, BrokerServer.LIVE)]:
            e = BrokerServerCatalogue.objects.get(name_key=server_name_key(name))
            self.assertEqual(e.environment, env)
            self.assertTrue(e.is_verified)
        # Resolver now classifies Vantage authoritatively as live from the catalogue.
        self.assertEqual(resolve_verified_environment(VANTAGE_LIVE), (BrokerServer.LIVE, CATALOGUE))


# ---------------------------------------------------------------------------
# bind_broker_identity — the SIBLING write path must honour the same governance
# (adversarial-review CONFIRMED HIGH: the demo bind branch bare-created a demo-default row and poisoned a
# verified-live server). Deferred-identity journey flag set (same as tests_deferred_identity_bind).
# ---------------------------------------------------------------------------
BIND_FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
                  HOSTED_MT5_EXECUTION_ENABLED=True, SUPERVISED_SINGLE_TENANT_BETA_ENABLED=True,
                  BETA_SELF_SERVE_ARM_ENABLED=True, HOSTED_DEFERRED_IDENTITY_BIND_ENABLED=True,
                  CLOSED_BETA_OPEN_ACCESS_ENABLED=True, BETA_RUNTIMES_ENABLED=True, BETA_MAX_TESTERS=1000)


@override_settings(**BIND_FLAGS)
class BindPathGovernanceTests(TestCase):
    def _unbound_demo_ws(self, n):
        from execution.models import TerminalNode
        user = User.objects.create_user(username=f"bind_{n}", email=f"bind_{n}@x.invalid", password="x")
        req = P.request_hosted_workspace(user, expected_login="", is_demo=True)
        self.assertTrue(req.ok, req.reason)
        TerminalNode.objects.get_or_create(hostname="bind-node-1",
                                           defaults=dict(status=TerminalNode.Status.ACTIVE,
                                                         rdp_host="10.60.0.9", max_accounts=1000))
        P.allocate_workspace_node(req.workspace)
        req.workspace.refresh_from_db()
        return user, req.workspace

    def test_demo_bind_to_verified_live_server_is_refused_no_poison(self):
        # THE FIX: a demo account binding a catalogue-verified LIVE server must fail closed and create NO row.
        user, ws = self._unbound_demo_ws("dvl")
        res = P.bind_broker_identity(user, ws, expected_login="800", expected_server=IS6_LIVE)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_LIVE_FORBIDDEN)
        self.assertFalse(BrokerServer.objects.filter(server_name=IS6_LIVE).exists())   # no poisoning row
        ws.trading_account.refresh_from_db()
        self.assertEqual((ws.trading_account.account_number or "").strip(), "")          # still UNBOUND

    def test_demo_bind_to_verified_demo_server_binds_from_catalogue(self):
        user, ws = self._unbound_demo_ws("dvd")
        res = P.bind_broker_identity(user, ws, expected_login="801", expected_server=IS6_DEMO)
        self.assertTrue(res.ok, res.reason)
        ws.trading_account.refresh_from_db()
        srv = ws.trading_account.broker_server
        self.assertEqual(srv.environment, BrokerServer.DEMO)
        self.assertEqual(srv.environment_source, BrokerServer.CATALOGUE)

    def test_demo_bind_to_verified_live_mismatch_fires_before_metadata_check(self):
        # A DEMO account on a verified-LIVE server is a demo/live disagreement — it must fail closed on the
        # mismatch (BIND_LIVE_FORBIDDEN) regardless of any existing row, never reaching the metadata branch.
        BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        user, ws = self._unbound_demo_ws("dmi")
        res = P.bind_broker_identity(user, ws, expected_login="802", expected_server=IS6_LIVE)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_LIVE_FORBIDDEN)
        self.assertEqual(BrokerServer.objects.get(server_name=IS6_LIVE).environment, BrokerServer.DEMO)  # untouched

    @override_settings(HOSTED_LIVE_MONITORING_ENABLED=True)
    def test_live_bind_against_poisoned_live_row_is_metadata_inconsistent(self):
        # A LIVE account (monitoring on) binding the verified-LIVE server whose existing row is poisoned demo:
        # desired==verified==live, so the mismatch guard passes and the row-vs-catalogue disagreement fails closed.
        BrokerServer.objects.create(server_name=IS6_LIVE, environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.INFERRED)
        user, ws = self._unbound_demo_ws("lmi")
        TradingAccount.objects.filter(pk=ws.trading_account_id).update(is_demo=False)  # make it LIVE
        ws.refresh_from_db()
        res = P.bind_broker_identity(user, ws, expected_login="805", expected_server=IS6_LIVE)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_SERVER_METADATA_INCONSISTENT)
        self.assertEqual(BrokerServer.objects.get(server_name=IS6_LIVE).environment, BrokerServer.DEMO)  # untouched

    @override_settings(BROKER_SERVER_CATALOGUE_STRICT_ENABLED=True)
    def test_strict_mode_bind_to_unknown_server_fails_closed(self):
        user, ws = self._unbound_demo_ws("dstrict")
        res = P.bind_broker_identity(user, ws, expected_login="803", expected_server="Unknown-Bind-Srv")
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, P.BIND_SERVER_UNVERIFIED)
        self.assertFalse(BrokerServer.objects.filter(server_name="Unknown-Bind-Srv").exists())

    def test_legacy_mode_bind_to_unknown_server_creates_inferred(self):
        user, ws = self._unbound_demo_ws("dlegacy")
        res = P.bind_broker_identity(user, ws, expected_login="804", expected_server="Unknown-Bind-Srv")
        self.assertTrue(res.ok, res.reason)
        ws.trading_account.refresh_from_db()
        srv = ws.trading_account.broker_server
        self.assertEqual(srv.environment, BrokerServer.DEMO)
        self.assertEqual(srv.environment_source, BrokerServer.INFERRED)   # compat: legacy auto-create preserved


# ---------------------------------------------------------------------------
# Case-variant idempotency (adversarial-review CONFIRMED LOW: iexact resolution + exact idempotency key could
# reach the create and raise a 500 on a case-variant re-request).
# ---------------------------------------------------------------------------
@override_settings(**dict(BASE, CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED=True))
class CaseVariantIdempotencyTests(TestCase):
    def setUp(self):
        from billing.models import UserSubscriptionState
        from trading.account_entitlement import grant_concurrent_enforcement
        self.user = User.objects.create_user(username="casevar", email="casevar@x.invalid", password="x")
        UserSubscriptionState.objects.update_or_create(user=self.user, defaults=dict(
            current_plan=UserSubscriptionState.Plan.BETA,
            plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False))
        grant_concurrent_enforcement(self.user)

    def test_case_variant_rerequest_is_idempotent_not_500(self):
        first = _req(self.user, "900", IS6_DEMO, is_demo=True)
        self.assertTrue(first.ok, first.reason)
        # same login, case-variant server name — must resolve to the SAME workspace, never a second account / 500
        again = _req(self.user, "900", IS6_DEMO.lower(), is_demo=True)
        self.assertTrue(again.ok, again.reason)
        self.assertFalse(again.created)
        self.assertEqual(again.reason, P.REQ_EXISTS)
        self.assertEqual(again.workspace.pk, first.workspace.pk)
        self.assertEqual(TradingAccount.objects.filter(user=self.user, account_number="900").count(), 1)
