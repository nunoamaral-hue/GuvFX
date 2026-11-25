"""Certification tests for the owner-operable governed #46→#48 demonstration control (GFX-170, ADR-0127).

Covers: tenant-owner allow; member / staff-other-tenant / anonymous denial + cross-tenant non-disclosure;
method enforcement; recent-auth (re-submitted password) requirement; confirm-token invalid/expired/wrong-user;
readiness with a master open position / follower job / kill switch / existing arm; route not-sole / not-live /
unconfigured; lease conflict (concurrent operator); prepare refused when already prepared / not ready; confirm
dry-run not ready; execution actuators unprovisioned (DARK); provisioned arm wiring; idempotent re-confirm;
teardown → DARK + lease release (only own lease); one-order and 0.01 caps enforced server-side; and no secret in
any response or audit record. The real atomic-arm internals are covered by ``tests_lifecycle_arming``; here the
``arm_live_lifecycle`` boundary is mocked so the ORCHESTRATION is tested deterministically.
"""
from __future__ import annotations

import logging
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from trade_copier import owner_demo_control as odc
from trade_copier import services
from trade_copier.models import (
    CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization, CopierLiveAuthorizationStatus,
    CopierLiveKillSwitch, CopierMaster, MasterEvent, MasterObservationPoll)
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

_BASE_FLAGS = dict(COPIER_ENABLED="1", COPIER_OWNER_DEMO_CONTROL_ENABLED="1")


def _mk_live_account(user, broker_server, account_number, name):
    return TradingAccount.objects.create(
        user=user, name=name, broker_name="TW", account_number=account_number,
        is_demo=False, is_active=True, broker_server=broker_server)


class _Fixture:
    """Shared governed-route fixture: owner owns #master (observed) → #follower (enabled, same broker → identity
    symbol map). A separate tenant + a staff user in another tenant exist for denial tests."""

    def _build(self):
        self.owner = User.objects.create_user(username="owner", email="owner@demo.invalid", password="pw-owner-1")
        self.other = User.objects.create_user(username="other", email="other@demo.invalid", password="pw-other-1")
        self.staff_other = User.objects.create_user(
            username="staffo", email="staffo@demo.invalid", password="pw-staff-1", is_staff=True)
        self.bs = BrokerServer.objects.create(broker_display_name="TW", server_name="Tradersway-Live",
                                              environment="live")
        self.master_acct = _mk_live_account(self.owner, self.bs, "5544200", "master")
        self.follower_acct = _mk_live_account(self.owner, self.bs, "5176383", "follower")
        self.cm = CopierMaster.objects.create(trading_account=self.master_acct, observe_enabled=True)
        self.link = services.add_follower(self.cm, self.follower_acct)
        if not self.link.enabled:
            services.set_follower_enabled(self.link, True, actor=self.owner)
            self.link.refresh_from_db()
        self.tmpdir = tempfile.mkdtemp(prefix="odc-lease-")
        self.lease_file = f"{self.tmpdir}/recovery_operational_lease.json"

    def _settings(self, **extra):
        s = dict(_BASE_FLAGS)
        s.update({
            "COPIER_DEMO_MASTER_ACCOUNT_ID": self.master_acct.id,
            "COPIER_DEMO_FOLLOWER_ACCOUNT_ID": self.follower_acct.id,
            "COPIER_DEMO_SOURCE_SYMBOL": "XAUUSD",
            "GUVFX_RECOVERY_LEASE_DIR": self.tmpdir,
        })
        s.update(extra)
        return override_settings(**s)


