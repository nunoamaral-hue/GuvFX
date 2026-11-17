"""GFX-COPIER-PRODUCTION-ACTUATORS-PIPELINE-153 (ADR-0117) — tests for the signed emitter host ops, the production
actuator adapters that REMOVE the ``emitter_enabler=None`` dead end, and the arm/teardown wiring.

No real host, broker, terminal, production endpoint or genuine secret: the signed executor is faked / resolved to
None (DARK), and host dispatch is mocked at the method boundary. Every adapter must FAIL CLOSED when the signed
executor is unavailable (the repository-only / DARK phase), and the risk-reducing teardown actuators must work
without the arm flag.
"""
import json
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES
from hosted_workspace.host_executor import SignedHostExecutor
from hosted_workspace.host_protocol import HOSTED_OPERATIONS, sign_hosted_request
from trade_copier import live_host_actuators as lha
from trade_copier.lifecycle_arming import arm_live_lifecycle
from trade_copier.tests_copier_live_arm import _Base
from trade_copier.tests_lifecycle_arming import FULL_FLAGS

_NEW_OPS = ("ENABLE_COPIER_EMITTER", "DISABLE_COPIER_EMITTER",
            "REMOVE_COPIER_EMITTER_KEY", "READ_COPIER_HOST_STATE")

_STANDING_ON = dict(COPIER_ENABLED="1", COPIER_EMITTER_STANDING_ENABLED="1")


class _FakeExecutor:
    """A minimal stand-in for ``SignedHostExecutor`` (no transport/signing). Records the ops it is asked to run and
    returns a canned (already 'verified') result. TEST-ONLY — never used in production code."""

    def __init__(self, account_id=46, result=None, calls=None):
        self.account_id = account_id
        self._result = result if result is not None else {"ok": True}
        self.calls = calls if calls is not None else []

    def _run(self, op):
        self.calls.append(op)
        r = dict(self._result)
        r.setdefault("account_id", self.account_id)
        r.setdefault("operation", op)
        return r

    def enable_copier_emitter(self):
        return self._run("ENABLE_COPIER_EMITTER")

    def disable_copier_emitter(self):
        return self._run("DISABLE_COPIER_EMITTER")

    def remove_copier_emitter_key(self):
        return self._run("REMOVE_COPIER_EMITTER_KEY")

    def read_copier_host_state(self):
        return self._run("READ_COPIER_HOST_STATE")


def _bare_executor(account_id):
    """A real ``SignedHostExecutor`` whose transport raises if ever called — so a test proves the #46 PIN refuses
    BEFORE any host contact. ``_send`` is mocked in the positive-path tests."""
    def _boom(*_a, **_k):
        raise AssertionError("transport must not be called")
    return SignedHostExecutor(account_id=account_id, rdp_host="", transport=_boom,
                              keyring={"k": "x" * 40}, key_id="k", base_url="http://x",
                              seal_password=lambda *_a, **_k: {})


# ───────────────────────────── signed executor ops: registration + #46 pin ─────────────────────────────
class ExecutorOpsTests(SimpleTestCase):
    def test_new_ops_registered_and_signable(self):
        for op in _NEW_OPS:
            self.assertIn(op, HOSTED_OPERATIONS, f"{op} missing from HOSTED_OPERATIONS")
            self.assertIn(op, OP_PRIMITIVES, f"{op} missing from OP_PRIMITIVES")
            self.assertEqual(OP_PRIMITIVES[op]["params_allow"], ())   # the emitter ops carry NO caller params
            req = sign_hosted_request(account_id=46, operation=op, correlation_id="c",
                                      keyring={"k": "s" * 40}, key_id="k", now=1000, params={}, nonce="n")
            self.assertEqual(req["operation"], op)

    def test_op_primitives_cover_hosted_operations(self):
        # Defence against a half-edit that registers an op in one table only (the module asserts this on import).
        self.assertEqual(set(OP_PRIMITIVES), set(HOSTED_OPERATIONS))

    def test_pinned_ops_dispatch_on_46(self):
        ex = _bare_executor(46)
        for method, op in (("enable_copier_emitter", "ENABLE_COPIER_EMITTER"),
                           ("disable_copier_emitter", "DISABLE_COPIER_EMITTER"),
                           ("remove_copier_emitter_key", "REMOVE_COPIER_EMITTER_KEY"),
                           ("read_copier_host_state", "READ_COPIER_HOST_STATE")):
            with mock.patch.object(ex, "_send", return_value={"ok": True}) as m:
                res = getattr(ex, method)()
            self.assertTrue(res["ok"])
            m.assert_called_once_with(op)

    def test_pinned_ops_refuse_non_46_before_host_contact(self):
        ex = _bare_executor(48)   # the follower — must never be the emitter
        for method in ("enable_copier_emitter", "disable_copier_emitter",
                       "remove_copier_emitter_key", "read_copier_host_state"):
            with mock.patch.object(ex, "_send") as m:
                res = getattr(ex, method)()
            self.assertFalse(res["ok"])
            self.assertEqual(res["reason"], "account_not_emitter_pinned")
            m.assert_not_called()   # refused BEFORE any host contact


