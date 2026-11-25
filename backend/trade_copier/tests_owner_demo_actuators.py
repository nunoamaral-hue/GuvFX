"""Synthetic tests for the GFX-171 owner-demo EXECUTION wiring (ADR-0128): the fail-closed actuator PROVIDER and
the confirm/stop orchestration that reuses it.

The real atomic-arm + verifier-window internals are covered by ``tests_lifecycle_arming`` / ``tests_verifier_window``;
here the ``arm_live_lifecycle`` and ``verifier_window.close_all`` boundaries are mocked so the ORCHESTRATION — the
host-side window opener, the provisioned/unprovisioned fail-closed gates, guaranteed window teardown on any
non-armed outcome, and the exclusion-lease retention on failed/unverified teardown — is tested deterministically
with synthetic inputs. No host, no network, no real key, no real window.
"""
from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from trade_copier import owner_demo_actuators as oda
from trade_copier import owner_demo_control as odc
from trade_copier.models import (
    CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization, CopierLiveAuthorizationStatus)
from trade_copier.tests_owner_demo_control import _Fixture, _fake_actuators


class _Base(TestCase, _Fixture):
    def setUp(self):
        self._build()

    def _rc(self):
        with self._settings():
            return odc.resolve_governed_demo_route(self.owner)


# ─────────────────────────────── the provider: build_demo_actuators ───────────────────────────────

class BuildDemoActuatorsTests(_Base):
    def test_flag_off_no_risk_increasing_actuators(self):
        rc = self._rc()
        with mock.patch.object(oda, "_signed_host_executor_available", return_value=True), \
             mock.patch("trade_copier.flags.copier_owner_demo_execution_enabled", return_value=False):
            a = oda.build_demo_actuators(rc, commit=True)
        self.assertIsNone(a.admission_provisioner)
        self.assertIsNone(a.emitter_enabler)
        self.assertIsNone(a.verifier_window_opener)   # never provisioned here
        # Teardown-side actuators ARE wired (flag-independent) when the executor is available.
        self.assertIsNotNone(a.emitter_disabler)
        self.assertIsNotNone(a.emitter_key_remover)
        self.assertIsNotNone(a.admission_restore)
        self.assertFalse(a.provisioned())

    def test_flag_on_executor_live_still_unprovisioned_without_opener(self):
        # The core GFX-171 property: even with the execution flag ON and a live executor, the production provider
        # leaves the verifier-window opener (and the #48 admission read-back) unprovisioned ⇒ confirm fails closed.
        rc = self._rc()
        with mock.patch.object(oda, "_signed_host_executor_available", return_value=True), \
             mock.patch("trade_copier.flags.copier_owner_demo_execution_enabled", return_value=True):
            a = oda.build_demo_actuators(rc, commit=True)
        self.assertIsNotNone(a.admission_provisioner)   # risk-increasing wired
        self.assertIsNotNone(a.emitter_enabler)
        self.assertIsNotNone(a.emitter_disabler)        # teardown wired
        self.assertIsNotNone(a.host_admission_probe)    # GFX-172: #48 admission probe now wired (fail-closed)
        self.assertEqual(a.host_admission_probe().get("off"), False)   # production reader UNAVAILABLE ⇒ fail closed
        self.assertIsNone(a.verifier_window_opener)     # DELIBERATELY unprovisioned (no key-transfer channel)
        self.assertFalse(a.provisioned())               # ⇒ confirm DARK

    def test_executor_absent_all_host_actuators_none(self):
        rc = self._rc()
        with mock.patch.object(oda, "_signed_host_executor_available", return_value=False), \
             mock.patch("trade_copier.flags.copier_owner_demo_execution_enabled", return_value=True):
            a = oda.build_demo_actuators(rc, commit=True)
        for f in ("admission_provisioner", "emitter_enabler", "emitter_disabler", "emitter_key_remover",
                  "admission_restore", "verifier_window_opener"):
            self.assertIsNone(getattr(a, f), f)
        # Probes are still wired (read-only, fail-closed internally); they never make provisioned() true alone.
        self.assertIsNotNone(a.emitter_key_probe)
        self.assertFalse(a.provisioned())

    def test_provider_is_default_when_api_injects_nothing(self):
        # confirm(actuators=None) routes through build_demo_actuators ⇒ fails closed (no executor in tests).
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=None, lease_path=self.lease_file)
        self.assertFalse(res["armed"])
        # No window opener ⇒ the real dry-run fails closed (no verifier window) ⇒ READINESS_NOT_READY, DARK.
        self.assertEqual(res["reason"], odc.R_READINESS_NOT_READY)