def _fake_actuators(*, provisioned=True, window_opener=True):
    a = odc.DemoActuators(
        emitter_key_probe=lambda: {"ready": True, "identity_ok": True},
        host_admission_probe=lambda: {"off": True},
        emitter_task_probe=lambda: {"disabled": True})
    if provisioned:
        a.admission_provisioner = lambda authz: {"ok": True, "armed": True}
        a.emitter_enabler = lambda: {"ok": True, "enabled": True}
        a.emitter_disabler = lambda: {"ok": True, "disabled": True}
        a.emitter_key_remover = lambda: {"ok": True, "removed": True}
        a.admission_restore = lambda: {"ok": True, "armed": True}
        if window_opener:
            # GFX-171: a no-op host-side opener (the arm boundary is mocked in these orchestration tests, so no real
            # verifier window is needed); the window-lifecycle tests below inject an opener that opens a REAL window.
            a.verifier_window_opener = lambda authz: {"ok": True, "opened": True, "window_id": 1}
    return a


# ─────────────────────────────── orchestration-layer tests ───────────────────────────────

class OwnerDemoRouteAuthzTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_route_resolves_for_owner(self):
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertTrue(rc.ok, rc.reason)
            self.assertEqual(rc.master.id, self.master_acct.id)
            self.assertEqual(rc.follower.id, self.follower_acct.id)
            self.assertEqual(rc.source_symbol, "XAUUSD")
            self.assertTrue(rc.follower_symbol)   # resolved from the canonical map, never hard-coded

    def test_route_denied_other_tenant_no_disclosure(self):
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.other)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_NOT_ROUTE_OWNER)
            self.assertIsNone(rc.master)   # non-disclosure: no account object leaked to a non-owner

    def test_route_denied_staff_other_tenant(self):
        # Staff is NOT privileged here — ownership is the sole authority.
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.staff_other)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_NOT_ROUTE_OWNER)

    def test_route_denied_anonymous(self):
        from django.contrib.auth.models import AnonymousUser
        with self._settings():
            rc = odc.resolve_governed_demo_route(AnonymousUser())
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_NOT_AUTHENTICATED)

    def test_route_denied_not_sole(self):
        third = _mk_live_account(self.owner, self.bs, "9999001", "third")
        link2 = services.add_follower(self.cm, third)
        services.set_follower_enabled(link2, True, actor=self.owner)
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_ROUTE_NOT_SOLE)

    def test_route_denied_not_live(self):
        demo_bs = BrokerServer.objects.create(broker_display_name="TW", server_name="Tradersway-Demo",
                                              environment="demo")
        self.follower_acct.is_demo = True
        self.follower_acct.broker_server = demo_bs
        self.follower_acct.save(update_fields=["is_demo", "broker_server"])
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_ACCOUNT_NOT_LIVE)

    def test_route_denied_unconfigured(self):
        self.link.enabled = False
        self.link.save(update_fields=["enabled"])
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_ROUTE_NOT_CONFIGURED)


class OwnerDemoReadinessTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_feature_dark_when_flag_off(self):
        with override_settings(COPIER_ENABLED="1", COPIER_OWNER_DEMO_CONTROL_ENABLED="0",
                               COPIER_DEMO_MASTER_ACCOUNT_ID=self.master_acct.id,
                               COPIER_DEMO_FOLLOWER_ACCOUNT_ID=self.follower_acct.id,
                               GUVFX_RECOVERY_LEASE_DIR=self.tmpdir):
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["enabled"])
            self.assertEqual(res["reason"], odc.R_FEATURE_DARK)

    def test_ready_when_dark(self):
        with self._settings():
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertTrue(res["authorized"])
            self.assertTrue(res["ready_to_prepare"], res.get("blockers"))
            self.assertEqual(res["blockers"], [])
            self.assertEqual(res["route"]["max_volume"], "0.01")
            self.assertEqual(res["route"]["max_orders"], 1)
            self.assertFalse(res["route"]["partial_close"])

    def test_blocked_master_open_position(self):
        poll = MasterObservationPoll.objects.create(master=self.cm, observed_at=timezone.now(),
                                                    read_at=timezone.now(), position_count=1)
        MasterEvent.objects.create(
            master=self.cm, sequence=1, from_poll=poll, event_type=MasterEvent.EventType.OPEN,
            position_ticket=777, symbol="XAUUSD", side="BUY", volume_before=Decimal("0"),
            volume_after=Decimal("0.10"), volume_delta=Decimal("0.10"), observed_at=timezone.now())
        with self._settings():
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["ready_to_prepare"])
            self.assertIn("MASTER_NOT_CLEAN", res["blockers"])

    def test_blocked_follower_job_in_flight(self):
        from execution.models import ExecutionJob
        ExecutionJob.objects.create(account=self.follower_acct, job_type="PLACE_ORDER",
                                    status=ExecutionJob.Status.PENDING, payload={})
        with self._settings():
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["ready_to_prepare"])
            self.assertIn("FOLLOWER_JOB_IN_FLIGHT", res["blockers"])

    def test_blocked_kill_switch(self):
        CopierLiveKillSwitch.objects.create(engaged=True)
        with self._settings():
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["ready_to_prepare"])
            self.assertIn("KILL_SWITCH_ENGAGED", res["blockers"])

    def test_blocked_existing_arm(self):
        authz = _make_authz(self)
        CopierLiveArm.objects.create(
            authorization=authz, owner=self.owner, master_account=self.master_acct,
            follower_account=self.follower_acct, source_symbol="XAUUSD", follower_symbol="XAUUSD",
            state=CopierLiveArmState.ENGAGED, expires_at=timezone.now() + timezone.timedelta(minutes=10))
        with self._settings():
            res = odc.readiness(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["ready_to_prepare"])
            self.assertIn(odc.R_ARM_EXISTS, res["blockers"])


def _make_authz(fx, **over):
    now = timezone.now()
    kw = dict(
        user=fx.owner, master_account=fx.master_acct, follower_account=fx.follower_acct,
        master_login="5544200", master_server="Tradersway-Live", master_environment="LIVE",
        follower_login="5176383", follower_server="Tradersway-Live", follower_environment="LIVE",
        source_symbol="XAUUSD", follower_symbol="XAUUSD",
        max_volume=Decimal("0.01"), max_concurrent_positions=1, max_orders=1, orders_consumed=0,
        valid_from=now, expires_at=now + timezone.timedelta(minutes=15),
        watermark_at=now, watermark_event_sequence=0,
        status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=fx.owner, reason="t",
        modify_authority=True, full_close_authority=True, partial_close_authority=False,
        correlation_id="odc-test")
    kw.update(over)
    return CopierLiveAuthorization.objects.create(**kw)


class OwnerDemoPrepareTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_prepare_creates_bounded_authorization(self):
        with self._settings():
            res = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(res["prepared"], res)
            self.assertIn("confirm_token", res)
            authz = CopierLiveAuthorization.objects.get(id=res["authorization_id"])
            # One-order, 0.01 caps enforced SERVER-SIDE regardless of any client input.
            self.assertEqual(authz.max_orders, odc.DEMO_MAX_ORDERS)
            self.assertEqual(authz.max_volume, odc.DEMO_MAX_VOLUME)
            self.assertEqual(authz.max_concurrent_positions, 1)
            self.assertTrue(authz.modify_authority)
            self.assertTrue(authz.full_close_authority)
            self.assertFalse(authz.partial_close_authority)       # partial-close EXCLUDED
            self.assertEqual(authz.user_id, self.owner.id)
            self.assertEqual(authz.approved_by_id, self.owner.id)  # owner self-approval
            self.assertEqual(authz.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_prepare_lease_conflict_second_operator(self):
        with self._settings():
            first = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(first["prepared"])
            # A second prepare (duplicate click / concurrent operator) is fail-closed: already prepared.
            second = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertFalse(second["prepared"])
            self.assertIn(second["reason"], (odc.R_READINESS_NOT_READY, odc.R_LEASE_CONFLICT, odc.R_AUTHZ_EXISTS))
            self.assertEqual(CopierLiveAuthorization.objects.filter(
                master_account=self.master_acct, follower_account=self.follower_acct,
                status=CopierLiveAuthorizationStatus.ACTIVE).count(), 1)

    def test_prepare_refused_when_not_ready(self):
        CopierLiveKillSwitch.objects.create(engaged=True)
        with self._settings():
            res = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["prepared"])
            self.assertEqual(res["reason"], odc.R_READINESS_NOT_READY)
            self.assertIn("KILL_SWITCH_ENGAGED", res["blockers"])
            # No lease left held on a refusal.
            from hosted_workspace import recovery_lease as RL
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "ABSENT")

    def test_prepare_denied_non_owner(self):
        with self._settings():
            res = odc.prepare(self.other, lease_path=self.lease_file)
            self.assertFalse(res.get("prepared"))
            self.assertFalse(res["authorized"])
            self.assertEqual(res["reason"], odc.R_NOT_ROUTE_OWNER)


class OwnerDemoConfirmTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def _prepare(self):
        res = odc.prepare(self.owner, lease_path=self.lease_file)
        self.assertTrue(res["prepared"], res)
        return res

    def test_confirm_requires_recent_auth(self):
        with self._settings():
            p = self._prepare()
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=False,
                              lease_path=self.lease_file)
            self.assertFalse(res["armed"])
            self.assertEqual(res["reason"], odc.R_RECENT_AUTH_REQUIRED)

    def test_confirm_token_invalid(self):
        with self._settings():
            self._prepare()
            res = odc.confirm(self.owner, confirm_token="tampered.bad.token", recent_auth_ok=True,
                              lease_path=self.lease_file)
            self.assertFalse(res["armed"])
            self.assertEqual(res["reason"], odc.R_CONFIRM_TOKEN_INVALID)

    def test_confirm_token_expired(self):
        with self._settings():
            p = self._prepare()
            with mock.patch.object(odc._SIGNER, "unsign", side_effect=signing.SignatureExpired("x")):
                res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                  lease_path=self.lease_file)
            self.assertEqual(res["reason"], odc.R_CONFIRM_TOKEN_EXPIRED)

    def test_confirm_token_wrong_user(self):
        with self._settings():
            p = self._prepare()
            # The other tenant is not the route owner → denied before the token is even consulted.
            res = odc.confirm(self.other, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              lease_path=self.lease_file)
            self.assertFalse(res["authorized"])
            self.assertEqual(res["reason"], odc.R_NOT_ROUTE_OWNER)

    def test_confirm_no_prepared_demo(self):
        with self._settings():
            # A validly-signed token for a non-existent authorization id.
            tok = odc._SIGNER.sign(f"999999:{self.owner.id}:odc-x")
            res = odc.confirm(self.owner, confirm_token=tok, recent_auth_ok=True, lease_path=self.lease_file)
            self.assertEqual(res["reason"], odc.R_NO_PREPARED_DEMO)

    def test_confirm_dry_run_not_ready(self):
        with self._settings():
            p = self._prepare()
            with mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle",
                            return_value={"ok": False, "blockers": ["VERIFIER_WINDOW_NOT_READY"]}) as m:
                res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                  actuators=_fake_actuators(), lease_path=self.lease_file)
            self.assertFalse(res["armed"])
            self.assertEqual(res["reason"], odc.R_READINESS_NOT_READY)
            self.assertIn("VERIFIER_WINDOW_NOT_READY", res["blockers"])
            # Only the dry-run (commit=False) was attempted; no commit arm.
            self.assertEqual(m.call_count, 1)
            self.assertFalse(m.call_args.kwargs["commit"])

    def test_confirm_execution_actuators_unprovisioned_dark(self):
        with self._settings():
            p = self._prepare()
            # Dry-run passes, but the risk-increasing actuators are absent ⇒ fail closed, NO commit arm.
            with mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle",
                            return_value={"ok": True, "armed": False, "dry_run": True}) as m:
                res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                  actuators=_fake_actuators(provisioned=False), lease_path=self.lease_file)
            self.assertFalse(res["armed"])
            self.assertEqual(res["reason"], odc.R_EXECUTION_ACTUATORS_UNPROVISIONED)
            self.assertEqual(m.call_count, 1)   # dry-run only; never commit
            self.assertFalse(CopierLiveArm.objects.filter(
                master_account=self.master_acct, follower_account=self.follower_acct,
                state=CopierLiveArmState.ENGAGED).exists())

    def test_confirm_production_no_actuators_fails_closed(self):
        # Production path: real arm, no probes/actuators ⇒ dry-run fails closed (DARK), authz intact.
        with self._settings():
            p = self._prepare()
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=None, lease_path=self.lease_file)
            self.assertFalse(res["armed"])
            self.assertEqual(res["reason"], odc.R_READINESS_NOT_READY)
            self.assertEqual(CopierLiveAuthorization.objects.get(id=p["authorization_id"]).status,
                             CopierLiveAuthorizationStatus.ACTIVE)   # authz NOT revoked by a failed confirm

    def test_confirm_arms_with_provisioned_actuators(self):
        with self._settings():
            p = self._prepare()

            def _side(**kw):
                if kw["commit"]:
                    return {"ok": True, "armed": True, "arm_id": 4242, "reason": "lifecycle_armed"}
                return {"ok": True, "armed": False, "dry_run": True}

            with mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle", side_effect=_side) as m:
                res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                  actuators=_fake_actuators(provisioned=True), lease_path=self.lease_file)
            self.assertTrue(res["armed"])
            self.assertEqual(res["arm_id"], 4242)
            # Dry-run then commit — exactly two calls, the second committing.
            self.assertEqual(m.call_count, 2)
            self.assertTrue(m.call_args.kwargs["commit"])

    def test_confirm_idempotent_when_already_armed(self):
        with self._settings():
            p = self._prepare()
            authz = CopierLiveAuthorization.objects.get(id=p["authorization_id"])
            arm = CopierLiveArm.objects.create(
                authorization=authz, owner=self.owner, master_account=self.master_acct,
                follower_account=self.follower_acct, source_symbol="XAUUSD", follower_symbol="XAUUSD",
                state=CopierLiveArmState.ENGAGED, expires_at=timezone.now() + timezone.timedelta(minutes=10))
            with mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle") as m:
                res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                  actuators=_fake_actuators(), lease_path=self.lease_file)
            self.assertTrue(res["armed"])
            self.assertTrue(res["idempotent"])
            self.assertEqual(res["arm_id"], arm.id)
            m.assert_not_called()   # never double-arm


class OwnerDemoStopTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_stop_returns_dark_and_releases_lease(self):
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(p["prepared"])
            res = odc.stop(self.owner, lease_path=self.lease_file)
            self.assertTrue(res["backend_dark"], res)
            self.assertTrue(res["lease_released"])
            authz = CopierLiveAuthorization.objects.get(id=p["authorization_id"])
            self.assertEqual(authz.status, CopierLiveAuthorizationStatus.REVOKED)
            from hosted_workspace import recovery_lease as RL
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "ABSENT")

    def test_stop_idempotent(self):
        with self._settings():
            odc.prepare(self.owner, lease_path=self.lease_file)
            odc.stop(self.owner, lease_path=self.lease_file)
            again = odc.stop(self.owner, lease_path=self.lease_file)   # safe to repeat
            self.assertTrue(again["backend_dark"])

    def test_stop_does_not_release_other_holders_lease(self):
        from hosted_workspace import recovery_lease as RL
        now = timezone.now()
        ok, _ = RL.acquire(RL.KIND_DEPLOY, "deploy", "ci", "someone-else-corr", 600, now, self.lease_file)
        self.assertTrue(ok)
        with self._settings():
            res = odc.stop(self.owner, lease_path=self.lease_file)
            # Backend teardown still runs, but a lease held by ANOTHER holder is never released here.
            self.assertIsNone(res["lease_released"])
            self.assertEqual(RL.status_metadata(now, self.lease_file)["correlation_id"], "someone-else-corr")