# ───────────────────────────── production adapters: DARK fail-closed + flag gating ─────────────────────────────
class AdapterTests(SimpleTestCase):
    @override_settings(COPIER_ENABLED="1")   # standing flag deliberately OFF
    def test_enabler_refuses_when_standing_flag_off(self):
        enabler = lha.make_emitter_enabler(executor_resolver=lambda _a=None: _FakeExecutor())
        r = enabler()
        self.assertFalse(r["ok"])
        self.assertFalse(r["enabled"])
        self.assertEqual(r["reason"], "copier_emitter_standing_disabled")

    @override_settings(**_STANDING_ON)
    def test_enabler_fails_closed_when_executor_dark(self):
        enabler = lha.make_emitter_enabler(executor_resolver=lambda _a=None: None)
        r = enabler()
        self.assertFalse(r["ok"])
        self.assertFalse(r["enabled"])
        self.assertEqual(r["reason"], "host_executor_unavailable")

    @override_settings(**_STANDING_ON)
    def test_enabler_ok_with_verified_attestation(self):
        calls = []
        fake = _FakeExecutor(result={"ok": True}, calls=calls)
        r = lha.make_emitter_enabler(executor_resolver=lambda _a=None: fake)()
        self.assertTrue(r["ok"] and r["enabled"])
        self.assertEqual(calls, ["ENABLE_COPIER_EMITTER"])   # no compensating disable on success

    @override_settings(**_STANDING_ON)
    def test_enabler_compensating_disable_on_unverified(self):
        calls = []
        fake = _FakeExecutor(result={"ok": False, "reason": "host_boom"}, calls=calls)
        r = lha.make_emitter_enabler(executor_resolver=lambda _a=None: fake)()
        self.assertFalse(r["ok"])
        self.assertFalse(r["enabled"])
        self.assertTrue(r["reason"].startswith("enable_unverified:"))
        # never leave it enabled on a failed verification -> a compensating DISABLE must have run
        self.assertEqual(calls, ["ENABLE_COPIER_EMITTER", "DISABLE_COPIER_EMITTER"])

    @override_settings(**_STANDING_ON)
    def test_enabler_rejects_wrong_account_echo(self):
        # a (compromised) host echoing a different account id must fail closed.
        fake = _FakeExecutor(result={"ok": True, "account_id": 99})
        r = lha.make_emitter_enabler(executor_resolver=lambda _a=None: fake)()
        self.assertFalse(r["ok"])
        self.assertIn("account_mismatch", r["reason"])

    @override_settings(**_STANDING_ON)
    def test_enabler_rejects_truncated_attestation(self):
        # a truncated {"ok": true} with NO account/operation echo must fail closed (not default to the expected).
        class _Bare:
            account_id = 46

            def enable_copier_emitter(self):
                return {"ok": True}   # no account_id, no operation

            def disable_copier_emitter(self):
                return {"ok": True}

        r = lha.make_emitter_enabler(executor_resolver=lambda _a=None: _Bare())()
        self.assertFalse(r["ok"])
        self.assertTrue(r["reason"].startswith("enable_unverified:"))
        self.assertIn("echo_absent", r["reason"])

    @override_settings(COPIER_ENABLED="1")   # standing flag OFF — the disabler must STILL work (risk-reducing)
    def test_disabler_is_not_flag_gated(self):
        fake = _FakeExecutor(result={"ok": True})
        r = lha.make_emitter_disabler(executor_resolver=lambda _a=None: fake)()
        self.assertTrue(r["ok"] and r["disabled"])

    def test_disabler_and_key_remover_fail_closed_when_dark(self):
        self.assertEqual(lha.make_emitter_disabler(executor_resolver=lambda _a=None: None)()["reason"],
                         "host_executor_unavailable")
        self.assertEqual(lha.make_emitter_key_remover(executor_resolver=lambda _a=None: None)()["reason"],
                         "host_executor_unavailable")

    def test_key_remover_ok(self):
        calls = []
        fake = _FakeExecutor(result={"ok": True}, calls=calls)
        r = lha.make_emitter_key_remover(executor_resolver=lambda _a=None: fake)()
        self.assertTrue(r["ok"] and r["removed"])
        self.assertEqual(calls, ["REMOVE_COPIER_EMITTER_KEY"])

    def test_read_state_is_value_blind(self):
        fake = _FakeExecutor(result={"ok": True, "emitter_task_disabled": True, "ingest_key_present": False,
                                     "ingest_key_acl_ok": True})
        r = lha.read_emitter_host_state(executor_resolver=lambda _a=None: fake)
        self.assertTrue(r["ok"])
        self.assertTrue(r["emitter_task_disabled"])
        self.assertFalse(r["ingest_key_present"])
        self.assertTrue(r["ingest_key_acl_ok"])
        # the returned dict exposes only metadata booleans + the attestation echo — never a key value.
        self.assertEqual(set(r) - {"attestation"},
                         {"ok", "emitter_task_enabled", "emitter_task_disabled",
                          "ingest_key_present", "ingest_key_acl_ok"})

    def test_read_state_dark(self):
        self.assertEqual(lha.read_emitter_host_state(executor_resolver=lambda _a=None: None)["reason"],
                         "host_executor_unavailable")

    def test_admission_adapters_wrap_integrator(self):
        with mock.patch("trade_copier.live_bridge_provision.restore_off", return_value={"ok": True}) as m:
            r = lha.make_admission_revoker(48, executor_resolver="RES")()
        m.assert_called_once_with(48, commit=True, executor_resolver="RES")
        self.assertTrue(r["ok"])

        class _A:
            id = 7
        with mock.patch("trade_copier.live_bridge_provision.provision_live",
                        return_value={"ok": True, "armed": True}) as m:
            r = lha.make_admission_provisioner(48, executor_resolver="RES")(_A())
        m.assert_called_once_with(48, 7, commit=True, executor_resolver="RES")
        self.assertTrue(r["ok"] and r["armed"])