# ─────────────────────────────── confirm: host-side verifier-window lifecycle ───────────────────────────────

class ConfirmWindowLifecycleTests(_Base):
    def _prep(self):
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(p["prepared"], p)
            return p

    def test_opener_called_then_armed_window_kept_open(self):
        p = self._prep()
        calls = {"opener": 0}

        def _opener(authz):
            calls["opener"] += 1
            return {"ok": True, "opened": True, "window_id": 7}

        acts = _fake_actuators(provisioned=True)
        acts.verifier_window_opener = _opener

        def _side(**kw):
            return {"ok": True, "armed": True, "arm_id": 99, "reason": "lifecycle_armed"} if kw["commit"] \
                else {"ok": True, "armed": False, "dry_run": True}

        with self._settings(), \
             mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle", side_effect=_side), \
             mock.patch("trade_copier.verifier_window.close_all") as close_all:
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=acts, lease_path=self.lease_file)
        self.assertTrue(res["armed"])
        self.assertEqual(calls["opener"], 1)                 # opened host-side exactly once
        close_all.assert_not_called()                        # armed success ⇒ window kept open for the live arm

    def test_window_closed_on_non_armed_outcome(self):
        p = self._prep()
        acts = _fake_actuators(provisioned=True)
        acts.verifier_window_opener = lambda authz: {"ok": True, "opened": True, "window_id": 8}
        with self._settings(), \
             mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle",
                        return_value={"ok": False, "blockers": ["MASTER_NOT_CLEAN"]}), \
             mock.patch("trade_copier.verifier_window.close_all") as close_all:
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=acts, lease_path=self.lease_file)
        self.assertFalse(res["armed"])
        self.assertEqual(res["reason"], odc.R_READINESS_NOT_READY)
        close_all.assert_called_once()                        # guaranteed window teardown ⇒ DARK

    def test_opener_failure_is_fail_closed(self):
        p = self._prep()
        acts = _fake_actuators(provisioned=True)
        acts.verifier_window_opener = lambda authz: {"ok": False, "opened": False, "reason": "host_unavailable"}
        with self._settings(), \
             mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle") as arm, \
             mock.patch("trade_copier.verifier_window.close_all"):
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=acts, lease_path=self.lease_file)
        self.assertFalse(res["armed"])
        self.assertEqual(res["reason"], odc.R_VERIFIER_WINDOW_OPEN_FAILED)
        arm.assert_not_called()                               # never dry-run/commit without an open window

    def test_partial_failure_rolls_back_and_closes_window_value_blind(self):
        p = self._prep()
        acts = _fake_actuators(provisioned=True)
        acts.verifier_window_opener = lambda authz: {"ok": True, "opened": True, "window_id": 9}

        def _side(**kw):
            # Dry-run passes; the commit arm partially fails and rolls back to DARK (its own teardown closes the
            # window; the orchestration's finally additionally guarantees it).
            return {"ok": False, "armed": False, "rolled_back": True, "reason": "emitter_enable_failed"} \
                if kw["commit"] else {"ok": True, "armed": False, "dry_run": True}

        with self._settings(), \
             mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle", side_effect=_side), \
             mock.patch("trade_copier.verifier_window.close_all") as close_all:
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=acts, lease_path=self.lease_file)
        self.assertFalse(res["armed"])
        self.assertTrue(res["rolled_back"])
        close_all.assert_called_once()
        # Value-blind: no key/secret in the response.
        blob = str(res).lower()
        for needle in ("hmac", "ingest.key", "keyring", "private", "password", "secret"):
            self.assertNotIn(needle, blob)


# ─────────────────────────────── expiry / interruption / retry / teardown exclusion ───────────────────────────────