class OwnerDemoNoSecretTests(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_no_secret_in_responses_or_audit(self):
        with self._settings():
            log = logging.getLogger("trade_copier.owner_demo_control")
            with self.assertLogs(log, level="INFO") as cap:
                rd = odc.readiness(self.owner, lease_path=self.lease_file)
                p = odc.prepare(self.owner, lease_path=self.lease_file)
                odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=False,
                            lease_path=self.lease_file)
                st = odc.stop(self.owner, lease_path=self.lease_file)
            blob = f"{rd} {p} {st} {' '.join(cap.output)}".lower()
            for needle in ("password", "pw-owner", "hmac", "secret", "keyring", "private", "token=", "ingest.key"):
                self.assertNotIn(needle, blob)
            # The confirm token is a SIGNED opaque value (expected in the prepare response), never a raw key.
            self.assertIn("confirm_token", p)


# ─────────────────────────────── HTTP surface tests ───────────────────────────────

class OwnerDemoApiTests(APITestCase, _Fixture):
    def setUp(self):
        self._build()

    def test_method_enforcement_get_on_prepare_405(self):
        with self._settings():
            self.client.force_authenticate(self.owner)
            self.assertEqual(self.client.get("/api/copy-trading/owner-demo/prepare/").status_code, 405)

    def test_feature_dark_503(self):
        with override_settings(COPIER_ENABLED="1", COPIER_OWNER_DEMO_CONTROL_ENABLED="0",
                               COPIER_DEMO_MASTER_ACCOUNT_ID=self.master_acct.id,
                               COPIER_DEMO_FOLLOWER_ACCOUNT_ID=self.follower_acct.id,
                               GUVFX_RECOVERY_LEASE_DIR=self.tmpdir):
            self.client.force_authenticate(self.owner)
            r = self.client.get("/api/copy-trading/owner-demo/readiness/")
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.data["reason"], odc.R_FEATURE_DARK)

    def test_non_owner_403(self):
        with self._settings():
            self.client.force_authenticate(self.other)
            r = self.client.get("/api/copy-trading/owner-demo/readiness/")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.data["reason"], odc.R_NOT_ROUTE_OWNER)

    def test_readiness_ok_200(self):
        with self._settings():
            self.client.force_authenticate(self.owner)
            r = self.client.get("/api/copy-trading/owner-demo/readiness/")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data["ready_to_prepare"])

    def test_confirm_without_password_403(self):
        with self._settings():
            self.client.force_authenticate(self.owner)
            p = self.client.post("/api/copy-trading/owner-demo/prepare/", {}, format="json")
            self.assertEqual(p.status_code, 200)
            r = self.client.post("/api/copy-trading/owner-demo/confirm/",
                                 {"confirm_token": p.data["confirm_token"]}, format="json")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.data["reason"], odc.R_RECENT_AUTH_REQUIRED)

    def test_confirm_wrong_password_403(self):
        with self._settings():
            self.client.force_authenticate(self.owner)
            p = self.client.post("/api/copy-trading/owner-demo/prepare/", {}, format="json")
            r = self.client.post("/api/copy-trading/owner-demo/confirm/",
                                 {"confirm_token": p.data["confirm_token"], "password": "wrong-pw"},
                                 format="json")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.data["reason"], odc.R_RECENT_AUTH_REQUIRED)

    def test_prepare_response_has_no_secret(self):
        with self._settings():
            self.client.force_authenticate(self.owner)
            p = self.client.post("/api/copy-trading/owner-demo/prepare/", {}, format="json")
            self.assertEqual(p.status_code, 200)
            blob = str(p.data).lower()
            for needle in ("password", "hmac", "keyring", "ingest.key", "private"):
                self.assertNotIn(needle, blob)


