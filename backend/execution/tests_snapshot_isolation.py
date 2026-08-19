"""P0 DATA-ISOLATION — per-tenant MT5 read transport + downstream identity firewall.

Proves the two layers that close the confirmed breach (a fresh customer's Trade History was populated with
support@'s deals because the deals READ went to a module-global bridge and nothing verified the terminal it
reached): (1) a customer-specific read resolves ONLY that account's OWN endpoint bridge and fails closed
otherwise; (2) even a resolved read is refused unless the OBSERVED session login matches the account.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase, override_settings

from execution import snapshot_transport as st
from execution.models import HostedExecutionEndpoint, TerminalNode
from execution.readiness import PERSISTENT_WORKSPACE
from execution.tests_per_tenant_transport import _make_tenant
from hosted_workspace.models import HostedMt5Workspace
from trading.models import BrokerServer, TradingAccount
from django.contrib.auth import get_user_model

GLOBAL = "http://guvfx-agent:8787"
HOSTED_ON = {"HOSTED_PERSISTENT_MT5_ENABLED": "1", "HOSTED_PER_TENANT_TRANSPORT_ENABLED": "1"}


def _endpoint(acct, ws, node, host, port, *, state=HostedExecutionEndpoint.State.READY, base=None):
    return HostedExecutionEndpoint.objects.create(
        workspace=ws, trading_account=acct, terminal_node=node, host=host, port=port,
        base_url=(base if base is not None else f"http://{host}:{port}"),
        windows_username=f"guvfx_u_{acct.account_number}", runtime_path="x",
        workspace_uuid=ws.workspace_uuid, state=state)


@override_settings(**HOSTED_ON)
class SnapshotTransportResolution(TestCase):
    """UPSTREAM routing: each account reads ONLY its own endpoint bridge; fail-closed everywhere else."""

    def setUp(self):
        self.node = TerminalNode.objects.create(hostname="beta", rdp_host="100.79.101.19")

    def test_hosted_account_resolves_its_own_ready_endpoint(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        _endpoint(acct, ws, self.node, "100.79.101.19", 8800)
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason_code, st.ST_PER_TENANT_OK)
        self.assertEqual(r.base_url, "http://100.79.101.19:8800")
        self.assertTrue(r.per_tenant)

    def test_two_tenants_never_cross(self):
        a, aws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        _endpoint(a, aws, self.node, "100.79.101.19", 8800)
        b, bws = _make_tenant("1302587", "guvfx_u_25", node=self.node)
        _endpoint(b, bws, self.node, "100.79.101.19", 8789)
        self.assertEqual(st.resolve_account_snapshot_base(a, global_base_url=GLOBAL).base_url,
                         "http://100.79.101.19:8800")
        self.assertEqual(st.resolve_account_snapshot_base(b, global_base_url=GLOBAL).base_url,
                         "http://100.79.101.19:8789")

    def test_endpoint_not_ready_fails_closed(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        _endpoint(acct, ws, self.node, "100.79.101.19", 8800, state=HostedExecutionEndpoint.State.ALLOCATED)
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, st.ST_ENDPOINT_NOT_READY)
        self.assertEqual(r.base_url, "")

    def test_retired_endpoint_is_treated_as_absent_and_hosted_fails_closed(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        _endpoint(acct, ws, self.node, "100.79.101.19", 8800, state=HostedExecutionEndpoint.State.RETIRED)
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertFalse(r.ok)                                 # hosted account, no live endpoint -> closed
        self.assertEqual(r.reason_code, st.ST_HOSTED_NO_ENDPOINT)

    def test_ready_endpoint_with_blank_base_fails_closed(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)
        _endpoint(acct, ws, self.node, "100.79.101.19", 8800, base="")
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, st.ST_ENDPOINT_UNCONFIGURED)

    def test_hosted_account_without_endpoint_never_uses_global(self):
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)   # no endpoint created
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, st.ST_HOSTED_NO_ENDPOINT)
        self.assertNotEqual(r.base_url, GLOBAL)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="0", HOSTED_PER_TENANT_TRANSPORT_ENABLED="0")
    def test_hosted_no_endpoint_fails_closed_even_with_master_flag_OFF(self):
        # Adversarial-review HIGH: hosted-ness for the read boundary MUST come from the durable
        # readiness_provider field, not the flag-gated is_hosted_workspace_account — so a hosted account with
        # no endpoint fails CLOSED regardless of the DARK master flag (never routes to the global/sibling
        # bridge). Before the fix this returned ST_LEGACY_GLOBAL (= support@'s :8789 on the node2 worker).
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=self.node)   # no endpoint
        r = st.resolve_account_snapshot_base(acct, global_base_url=GLOBAL)
        self.assertFalse(r.ok, f"hosted account fail-opened to {r.base_url!r}")
        self.assertEqual(r.reason_code, st.ST_HOSTED_NO_ENDPOINT)
        self.assertNotEqual(r.base_url, GLOBAL)

    def test_legacy_non_hosted_account_uses_global(self):
        # A plain (Customer-Zero / Provider-A) account with no workspace endpoint keeps the global agent.
        user = get_user_model().objects.create_user(username="cz", email="cz@x.invalid", password="x")
        cz = TradingAccount.objects.create(user=user, name="cz", broker_name="B", account_number="1302561",
                                           is_demo=True, is_active=True)
        r = st.resolve_account_snapshot_base(cz, global_base_url=GLOBAL)
        self.assertTrue(r.ok)
        self.assertEqual(r.reason_code, st.ST_LEGACY_GLOBAL)
        self.assertEqual(r.base_url, GLOBAL)
        self.assertFalse(r.per_tenant)

    def test_none_account_fails_closed(self):
        r = st.resolve_account_snapshot_base(None, global_base_url=GLOBAL)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, st.ST_NO_ACCOUNT)


class SnapshotIdentityFirewall(TestCase):
    """DOWNSTREAM firewall: the observed session login MUST equal the account's account_number."""

    def _acct(self, number, server_name="IS6Technologies-Demo"):
        user = get_user_model().objects.create_user(
            username=f"u{number}", email=f"u{number}@x.invalid", password="x")
        bs = BrokerServer.objects.create(broker_display_name="IS6", server_name=server_name)
        return TradingAccount.objects.create(user=user, name="a", broker_name="B", account_number=number,
                                             is_demo=True, is_active=True, broker_server=bs)

    def test_matching_login_passes(self):
        acct = self._acct("1302575")
        r = st.verify_snapshot_identity(acct, "1302575", "IS6Technologies-Demo")
        self.assertTrue(r.ok)
        self.assertEqual(r.reason_code, st.ID_OK)

    def test_the_breach_scenario_wrong_tenant_login_is_refused(self):
        # acct28 (1302575) but the read reached support@'s terminal (1302587) -> MUST refuse.
        acct = self._acct("1302575")
        r = st.verify_snapshot_identity(acct, "1302587", "IS6Technologies-Demo")
        self.assertFalse(r.ok)
        self.assertEqual(r.reason_code, st.ID_LOGIN_MISMATCH)
        self.assertEqual((r.expected_login, r.observed_login), ("1302575", "1302587"))

    def test_missing_observed_login_is_refused(self):
        acct = self._acct("1302575")
        for missing in (None, "", "   "):
            self.assertEqual(st.verify_snapshot_identity(acct, missing, "S").reason_code,
                             st.ID_OBSERVED_MISSING)

    def test_account_without_number_has_no_expected_and_is_refused(self):
        acct = self._acct("")   # no broker login known
        self.assertEqual(st.verify_snapshot_identity(acct, "1302587", "S").reason_code,
                         st.ID_NO_EXPECTED_LOGIN)

    def test_server_mismatch_refused_only_when_required(self):
        acct = self._acct("1302575", server_name="IS6Technologies-Demo")
        # login matches, server differs
        self.assertTrue(st.verify_snapshot_identity(acct, "1302575", "OtherBroker-Demo").ok)  # lenient default
        self.assertEqual(
            st.verify_snapshot_identity(acct, "1302575", "OtherBroker-Demo", require_server=True).reason_code,
            st.ID_SERVER_MISMATCH)

    def test_int_observed_login_is_normalised(self):
        acct = self._acct("1302575")
        self.assertTrue(st.verify_snapshot_identity(acct, 1302575, "S").ok)  # MT5 returns an int login


