"""GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148 (ADR-0114) — deterministic, fail-closed tests for the
atomic OPEN+MODIFY+FULL_CLOSE lifecycle-arming contract, the arming orchestration + full rollback, the idempotent
DARK teardown, and the hardened preflight ``arm_readiness`` section.

No broker, Windows host, terminal, production endpoint or genuine secret. Host-side effects are injected fakes;
synthetic authorizations/accounts only. Reuses the ``copier_live_arm`` suite's ``_Base`` fixture.
"""
import json
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from trade_copier import lifecycle_arm_contract as contract
from trade_copier.lifecycle_arming import arm_live_lifecycle, master_clean
from trade_copier.lifecycle_teardown import teardown_live_dark
from trade_copier.models import CopierLiveArm, CopierLiveArmState, CopierLiveAuthorization, \
    CopierLiveAuthorizationStatus
from trade_copier.tests_copier_live_arm import _Base

# The complete OPEN+MODIFY+FULL_CLOSE flag set (partial-close deliberately OFF).
FULL_FLAGS = dict(
    COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
    COPIER_LIVE_MODIFY_ENABLED="1", COPIER_LIVE_FULL_CLOSE_ENABLED="1",
    COPIER_MUTATION_DISPATCH_ENABLED="1", COPIER_MUTATION_TRANSPORT_ENABLED="1",
    HOSTED_LIVE_EXECUTION_ENABLED="1")


def _ok_admission(authz):
    return {"ok": True, "armed": True, "reason": "live_bridge_activated"}


def _ok_emitter():
    return {"ok": True, "enabled": True}


def _probes_ready():
    return (lambda: {"ready": True, "identity_ok": True},   # emitter_key_probe
            lambda: {"off": True},                           # host_admission_probe
            lambda: {"disabled": True})                      # emitter_task_probe