class OwnerDemoReviewHardeningTests(TestCase, _Fixture):
    """Regression tests for the two-round adversarial review (GFX-170): no existence oracle; xor-ownership denial;
    token uid-binding; single-effect replay; and stop never drops the concurrency guard / reclaims a MALFORMED lease."""

    def setUp(self):
        self._build()

    def test_route_denied_owns_only_one_account(self):
        # Owns #master but NOT #follower (another tenant) => NOT_ROUTE_OWNER (the `and` requires BOTH).
        self.follower_acct.user = self.other
        self.follower_acct.save(update_fields=["user"])
        with self._settings():
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_NOT_ROUTE_OWNER)

    def test_missing_account_is_not_route_owner_no_oracle(self):
        # A non-existent governed account yields NOT_ROUTE_OWNER (not ROUTE_NOT_CONFIGURED) => no existence oracle.
        with self._settings(COPIER_DEMO_FOLLOWER_ACCOUNT_ID=99999999):
            rc = odc.resolve_governed_demo_route(self.owner)
            self.assertFalse(rc.ok)
            self.assertEqual(rc.reason, odc.R_NOT_ROUTE_OWNER)

    def test_verify_confirm_token_uid_mismatch(self):
        # A genuinely-signed token minted for a DIFFERENT uid is rejected by the verifier's uid binding.
        tok = odc._SIGNER.sign(f"123:{self.other.id}:odc-x")
        authz_id, corr, err = odc._verify_confirm_token(tok, self.owner)
        self.assertIsNone(authz_id)
        self.assertEqual(err, odc.R_CONFIRM_TOKEN_INVALID)

    def test_confirm_replay_is_single_effect_dark(self):
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(p["prepared"])
            r1 = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                             actuators=None, lease_path=self.lease_file)
            r2 = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                             actuators=None, lease_path=self.lease_file)
            # Replay yields the SAME fail-closed result and NO additional effect: no arm, authz still ACTIVE.
            self.assertFalse(r1["armed"])
            self.assertFalse(r2["armed"])
            self.assertEqual(r1["reason"], r2["reason"])
            self.assertFalse(CopierLiveArm.objects.filter(
                master_account=self.master_acct, follower_account=self.follower_acct,
                state=CopierLiveArmState.ENGAGED).exists())
            self.assertEqual(CopierLiveAuthorization.objects.get(id=p["authorization_id"]).status,
                             CopierLiveAuthorizationStatus.ACTIVE)

    def test_stop_keeps_lease_when_teardown_not_dark(self):
        # F1 regression: if teardown cannot prove DARK (e.g. unflat follower position), the acceptance lease is
        # KEPT so the deploy/recovery mutual-exclusion guard is never dropped over a still-live route.
        with self._settings():
            odc.prepare(self.owner, lease_path=self.lease_file)
            with mock.patch("trade_copier.lifecycle_teardown.teardown_live_dark",
                            return_value={"ok": False, "backend_dark": False, "host_dark": False,
                                          "cleanup_required": True}):
                res = odc.stop(self.owner, lease_path=self.lease_file)
            self.assertFalse(res["backend_dark"])
            self.assertIs(res["lease_released"], False)
            from hosted_workspace import recovery_lease as RL
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "ACTIVE")

    def test_stop_does_not_reclaim_malformed_lease(self):
        # F3 regression: an owner stop never reclaims a MALFORMED lease (left fail-closed for operator review).
        with open(self.lease_file, "w", encoding="utf-8") as fh:
            fh.write("{ not valid json")
        with self._settings():
            odc.stop(self.owner, lease_path=self.lease_file)
            from hosted_workspace import recovery_lease as RL
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "MALFORMED")