# ───────────────────────────── the dead end is GONE: arm reaches the real emitter step ─────────────────────────────
@override_settings(**FULL_FLAGS)
class ArmWiringTests(_Base):
    def setUp(self):
        super().setUp()
        # These tests prove the emitter_enabler=None dead end is gone (the arm reaches the REAL emitter/admission
        # step). The ADR-0101 verifier-window gate is a separate precondition tested by VerifierWindowGateTests, so
        # stub it ready here.
        _p = mock.patch("trade_copier.verifier_window.covers_live_authorization",
                        return_value={"ready": True, "reason": "ok"})
        _p.start()
        self.addCleanup(_p.stop)

    def _probes(self):
        return (lambda: {"ready": True, "identity_ok": True},
                lambda: {"off": True},
                lambda: {"disabled": True})

    @override_settings(**{**FULL_FLAGS, **_STANDING_ON})
    def test_commit_fails_at_real_emitter_not_dead_end(self):
        authz = self._authz()
        kp, ap, tp = self._probes()
        res = arm_live_lifecycle(
            master_account_id=self.macct.id, follower_account_id=self.facct.id,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", authorization_id=authz.id,
            follower_flat_confirmed=True, emitter_key_probe=kp, host_admission_probe=ap, emitter_task_probe=tp,
            admission_provisioner=lambda _authz: {"ok": True, "armed": True},   # admission passes (fake)
            emitter_enabler=lha.make_emitter_enabler(executor_resolver=lambda _a=None: None),   # real adapter, DARK
            commit=True, by_email=self.staff.email, reason="t")
        self.assertFalse(res["ok"])
        self.assertFalse(res["armed"])
        # the arm now fails at the REAL emitter step (truthful), never the old "emitter_enabler_unavailable".
        self.assertEqual(res["reason"], "emitter_enable_failed")
        self.assertNotEqual(res["reason"], "emitter_enabler_unavailable")

    def test_command_commit_has_no_emitter_enabler_dead_end(self):
        """The command's --commit now constructs a REAL emitter_enabler + admission_provisioner, so the arm fails
        closed at a real host step (admission, because the synthetic account has no READY live endpoint) — never
        the removed ``emitter_enabler_unavailable`` dead end."""
        authz = self._authz()
        with self.assertRaises(CommandError) as cm:
            call_command("arm_copier_live_lifecycle",
                         "--master-account-id", str(self.macct.id),
                         "--follower-account-id", str(self.facct.id),
                         "--source-symbol", "XAUUSD", "--follower-symbol", "XAUUSD.std",
                         "--authorization-id", str(authz.id), "--by", self.staff.email, "--reason", "t",
                         "--follower-flat-confirmed", "--emitter-key-ready", "--host-admission-off",
                         "--emitter-task-disabled", "--commit")
        msg = str(cm.exception)
        self.assertNotIn("emitter_enabler_unavailable", msg)
        self.assertIn("arm refused", msg)