@override_settings(**HOSTED_ON)
@mock.patch("hosted_workspace.provisioning.hosted_workspace_admission", return_value=(True, "admit_ok"))
class ConfirmationStampsCutover(TestCase):
    """PHASE 6 — confirmation is the server-derived immutable cutover milestone (pre-customer history excluded)."""

    def test_confirm_broker_account_stamps_ingest_cutover(self, _adm):
        from hosted_workspace.provisioning import confirm_broker_account
        from hosted_workspace.state_machine import WorkspaceLifecycleState as S
        node = TerminalNode.objects.create(hostname="beta", rdp_host="100.79.101.19")
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=node)
        # Bring the workspace to a confirmable state (CONNECTED + observed positive match).
        ws.canonical_state = S.CONNECTED
        ws.proj_account_match = True
        ws.save(update_fields=["canonical_state", "proj_account_match"])
        self.assertIsNone(acct.ingest_cutover_time)
        res = confirm_broker_account(acct.user, ws)
        self.assertTrue(res.ok, res.reason)
        acct.refresh_from_db()
        self.assertIsNotNone(acct.ingest_cutover_time)
        self.assertEqual(acct.ingest_cutover_time, acct.workspace_confirmed_at)  # same instant

    def test_confirm_never_overwrites_an_existing_cutover(self, _adm):
        from hosted_workspace.provisioning import confirm_broker_account
        from hosted_workspace.state_machine import WorkspaceLifecycleState as S
        from django.utils import timezone
        node = TerminalNode.objects.create(hostname="beta", rdp_host="100.79.101.19")
        acct, ws = _make_tenant("1302575", "guvfx_u_28", node=node)
        preset = timezone.now()
        acct.ingest_cutover_time = preset
        acct.save(update_fields=["ingest_cutover_time"])
        ws.canonical_state = S.CONNECTED
        ws.proj_account_match = True
        ws.save(update_fields=["canonical_state", "proj_account_match"])
        confirm_broker_account(acct.user, ws)
        acct.refresh_from_db()
        self.assertEqual(acct.ingest_cutover_time, preset)   # operator value preserved
