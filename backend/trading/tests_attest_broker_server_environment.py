"""GFX-DOMINION-SERVER-ATTESTATION-001 — the governed broker-server environment attestation command.

Proves: dry-run writes nothing; --apply creates the verified catalogue entry + reconciles the mirrored BrokerServer
row to live/operator; idempotent re-apply; FAIL-CLOSED on authoritative contradiction (verified catalogue or an
operator-stamped row that disagrees); no other server / account / workspace is touched; and, after the correction,
a LIVE onboarding request for the server proceeds while a DEMO request still fails closed. Observation-only — the
command performs no broker login, order, or account/workspace creation.
"""
from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from hosted_workspace import provisioning as P
from trading.models import BrokerServer, BrokerServerCatalogue, TradingAccount
from trading.server_environment import resolve_verified_environment

User = get_user_model()

SERVER = "DominionMarkets-Live"
EVIDENCE = "Sponsor-attested broker-issued account evidence dated 2026-11-04 (GFX-DOMINION-SERVER-ATTESTATION-001)"
LIVE_ON = dict(HOSTED_PERSISTENT_MT5_ENABLED=True, HOSTED_WORKSPACE_ONBOARDING_ENABLED=True,
               CLOSED_BETA_OPEN_ACCESS_ENABLED=True, HOSTED_LIVE_ONBOARDING_ENABLED=True)


def _inferred_demo_row(name=SERVER):
    return BrokerServer.objects.create(server_name=name, environment=BrokerServer.DEMO,
                                       environment_source=BrokerServer.INFERRED)


def _run(environment="live", server=SERVER, evidence=EVIDENCE, **extra):
    out = StringIO()
    call_command("attest_broker_server_environment", server=server, environment=environment, evidence=evidence,
                 stdout=out, stderr=StringIO(), **extra)
    return out.getvalue()


class AttestDryRunTests(TestCase):
    def test_dry_run_writes_nothing(self):
        row = _inferred_demo_row()
        _run()  # no --apply
        self.assertEqual(BrokerServerCatalogue.objects.filter(server_name__iexact=SERVER).count(), 0)
        row.refresh_from_db()
        self.assertEqual(row.environment, BrokerServer.DEMO)
        self.assertEqual(row.environment_source, BrokerServer.INFERRED)


class AttestApplyTests(TestCase):
    def test_apply_creates_catalogue_and_reconciles_row(self):
        row = _inferred_demo_row()
        _run(apply=True)
        cat = BrokerServerCatalogue.objects.get(server_name__iexact=SERVER)
        self.assertEqual(cat.environment, "live")
        self.assertTrue(cat.is_verified)
        self.assertIn("2026-11-04", cat.evidence)
        row.refresh_from_db()
        self.assertEqual(row.environment, "live")
        self.assertEqual(row.environment_source, BrokerServer.OPERATOR)
        env, _src = resolve_verified_environment(SERVER)
        self.assertEqual(env, "live")

    def test_apply_is_idempotent(self):
        _inferred_demo_row()
        _run(apply=True)
        _run(apply=True)  # second apply = consistent, no-op
        self.assertEqual(BrokerServerCatalogue.objects.filter(server_name__iexact=SERVER).count(), 1)
        row = BrokerServer.objects.get(server_name__iexact=SERVER)
        self.assertEqual((row.environment, row.environment_source), ("live", BrokerServer.OPERATOR))

    def test_apply_creates_no_account_or_workspace(self):
        _inferred_demo_row()
        from hosted_workspace.models import HostedMt5Workspace
        before_a, before_w = TradingAccount.objects.count(), HostedMt5Workspace.objects.count()
        _run(apply=True)
        self.assertEqual(TradingAccount.objects.count(), before_a)
        self.assertEqual(HostedMt5Workspace.objects.count(), before_w)


class AttestFailClosedTests(TestCase):
    def test_refuses_when_verified_catalogue_entry_contradicts(self):
        _inferred_demo_row()
        BrokerServerCatalogue.objects.create(server_name=SERVER, environment=BrokerServer.DEMO, is_verified=True)
        with self.assertRaises(CommandError):
            _run(apply=True)   # attesting live against a VERIFIED demo catalogue entry → refuse
        row = BrokerServer.objects.get(server_name__iexact=SERVER)
        self.assertEqual(row.environment, BrokerServer.DEMO)   # unchanged

    def test_refuses_when_operator_row_contradicts(self):
        BrokerServer.objects.create(server_name=SERVER, environment=BrokerServer.DEMO,
                                    environment_source=BrokerServer.OPERATOR)
        with self.assertRaises(CommandError):
            _run(apply=True)   # operator-stamped demo row contradicts attested live → refuse
        self.assertEqual(BrokerServerCatalogue.objects.filter(server_name__iexact=SERVER).count(), 0)