# ───────────────────────────── Phase 3: ADR-0101 verifier-window arm gate ─────────────────────────────
@override_settings(**FULL_FLAGS)
class VerifierWindowGateTests(_Base):
    def setUp(self):
        super().setUp()
        from trade_copier.verifier_window import close_all
        self.addCleanup(close_all)   # never leak an OPEN window into a sibling test (single-flight)

    def _short_authz(self, minutes=5):
        from datetime import timedelta
        from django.utils import timezone
        return self._authz(expires_at=timezone.now() + timedelta(minutes=minutes))

    def _dry_arm(self, authz):
        return arm_live_lifecycle(
            master_account_id=self.macct.id, follower_account_id=self.facct.id,
            source_symbol="XAUUSD", follower_symbol="XAUUSD.std", authorization_id=authz.id,
            follower_flat_confirmed=True,
            emitter_key_probe=lambda: {"ready": True, "identity_ok": True},
            host_admission_probe=lambda: {"off": True},
            emitter_task_probe=lambda: {"disabled": True},
            commit=False, by_email=self.staff.email, reason="t")

    def test_covers_requires_open_ingest_window(self):
        from trade_copier.verifier_window import covers_live_authorization
        self.assertEqual(covers_live_authorization(self._short_authz())["reason"], "no_open_window")

    def test_covers_rejects_wrong_purpose_and_short_window(self):
        from trade_copier.verifier_window import covers_live_authorization, verification_window
        authz = self._short_authz(minutes=10)
        with verification_window("x" * 40, purpose="ATTEST", ttl_seconds=800):
            self.assertEqual(covers_live_authorization(authz)["reason"], "purpose_not_ingest")
        with verification_window("x" * 40, purpose="INGEST", ttl_seconds=60):
            self.assertEqual(covers_live_authorization(authz)["reason"],
                             "window_expires_before_authorization")

    def test_covers_ok_with_covering_ingest_window(self):
        from trade_copier.verifier_window import covers_live_authorization, verification_window
        authz = self._short_authz(minutes=5)
        with verification_window("x" * 40, purpose="INGEST", ttl_seconds=600):
            self.assertTrue(covers_live_authorization(authz)["ready"])

    def test_covers_refuses_use_capped_window(self):
        # a max_uses-capped window auto-closes after the first ingested event -> would strand the follower CLOSE.
        from trade_copier.verifier_window import covers_live_authorization, verification_window
        authz = self._short_authz(minutes=5)
        with verification_window("x" * 40, purpose="INGEST", ttl_seconds=600, max_uses=1):
            self.assertEqual(covers_live_authorization(authz)["reason"], "window_use_capped")

    def test_arm_refuses_without_window(self):
        res = self._dry_arm(self._short_authz())
        self.assertFalse(res["ok"])
        self.assertIn("VERIFIER_WINDOW_NOT_READY", res["detail"]["blockers"])

    def test_arm_validates_with_covering_window(self):
        from trade_copier.verifier_window import verification_window
        authz = self._short_authz(minutes=5)
        with verification_window("x" * 40, purpose="INGEST", ttl_seconds=600):
            res = self._dry_arm(authz)
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("dry_run"))
        self.assertNotIn("VERIFIER_WINDOW_NOT_READY", res["detail"]["blockers"])

    def test_teardown_closes_and_shreds_window(self):
        import os
        import tempfile
        from trade_copier.lifecycle_teardown import teardown_live_dark
        from trade_copier.verifier_window import open_window, window_status, write_key_file
        fd, path = tempfile.mkstemp(prefix="guvfx_test_", suffix=".key")
        os.close(fd)
        write_key_file(path, "x" * 40)
        open_window(purpose="INGEST", ttl_seconds=600, key_path=path)
        self.assertTrue(window_status()["open"])
        res = teardown_live_dark(master_account_id=self.macct.id, follower_account_id=self.facct.id,
                                 authorization_id=None, revoke_authorization=False)
        self.assertTrue(res["steps"]["verifier_window_closed"]["ok"])
        self.assertFalse(window_status()["open"])
        self.assertFalse(os.path.exists(path))   # ephemeral key shredded