class LifecycleRecoveryTests(_Base):
    def test_expired_authorization_confirm_is_no_prepared_demo(self):
        # Expiry at the authorization boundary: a confirm after the authorization has lapsed/been revoked is refused
        # (the ACTIVE-authz lookup no longer matches) ⇒ fail closed, no arm.
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            authz = CopierLiveAuthorization.objects.get(id=p["authorization_id"])
            authz.status = CopierLiveAuthorizationStatus.REVOKED
            authz.save(update_fields=["status"])
            res = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                              actuators=_fake_actuators(), lease_path=self.lease_file)
        self.assertFalse(res["armed"])
        self.assertEqual(res["reason"], odc.R_NO_PREPARED_DEMO)

    def test_interrupted_session_lease_self_expires_and_stop_reclaims(self):
        # Interruption: the owner prepares then the session drops (no confirm/stop). The acceptance lease self-expires
        # via its TTL, and a later stop reclaims the EXPIRED lease it owns (returns to a clean ABSENT state).
        from hosted_workspace import recovery_lease as RL
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            self.assertTrue(p["prepared"])
            now = timezone.now()
            later = now + timezone.timedelta(seconds=odc.LEASE_TTL_S + 5)
            self.assertEqual(RL.status_metadata(later, self.lease_file)["status"], "EXPIRED")
            res = odc.stop(self.owner, now=later, lease_path=self.lease_file)
            self.assertTrue(res["lease_released"])
            self.assertEqual(RL.status_metadata(later, self.lease_file)["status"], "ABSENT")

    def test_retry_confirm_is_idempotent_single_arm(self):
        # Retry (duplicate confirm) never double-arms: the one-engaged-arm invariant returns ALREADY_ARMED.
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)
            authz = CopierLiveAuthorization.objects.get(id=p["authorization_id"])
            arm = CopierLiveArm.objects.create(
                authorization=authz, owner=self.owner, master_account=self.master_acct,
                follower_account=self.follower_acct, source_symbol="XAUUSD", follower_symbol="XAUUSD",
                state=CopierLiveArmState.ENGAGED, expires_at=timezone.now() + timezone.timedelta(minutes=10))
            with mock.patch("trade_copier.lifecycle_arming.arm_live_lifecycle") as m:
                r1 = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                 actuators=_fake_actuators(), lease_path=self.lease_file)
                r2 = odc.confirm(self.owner, confirm_token=p["confirm_token"], recent_auth_ok=True,
                                 actuators=_fake_actuators(), lease_path=self.lease_file)
            self.assertTrue(r1["idempotent"])
            self.assertTrue(r2["idempotent"])
            self.assertEqual(r1["arm_id"], arm.id)
            m.assert_not_called()

    def test_unverified_host_teardown_after_arm_retains_exclusion(self):
        # The key GFX-171 teardown property: when an arm HAD engaged the host, a stop whose signed host teardown is
        # UNVERIFIED (host_dark False) must NOT release the acceptance lease — the deploy/recovery exclusion guard is
        # held until a re-run proves the host DARK. (backend_dark True alone is NOT sufficient after an arm.)
        from hosted_workspace import recovery_lease as RL
        with self._settings():
            p = odc.prepare(self.owner, lease_path=self.lease_file)   # acquires the owner's acceptance lease + authz
            authz = CopierLiveAuthorization.objects.get(id=p["authorization_id"])
            CopierLiveArm.objects.create(
                authorization=authz, owner=self.owner, master_account=self.master_acct,
                follower_account=self.follower_acct, source_symbol="XAUUSD", follower_symbol="XAUUSD",
                state=CopierLiveArmState.ENGAGED, expires_at=timezone.now() + timezone.timedelta(minutes=10))
            with mock.patch("trade_copier.lifecycle_teardown.teardown_live_dark",
                            return_value={"ok": True, "backend_dark": True, "host_dark": False,
                                          "cleanup_required": False}):
                res = odc.stop(self.owner, actuators=_fake_actuators(), lease_path=self.lease_file)
            self.assertTrue(res["backend_dark"])
            self.assertFalse(res["host_dark"])
            self.assertIs(res["lease_released"], False)         # exclusion RETAINED on unverified host teardown
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "ACTIVE")

    def test_pure_dark_stop_releases_without_host_proof(self):
        # Converse: a pure-DARK stop (no arm ever engaged) needs NO host proof — backend_dark alone releases.
        from hosted_workspace import recovery_lease as RL
        with self._settings():
            odc.prepare(self.owner, lease_path=self.lease_file)
            res = odc.stop(self.owner, lease_path=self.lease_file)
            self.assertTrue(res["backend_dark"])
            self.assertFalse(res["host_dark"])
            self.assertTrue(res["lease_released"])              # no arm ⇒ nothing host-side to prove
            self.assertEqual(RL.status_metadata(timezone.now(), self.lease_file)["status"], "ABSENT")