class AttestIsolationTests(TestCase):
    def test_does_not_touch_other_servers(self):
        _inferred_demo_row()
        other = BrokerServer.objects.create(server_name="OtherBroker-Live", environment=BrokerServer.DEMO,
                                            environment_source=BrokerServer.INFERRED)
        _run(apply=True)
        other.refresh_from_db()
        self.assertEqual((other.environment, other.environment_source), (BrokerServer.DEMO, BrokerServer.INFERRED))
        self.assertEqual(BrokerServerCatalogue.objects.filter(server_name__iexact="OtherBroker-Live").count(), 0)


@override_settings(**LIVE_ON)
class AttestAcceptanceTests(TestCase):
    def test_after_apply_live_onboards_and_demo_fails_closed(self):
        _inferred_demo_row()
        _run(apply=True)
        u1 = User.objects.create_user(username="dom_live", email="dom_live@x.invalid", password="x")
        live = P.request_hosted_workspace(u1, expected_login="128736", expected_server=SERVER,
                                          broker_name="Dominion", is_demo=False)
        self.assertTrue(live.ok, live.reason)                 # LIVE now reaches the next stage (intent workspace)
        self.assertFalse(live.workspace.trading_account.is_demo)
        u2 = User.objects.create_user(username="dom_demo", email="dom_demo@x.invalid", password="x")
        demo = P.request_hosted_workspace(u2, expected_login="222", expected_server=SERVER,
                                          broker_name="Dominion", is_demo=True)
        self.assertFalse(demo.ok)                             # a genuine demo/live conflict still fails closed
        self.assertEqual(demo.reason, P.REQ_ENV_MISMATCH)


class AttestReviewHardeningTests(TestCase):
    def test_demo_attestation_flips_inferred_live_row(self):
        # Exercise the demo branch: an inferred LIVE row attested DEMO → catalogue demo + row demo/operator.
        row = BrokerServer.objects.create(server_name=SERVER, environment=BrokerServer.LIVE,
                                          environment_source=BrokerServer.INFERRED)
        _run(environment="demo", apply=True)
        cat = BrokerServerCatalogue.objects.get(server_name__iexact=SERVER)
        self.assertEqual(cat.environment, "demo")
        row.refresh_from_db()
        self.assertEqual((row.environment, row.environment_source), ("demo", BrokerServer.OPERATOR))

    def test_update_preserves_existing_display_name_when_display_omitted(self):
        # An UPDATE triggered for another reason (is_verified False→True) must NOT blank an existing display name.
        _inferred_demo_row(SERVER)
        BrokerServerCatalogue.objects.create(server_name=SERVER, environment=BrokerServer.LIVE, is_verified=False,
                                             broker_display_name="Dominion Markets")
        # The unverified LIVE entry AGREES with the attested env (no refusal); the update only flips is_verified→True.
        _run(environment="live", apply=True)  # no --display supplied
        cat = BrokerServerCatalogue.objects.get(server_name__iexact=SERVER)
        self.assertTrue(cat.is_verified)
        self.assertEqual(cat.broker_display_name, "Dominion Markets")   # preserved, not blanked

    def test_agreeing_catalogue_sourced_row_is_not_restamped(self):
        # A BrokerServer row already stamped CATALOGUE and already on the attested env must be left untouched
        # (never downgraded to operator for a no-op).
        row = BrokerServer.objects.create(server_name=SERVER, environment=BrokerServer.LIVE,
                                          environment_source=BrokerServer.CATALOGUE)
        _run(environment="live", apply=True)
        row.refresh_from_db()
        self.assertEqual(row.environment_source, BrokerServer.CATALOGUE)   # not re-stamped to operator

    def test_refuses_evidence_containing_a_credential_keyword(self):
        _inferred_demo_row(SERVER)
        with self.assertRaises(CommandError):
            _run(environment="live", apply=True, evidence="login 128736 password hunter2")
        self.assertEqual(BrokerServerCatalogue.objects.filter(server_name__iexact=SERVER).count(), 0)
