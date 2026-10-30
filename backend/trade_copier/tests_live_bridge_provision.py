"""ADR-0049 — orchestration + gate tests for the LIVE bridge provisioning integrator. Default dry-run mutates
nothing; each backend gate independently rejects LIVE; the host executor is a fake (no real host) whose
attestation is verified against the backend-rendered env; a post-verify mismatch fails closed to an OFF restore;
the OFF restore works with the LIVE arm flags OFF and no valid authorization."""
import base64
import hashlib
from decimal import Decimal
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from trade_copier import services, live_bridge_provision as LBP
from trade_copier.models import (CopierMaster, CopierSymbolMap, CopierLiveAuthorization,
                                  CopierLiveAuthorizationStatus, CopierLiveKillSwitch, SizingMode)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()
ARM = dict(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
           HOSTED_LIVE_EXECUTION_ENABLED="1")


def _live_acct(user, name, num, *, server, display):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": "live"})
    return TradingAccount.objects.create(user=user, name=name, broker_name=display, account_number=num,
                                         is_demo=False, is_active=True, broker_server=bs)


class _FakeExecutor:
    """Simulates a faithful host: writes the exact env (so env_sha256 == what the backend sent) and reports the
    live posture parsed from the env, unless overridden."""
    def __init__(self, account_id, override=None):
        self.calls = []
        self.account_id = account_id
        self.override = override or {}
    def _attest(self, port, b64):
        env = base64.b64decode(b64).decode("ascii")
        sha = hashlib.sha256(env.encode("ascii")).hexdigest()
        res = {"ok": True, "account_id": self.account_id, "port": port, "env_sha256": sha,
               "allow_live": ("set MT5_ALLOW_LIVE=1" in env), "require_pin": ("set MT5_REQUIRE_IDENTITY_PIN=1" in env),
               "guarded_attach": ("set MT5_GUARDED_ATTACH=1" in env), "listening": True, "task": "GuvFX_TenantBridge_48"}
        res.update(self.override)
        return res
    def activate_tenant_bridge_live_authorized(self, runtime_root, port, bridge_env_b64):
        self.calls.append(("activate", runtime_root, port, bridge_env_b64))
        return self._attest(port, bridge_env_b64)
    def restore_tenant_bridge_execution_off(self, runtime_root, port, bridge_env_b64):
        self.calls.append(("restore", runtime_root, port, bridge_env_b64))
        return self._attest(port, bridge_env_b64)