# ───────────────────────────── pure contract units (no DB) ─────────────────────────────
class ContractUnitTests(_Base):
    def _mk(self, **over):
        """An UNSAVED authorization (the contract functions only read attributes) so several variants in one
        test never collide on the one-ACTIVE-per-pair unique constraint."""
        now = timezone.now()
        d = dict(user=self.owner, master_account=self.macct, follower_account=self.facct,
                 master_login=self.ml, master_server=self.ms, master_environment=self.me,
                 follower_login=self.fl, follower_server=self.fs, follower_environment=self.fe,
                 source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
                 max_volume=Decimal("0.01"), max_concurrent_positions=1, max_orders=1, orders_consumed=0,
                 valid_from=now, expires_at=now, watermark_at=now, watermark_event_sequence=5,
                 status=CopierLiveAuthorizationStatus.ACTIVE, approved_by=self.staff, reason="t",
                 modify_authority=True, full_close_authority=True)
        d.update(over)
        return CopierLiveAuthorization(**d)

    def test_full_lifecycle_authority_contract_ok(self):
        authz = self._mk()
        self.assertEqual(contract.authority_contract_blockers(
            authz, source_symbol="XAUUSD", follower_symbol="XAUUSD.std",
            master_account_id=self.macct.id, follower_account_id=self.facct.id), [])

    def test_missing_modify_authority_refused(self):
        authz = self._authz(modify_authority=False)
        self.assertIn(contract.MODIFY_AUTHORITY_MISSING, contract.authority_contract_blockers(authz))

    def test_missing_full_close_is_open_without_close(self):
        authz = self._authz(full_close_authority=False)
        b = contract.authority_contract_blockers(authz)
        self.assertIn(contract.FULL_CLOSE_AUTHORITY_MISSING, b)
        self.assertIn(contract.OPEN_WITHOUT_CLOSE_REFUSED, b)

    def test_partial_close_authority_refused(self):
        authz = self._authz(partial_close_authority=True)
        self.assertIn(contract.PARTIAL_CLOSE_NOT_PERMITTED, contract.authority_contract_blockers(authz))

    def test_volume_cap_and_order_cap(self):
        self.assertIn(contract.VOLUME_EXCEEDS_CAP,
                      contract.authority_contract_blockers(self._mk(max_volume=Decimal("0.02"))))
        self.assertIn(contract.MAX_ORDERS_NOT_ONE,
                      contract.authority_contract_blockers(self._mk(max_orders=2)))

    def test_route_and_symbol_mismatch(self):
        authz = self._authz()
        self.assertIn(contract.ROUTE_MISMATCH, contract.authority_contract_blockers(
            authz, master_account_id=self.macct.id, follower_account_id=self.macct.id))
        self.assertIn(contract.SYMBOL_MISMATCH, contract.authority_contract_blockers(
            authz, source_symbol="EURUSD", follower_symbol="EURUSD"))

    @override_settings(**FULL_FLAGS)
    def test_execution_flags_coherent_when_all_on(self):
        self.assertEqual(contract.execution_flag_blockers(self._authz()), [])

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                       COPIER_LIVE_FULL_CLOSE_ENABLED="1", COPIER_MUTATION_DISPATCH_ENABLED="1",
                       COPIER_MUTATION_TRANSPORT_ENABLED="1")
    def test_modify_flag_missing_not_armable(self):
        # MODIFY authority present but COPIER_LIVE_MODIFY_ENABLED off -> not armable.
        self.assertIn(contract.MODIFY_EXECUTION_NOT_ARMABLE, contract.execution_flag_blockers(self._authz()))

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                       COPIER_LIVE_MODIFY_ENABLED="1", COPIER_MUTATION_DISPATCH_ENABLED="1",
                       COPIER_MUTATION_TRANSPORT_ENABLED="1")
    def test_full_close_flag_missing_is_open_without_close(self):
        # OPEN + MODIFY armed but FULL_CLOSE flag off -> stranded-position risk -> refuse.
        b = contract.execution_flag_blockers(self._authz())
        self.assertIn(contract.FULL_CLOSE_EXECUTION_NOT_ARMABLE, b)
        self.assertIn(contract.OPEN_WITHOUT_CLOSE_REFUSED, b)

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                       COPIER_LIVE_MODIFY_ENABLED="1", COPIER_LIVE_FULL_CLOSE_ENABLED="1",
                       COPIER_LIVE_PARTIAL_CLOSE_ENABLED="1", COPIER_MUTATION_DISPATCH_ENABLED="1",
                       COPIER_MUTATION_TRANSPORT_ENABLED="1")
    def test_partial_close_flag_enabled_refused(self):
        self.assertIn(contract.PARTIAL_CLOSE_FLAG_ENABLED, contract.execution_flag_blockers(self._authz()))

    @override_settings(COPIER_ENABLED="1", COPIER_EXECUTE_ENABLED="1", COPIER_LIVE_EXECUTE_ENABLED="1",
                       COPIER_LIVE_MODIFY_ENABLED="1", COPIER_MUTATION_DISPATCH_ENABLED="1",
                       COPIER_MUTATION_TRANSPORT_ENABLED="1")
    def test_flag_without_authority_is_mismatch(self):
        # MODIFY flag on but the authorization does NOT grant modify_authority -> incoherent arm.
        self.assertIn(contract.LIFECYCLE_AUTHORITY_FLAG_MISMATCH,
                      contract.execution_flag_blockers(self._authz(modify_authority=False)))

    def test_teardown_command_is_available(self):
        self.assertTrue(contract.teardown_available())