@override_settings(**ARM)
class LiveBridgeProvisionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("o", "support@guvfx.invalid", "x")
        self.staff = User.objects.create_user("s", "staff@guvfx.invalid", "x", is_staff=True)
        self.master = _live_acct(self.owner, "m", "55425442", server="Tradersway-Live", display="TradersWay")
        self.follower = _live_acct(self.owner, "f", "38313831", server="IS6Technologies-Live", display="IS6")
        self.cm = CopierMaster.objects.create(trading_account=self.master, observe_enabled=True)
        self.f = services.add_follower(self.cm, self.follower, sizing_mode=SizingMode.FIXED_LOT,
                                       fixed_lot=Decimal("0.01"), max_volume=Decimal("0.01"), max_concurrent_positions=1)
        services.set_follower_enabled(self.f, True, actor=self.staff)
        CopierSymbolMap.objects.create(master_broker="Tradersway-Live", master_symbol="XAUUSD",
                                       follower_broker="IS6Technologies-Live", follower_symbol="XAUUSD.std",
                                       verified=True, verified_by=self.staff, verified_at=timezone.now())
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.create(account=self.follower, broker_symbol="XAUUSD.std", base_symbol="XAUUSD.std",
                                        enabled=True, metadata={"volume_min": 0.01, "volume_step": 0.01})
        self.authz = self._grant()
        self.fake_ep = SimpleNamespace(trading_account_id=self.follower.id, runtime_path=r"C:\GuvFX\accounts\48\terminal\terminal64.exe",
                                       port=8801, expected_login="38313831", expected_server="IS6Technologies-Live",
                                       windows_username="guvfx_u_48", workspace_uuid="w-uuid", is_demo=False)
        self._p_ep = mock.patch.object(LBP, "resolve_ready_endpoint", return_value=self.fake_ep)
        self._p_ep.start()
        self.addCleanup(self._p_ep.stop)

    def _grant(self, **over):
        now = timezone.now()
        d = dict(user=self.owner, master_account=self.master, follower_account=self.follower,
                 master_login="55425442", master_server="Tradersway-Live", master_environment="LIVE",
                 follower_login="38313831", follower_server="IS6Technologies-Live", follower_environment="LIVE",
                 source_symbol="XAUUSD", follower_symbol="XAUUSD.std", max_volume=Decimal("0.01"),
                 max_concurrent_positions=1, max_orders=1, orders_consumed=0,
                 valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(hours=1),
                 watermark_at=now - timedelta(hours=1), watermark_event_sequence=5,
                 status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=self.staff, reason="test")
        d.update(over)
        return CopierLiveAuthorization.objects.create(**d)

    def _acct48(self):
        return TradingAccount.objects.get(id=self.follower.id)

    # --- gate evaluation ---
    def test_all_gates_pass(self):
        ok, gates = LBP.evaluate_live_gates(self._acct48(), self.authz.id)
        self.assertTrue(ok, [g for g in gates if not g[1]])

    def _assert_gate_fails(self, gate_name):
        ok, gates = LBP.evaluate_live_gates(self._acct48(), self.authz.id)
        self.assertFalse(ok)
        self.assertIn(gate_name, [n for (n, g, _d) in gates if not g])

    @override_settings(**{**ARM, "COPIER_LIVE_EXECUTE_ENABLED": "0"})
    def test_gate_arm_flag_off(self):
        self._assert_gate_fails("copier_live_execute_enabled")

    @override_settings(**{**ARM, "HOSTED_LIVE_EXECUTION_ENABLED": "0"})
    def test_gate_hosted_off(self):
        self._assert_gate_fails("hosted_live_execution_enabled")

    def test_gate_no_authorization(self):
        self.authz.delete()
        ok, gates = LBP.evaluate_live_gates(self._acct48(), self.authz.id)
        self.assertFalse(ok)

    def test_gate_authz_for_other_follower(self):
        other = _live_acct(self.owner, "f2", "99998888", server="IS6Technologies-Live", display="IS6")
        self.authz.follower_account = other
        self.authz.save(update_fields=["follower_account"])
        self._assert_gate_fails("authorization_follower_is_account")

    def test_gate_expired(self):
        self.authz.expires_at = timezone.now() - timedelta(seconds=1)
        self.authz.save(update_fields=["expires_at"])
        self._assert_gate_fails("within_window")

    def test_gate_consumed(self):
        self.authz.orders_consumed = 1
        self.authz.save(update_fields=["orders_consumed"])
        self._assert_gate_fails("allowance_remaining")

    def test_gate_revoked(self):
        self.authz.revoked_at = timezone.now()
        self.authz.save(update_fields=["revoked_at"])
        self._assert_gate_fails("authorization_not_revoked")

    def test_gate_kill_switch(self):
        CopierLiveKillSwitch.objects.create(engaged=True, reason="drill")
        self._assert_gate_fails("no_kill_switch")

    def test_gate_runtime_pause(self):
        from execution.models import BrokerRuntimePause
        BrokerRuntimePause.objects.create(account_id=self.follower.id, paused=True)
        self._assert_gate_fails("no_runtime_pause")

    def test_gate_follower_inactive(self):
        a = self._acct48(); a.is_active = False; a.save(update_fields=["is_active"])
        self._assert_gate_fails("follower_active")

    def test_gate_instrument_disabled(self):
        from execution.models import BrokerInstrument
        BrokerInstrument.objects.filter(account=self.follower).update(enabled=False)
        self._assert_gate_fails("instrument_enabled_authoritative")

    def test_gate_identity_drift(self):
        a = self._acct48(); a.account_number = "00000000"; a.save(update_fields=["account_number"])
        self._assert_gate_fails("follower_identity_matches")

    def test_gate_unverified_map(self):
        CopierSymbolMap.objects.update(verified=False)
        self._assert_gate_fails("verified_map")

    def test_gate_no_endpoint(self):
        with mock.patch.object(LBP, "resolve_ready_endpoint", return_value=None):
            self._assert_gate_fails("endpoint_ready")

    # --- orchestration ---
    def test_dry_run_no_dispatch(self):
        ex = _FakeExecutor(self.follower.id)
        res = LBP.provision_live(self.follower.id, self.authz.id, commit=False, executor_resolver=lambda a: ex)
        self.assertTrue(res["ok"])
        self.assertFalse(res["armed"])
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["preview"]["env_will_be_live"])
        self.assertEqual(ex.calls, [])                                  # nothing dispatched

    def test_commit_arms_and_attests(self):
        ex = _FakeExecutor(self.follower.id)
        res = LBP.provision_live(self.follower.id, self.authz.id, commit=True, executor_resolver=lambda a: ex)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["armed"])
        self.assertEqual(ex.calls[0][0], "activate")
        # the env dispatched is truthfully LIVE
        env = base64.b64decode(ex.calls[0][3]).decode("ascii")
        self.assertIn("set MT5_ALLOW_LIVE=1", env)

    def test_commit_refused_when_a_gate_fails(self):
        self.authz.orders_consumed = 1
        self.authz.save(update_fields=["orders_consumed"])
        ex = _FakeExecutor(self.follower.id)
        res = LBP.provision_live(self.follower.id, self.authz.id, commit=True, executor_resolver=lambda a: ex)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "gate_failed")
        self.assertEqual(ex.calls, [])

    def test_attestation_mismatch_fails_closed_to_off_restore(self):
        ex = _FakeExecutor(self.follower.id, override={"env_sha256": "deadbeef"})          # host reports a different env checksum
        res = LBP.provision_live(self.follower.id, self.authz.id, commit=True, executor_resolver=lambda a: ex)
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"].startswith("attestation_failed:env_sha_mismatch"))
        self.assertIn("off_restore", res)                               # OFF restoration attempted
        self.assertEqual([c[0] for c in ex.calls], ["activate", "restore"])

    def test_attestation_live_posture_mismatch_fails(self):
        ex = _FakeExecutor(self.follower.id, override={"allow_live": False})
        res = LBP.provision_live(self.follower.id, self.authz.id, commit=True, executor_resolver=lambda a: ex)
        self.assertFalse(res["ok"])
        self.assertIn("live_posture_mismatch", res["reason"])

    def test_real_resolver_called_with_int_account_id(self):
        # Production path (no injected resolver): _resolve_executor must call resolve_signed_host_executor with
        # the INT account id (keyword) - the MEDIUM-1 fix. The mock returns None (DARK), so it reports
        # host_executor_unavailable, but the call shape is the regression guard.
        import hosted_workspace.host_executor as HE
        with mock.patch.object(HE, "resolve_signed_host_executor", return_value=None) as m:
            res = LBP.provision_live(self.follower.id, self.authz.id, commit=True)
        m.assert_called_once_with(account_id=self.follower.id)
        self.assertEqual(res["reason"], "host_executor_unavailable")

    def test_restore_off_works_without_arm_or_authz(self):
        self.authz.delete()
        with override_settings(COPIER_ENABLED="0", COPIER_EXECUTE_ENABLED="0", COPIER_LIVE_EXECUTE_ENABLED="0",
                               HOSTED_LIVE_EXECUTION_ENABLED="0"):
            ex = _FakeExecutor(self.follower.id, override={"allow_live": False})
            res = LBP.restore_off(self.follower.id, commit=True, executor_resolver=lambda a: ex)
            self.assertTrue(res["ok"], res)
            env = base64.b64decode(ex.calls[0][3]).decode("ascii")
            self.assertNotIn("MT5_ALLOW_LIVE", env)                      # OFF env never carries it