# ───────────────────────────── atomic arming orchestration + rollback ─────────────────────────────
@override_settings(**FULL_FLAGS)
class ArmOrchestrationTests(_Base):
    def _arm(self, *, commit, authz=None, admission=_ok_admission, emitter=_ok_emitter,
             key_probe=None, admission_probe=None, task_probe=None, flat=True, teardown=None):
        authz = authz or self._authz()
        kp, ap, tp = _probes_ready()
        return arm_live_lifecycle(
            master_account_id=self.macct.id, follower_account_id=self.facct.id,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", authorization_id=authz.id,
            follower_flat_confirmed=flat, emitter_key_probe=key_probe or kp,
            host_admission_probe=admission_probe or ap, emitter_task_probe=task_probe or tp,
            admission_provisioner=admission, emitter_enabler=emitter, teardown=teardown,
            commit=commit, by_email=self.staff.email, reason="t"), authz

    def test_dry_run_full_contract_ok(self):
        res, _ = self._arm(commit=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["armed"])
        self.assertTrue(res["dry_run"])

    def test_commit_full_lifecycle_arms(self):
        res, authz = self._arm(commit=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["armed"])
        self.assertTrue(CopierLiveArm.objects.filter(
            authorization=authz, state=CopierLiveArmState.ENGAGED).exists())

    def test_post_arm_verify_tolerates_flipped_host_state(self):
        # After a real commit the admission is ON and the emitter ENABLED; the post-arm re-verify must NOT tear
        # down a correctly-armed stack by re-requiring the (now intentionally flipped) entry preconditions.
        authz = self._authz()
        state = {"admitted": False, "emitter_on": False}

        def admission(a):
            state["admitted"] = True
            return {"ok": True, "armed": True}

        def emitter():
            state["emitter_on"] = True
            return {"ok": True, "enabled": True}

        res = arm_live_lifecycle(
            master_account_id=self.macct.id, follower_account_id=self.facct.id,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", authorization_id=authz.id,
            follower_flat_confirmed=True, emitter_key_probe=lambda: {"ready": True, "identity_ok": True},
            host_admission_probe=lambda: {"off": not state["admitted"]},
            emitter_task_probe=lambda: {"disabled": not state["emitter_on"]},
            admission_provisioner=admission, emitter_enabler=emitter,
            commit=True, by_email=self.staff.email, reason="t")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["armed"])
        self.assertTrue(CopierLiveArm.objects.filter(
            authorization=authz, state=CopierLiveArmState.ENGAGED).exists())

    def test_missing_emitter_key_refuses_before_mutation(self):
        res, authz = self._arm(commit=True, key_probe=lambda: {"ready": False})
        self.assertFalse(res["ok"])
        self.assertEqual(res["stage"], "validate")
        self.assertIn(contract.EMITTER_KEY_ABSENT, res["blockers"])
        self.assertFalse(CopierLiveArm.objects.filter(authorization=authz).exists())

    def test_emitter_key_identity_invalid_refuses(self):
        res, _ = self._arm(commit=True, key_probe=lambda: {"ready": True, "identity_ok": False})
        self.assertFalse(res["ok"])
        self.assertIn(contract.EMITTER_KEY_IDENTITY_INVALID, res["blockers"])

    def test_follower_not_flat_confirmed_refuses(self):
        res, _ = self._arm(commit=True, flat=False)
        self.assertFalse(res["ok"])
        self.assertIn(contract.FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED, res["blockers"])

    def test_host_admission_not_off_refuses(self):
        res, _ = self._arm(commit=True, admission_probe=lambda: {"off": False})
        self.assertFalse(res["ok"])
        self.assertIn(contract.HOST_ADMISSION_NOT_OFF, res["blockers"])

    def test_emitter_task_not_disabled_refuses(self):
        res, _ = self._arm(commit=True, task_probe=lambda: {"disabled": False})
        self.assertFalse(res["ok"])
        self.assertIn(contract.EMITTER_TASK_NOT_DISABLED, res["blockers"])

    def test_missing_full_close_authority_refuses(self):
        res, _ = self._arm(commit=True, authz=self._authz(full_close_authority=False))
        self.assertFalse(res["ok"])
        self.assertIn(contract.OPEN_WITHOUT_CLOSE_REFUSED, res["blockers"])

    def test_master_clean_derivation_flat_when_no_events(self):
        clean, wm, open_pos = master_clean(self.macct.id)
        self.assertTrue(clean)
        self.assertEqual(open_pos, 0)
        self.assertEqual(wm, 0)

    def test_master_not_clean_refuses(self):
        with mock.patch("trade_copier.lifecycle_arming.master_clean", return_value=(False, 7, 1)):
            res, _ = self._arm(commit=True)
        self.assertFalse(res["ok"])
        self.assertIn("MASTER_NOT_CLEAN", res["blockers"])

    def test_admission_failure_rolls_back(self):
        res, authz = self._arm(commit=True, admission=lambda a: {"ok": False, "reason": "gate_failed"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "admission_failed")
        self.assertIn("rolled_back", res)
        self.assertFalse(CopierLiveArm.objects.filter(authorization=authz).exists())
        # a rollback that preserves the still-valid authorization is still backend-DARK (no engaged arm remains;
        # the arm is the execution AND-condition) — require_authz_absent=False.
        self.assertTrue(res["rolled_back"]["backend_dark"])
        authz.refresh_from_db()
        self.assertEqual(authz.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_emitter_enable_failure_rolls_back(self):
        res, authz = self._arm(commit=True, emitter=lambda: {"ok": False})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "emitter_enable_failed")
        self.assertFalse(CopierLiveArm.objects.filter(authorization=authz).exists())

    def test_arm_create_failure_rolls_back(self):
        with mock.patch("trade_copier.lifecycle_arming._create_arm", side_effect=RuntimeError("boom")):
            res, authz = self._arm(commit=True)
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"].startswith("arm_create_exception"))
        self.assertIn("rolled_back", res)
        self.assertFalse(CopierLiveArm.objects.filter(authorization=authz).exists())

    def test_expired_authorization_refuses(self):
        authz = self._authz(valid_from=timezone.now() - timezone.timedelta(hours=2),
                            expires_at=timezone.now() - timezone.timedelta(minutes=1))
        res, _ = self._arm(commit=True, authz=authz)
        self.assertFalse(res["ok"])
        self.assertIn("AUTHZ_OUT_OF_WINDOW", res["blockers"])

    def test_repeated_arm_is_idempotent(self):
        res1, authz = self._arm(commit=True)
        self.assertTrue(res1["armed"])
        res2, _ = self._arm(commit=True, authz=authz)
        self.assertTrue(res2["armed"])
        self.assertTrue(res2["arm_idempotent"])
        self.assertEqual(
            CopierLiveArm.objects.filter(authorization=authz, state=CopierLiveArmState.ENGAGED).count(), 1)

    def test_no_secret_in_result(self):
        res, _ = self._arm(commit=True)
        blob = json.dumps(res, default=str)
        # the arming path only ever handles booleans/ids; assert no long opaque token leaked.
        self.assertNotRegex(blob, r"[A-Fa-f0-9]{32,}")


# ───────────────────────────── idempotent DARK teardown ─────────────────────────────
@override_settings(**FULL_FLAGS)
class TeardownTests(_Base):
    def _fake_actuators(self):
        return (lambda: {"ok": True}, lambda: {"ok": True}, lambda: {"ok": True})

    def test_teardown_after_success_is_dark(self):
        from trade_copier.tests_copier_live_arm import attach_engaged_arm
        authz = self._authz()
        attach_engaged_arm(authz)
        ar, ed, kr = self._fake_actuators()
        res = teardown_live_dark(master_account_id=self.macct.id, follower_account_id=self.facct.id,
                                 authorization_id=authz.id, admission_restore=ar, emitter_disabler=ed,
                                 emitter_key_remover=kr, revoked_by_email=self.staff.email)
        self.assertTrue(res["backend_dark"], res)
        self.assertTrue(res["host_dark"])
        self.assertTrue(res["dark"])
        authz.refresh_from_db()
        self.assertEqual(authz.status, CopierLiveAuthorizationStatus.REVOKED)
        self.assertFalse(CopierLiveArm.objects.filter(
            authorization=authz, state=CopierLiveArmState.ENGAGED).exists())

    def test_teardown_is_idempotent_on_already_dark(self):
        ar, ed, kr = self._fake_actuators()
        kw = dict(master_account_id=self.macct.id, follower_account_id=self.facct.id,
                  admission_restore=ar, emitter_disabler=ed, emitter_key_remover=kr)
        r1 = teardown_live_dark(**kw)
        r2 = teardown_live_dark(**kw)
        self.assertTrue(r1["backend_dark"])
        self.assertTrue(r2["backend_dark"])  # re-running changes nothing and still reports DARK

    def test_teardown_partial_state_returns_dark(self):
        # partial arm: an engaged arm exists but no host admission was ever provisioned.
        from trade_copier.tests_copier_live_arm import attach_engaged_arm
        authz = self._authz()
        attach_engaged_arm(authz)
        ar, ed, kr = self._fake_actuators()
        res = teardown_live_dark(master_account_id=self.macct.id, follower_account_id=self.facct.id,
                                 admission_restore=ar, emitter_disabler=ed, emitter_key_remover=kr)
        self.assertTrue(res["backend_dark"])
        self.assertEqual(res["steps"]["disarm"]["changed"], 1)

    def test_teardown_without_host_actuators_reports_unproven(self):
        res = teardown_live_dark(master_account_id=self.macct.id, follower_account_id=self.facct.id)
        self.assertFalse(res["host_dark"])  # host steps without an actuator are UNPROVEN, never 'done'
        self.assertFalse(res["steps"]["host_admission_off"]["proven"])

    def test_rollback_preserves_authorization(self):
        authz = self._authz()
        res = teardown_live_dark(master_account_id=self.macct.id, follower_account_id=self.facct.id,
                                 authorization_id=authz.id, revoke_authorization=False)
        self.assertTrue(res["steps"]["revoke_authorization"].get("skipped"))
        authz.refresh_from_db()
        self.assertEqual(authz.status, CopierLiveAuthorizationStatus.ACTIVE)


# ───────────────────────────── legacy copier_live_arm authority guard (ADR-0114) ─────────────────────────────
class LegacyArmAuthorityGuardTests(_Base):
    """The legacy control-plane arm must ALSO refuse an authorization that would permit an OPEN with no executable
    CLOSE, so the stranding guard is not bypassable by using the pre-existing command."""

    def test_legacy_arm_refuses_open_only_authz(self):
        authz = self._authz(full_close_authority=False)
        with self.assertRaises(CommandError) as cm:
            self._arm(authorization_id=authz.id, by=self.staff.email, reason="t")
        self.assertIn("authority contract", str(cm.exception))

    def test_legacy_arm_refuses_partial_close_authz(self):
        authz = self._authz(partial_close_authority=True)
        with self.assertRaises(CommandError) as cm:
            self._arm(authorization_id=authz.id, by=self.staff.email, reason="t")
        self.assertIn("authority contract", str(cm.exception))

    def test_legacy_arm_accepts_full_lifecycle_authz(self):
        authz = self._authz()  # modify + full_close authority by default
        out = self._arm(authorization_id=authz.id, by=self.staff.email, reason="t")
        self.assertIn('"action": "arm"', out)


# ───────────────────────────── hardened preflight arm_readiness ─────────────────────────────
class PreflightArmReadinessTests(_Base):
    def _preflight(self):
        buf = StringIO()
        call_command("copier_live_demo_preflight", master_account_id=self.macct.id,
                     follower_account_id=self.facct.id, source_symbol="XAUUSD",
                     follower_symbol="XAUUSD.std", stdout=buf)
        return json.loads(buf.getvalue())

    def test_arm_readiness_present_and_fail_closed(self):
        out = self._preflight()
        self.assertIn("arm_readiness", out)
        self.assertIn("arm_ready", out)
        self.assertFalse(out["arm_ready"])  # host/human gates unverified in a read-only preflight
        b = out["arm_readiness"]["blockers"]
        for code in (contract.EMITTER_KEY_ABSENT, contract.FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED,
                     contract.HOST_ADMISSION_NOT_OFF, contract.EMITTER_TASK_NOT_DISABLED):
            self.assertIn(code, b)

    def test_no_authz_reports_authz_none_in_arm_readiness(self):
        out = self._preflight()
        self.assertIn(contract.AUTHZ_NONE, out["arm_readiness"]["blockers"])

    def test_open_only_authz_flags_open_without_close(self):
        self._authz(full_close_authority=False)
        out = self._preflight()
        self.assertIn(contract.OPEN_WITHOUT_CLOSE_REFUSED, out["arm_readiness"]["blockers"])

    def test_teardown_not_proven_absent_when_command_present(self):
        out = self._preflight()
        self.assertNotIn(contract.TEARDOWN_NOT_PROVEN, out["arm_readiness"]["blockers"])

    def test_legacy_blockers_unchanged_by_hardening(self):
        # The legacy ``blockers`` list (consumed by copier_live_arm) must NOT carry the new arm-readiness codes.
        out = self._preflight()
        for code in (contract.EMITTER_KEY_ABSENT, contract.FOLLOWER_FLAT_HUMAN_CONFIRM_REQUIRED,
                     contract.HOST_ADMISSION_NOT_OFF):
            self.assertNotIn(code, out["blockers"])
