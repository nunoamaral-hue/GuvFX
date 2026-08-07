"""ADR-0033 (Tension 3) — bridge order-time gate hardening.

Proved here:
1. MANDATORY per-job identity pin — enforced when the JOB declares require_identity_pin OR when the
   bridge/terminal is configured to require it (MT5_REQUIRE_IDENTITY_PIN). Expected (login, server) come
   from the PAYLOAD (never env), required for demo+live, fail-closed on a missing/half pin.
2. TOCTOU close — each opening path re-verifies immediately before order_send AND enforces the result
   (rejects on failure), with no account-changing MT5 call in between (asserted structurally).

The certified pure evaluate_binding is UNCHANGED (its mutation suite still passes).
"""
import os
import re
from unittest import mock

from django.test import SimpleTestCase

from execution.tests_bridge_binding import (
    _DEMO,
    _FakeInfo,
    _FakeMt5,
    _REAL,
    _TERM_OK,
    _load_bridge,
)


class MandatoryPinTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = _load_bridge()

    def _mt5(self, acc, term):
        return _FakeMt5(acc=_FakeInfo(**acc), term=_FakeInfo(**term))

    def _verify(self, acc, payload, env=None):
        env = env or {"MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": "", "MT5_ALLOW_LIVE": "",
                      "MT5_REQUIRE_IDENTITY_PIN": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            return self.mod.verify_execution_binding(self._mt5(acc, _TERM_OK), payload)

    def test_missing_pin_fails_closed_demo(self):
        ok, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True})
        self.assertFalse(ok)
        self.assertEqual(reason, "identity_pin_required")

    def test_half_pin_fails_closed(self):
        _, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True,
                                            "expected_login": str(_DEMO["login"])})
        self.assertEqual(reason, "identity_pin_required")
        _, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True,
                                            "expected_server": _DEMO["server"]})
        self.assertEqual(reason, "identity_pin_required")

    def test_matching_payload_pin_ok_demo(self):
        ok, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True,
                                             "expected_login": str(_DEMO["login"]),
                                             "expected_server": _DEMO["server"]})
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")

    def test_login_and_server_mismatch_deny(self):
        _, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True,
                                            "expected_login": "999", "expected_server": _DEMO["server"]})
        self.assertEqual(reason, "account_login_mismatch")
        _, reason, _ = self._verify(_DEMO, {"is_demo": True, "require_identity_pin": True,
                                            "expected_login": str(_DEMO["login"]),
                                            "expected_server": "OtherServer"})
        self.assertEqual(reason, "broker_server_mismatch")

    def test_pin_ignores_env_fallback(self):
        ok, reason, _ = self._verify(
            _DEMO,
            {"is_demo": True, "require_identity_pin": True,
             "expected_login": str(_DEMO["login"]), "expected_server": _DEMO["server"]},
            env={"MT5_EXPECTED_LOGIN": "111111", "MT5_EXPECTED_SERVER": "WrongServer",
                 "MT5_ALLOW_LIVE": "", "MT5_REQUIRE_IDENTITY_PIN": ""})
        self.assertTrue(ok, reason)

    def test_mandatory_pin_applies_to_live_too(self):
        ok, reason, _ = self._verify(
            _REAL, {"is_demo": False, "require_identity_pin": True},
            env={"MT5_ALLOW_LIVE": "true", "MT5_EXPECTED_LOGIN": "", "MT5_EXPECTED_SERVER": "",
                 "MT5_REQUIRE_IDENTITY_PIN": ""})
        self.assertFalse(ok)
        self.assertEqual(reason, "identity_pin_required")

    def test_terminal_level_require_pin_env(self):
        # MT5_REQUIRE_IDENTITY_PIN makes the pin mandatory for EVERY job on this bridge, even without the
        # per-job flag — so a persistent-workspace bridge cannot fall back to env pins (review MEDIUM).
        ok, reason, _ = self._verify(
            _DEMO, {"is_demo": True},  # no require_identity_pin in the payload
            env={"MT5_REQUIRE_IDENTITY_PIN": "1", "MT5_EXPECTED_LOGIN": str(_DEMO["login"]),
                 "MT5_EXPECTED_SERVER": _DEMO["server"], "MT5_ALLOW_LIVE": ""})
        # env pins are ignored under the terminal-level requirement; no payload pin ⇒ fail closed.
        self.assertFalse(ok)
        self.assertEqual(reason, "identity_pin_required")
        # With a matching payload pin it proceeds.
        ok, reason, _ = self._verify(
            _DEMO, {"is_demo": True, "expected_login": str(_DEMO["login"]),
                    "expected_server": _DEMO["server"]},
            env={"MT5_REQUIRE_IDENTITY_PIN": "1", "MT5_ALLOW_LIVE": ""})
        self.assertTrue(ok, reason)

    def test_legacy_path_unchanged(self):
        ok, reason, _ = self._verify(_DEMO, {"is_demo": True})
        self.assertTrue(ok, reason)


class TOCTOUCloseSourceTests(SimpleTestCase):
    """The TOCTOU close is a control-flow guard in the two opening order paths. Assert (at source level)
    that each re-verifies the binding immediately before order_send, ENFORCES the result (rejects on
    failure), and has no account-changing MT5 call between the re-verify and the send."""

    def setUp(self):
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self.src = open(os.path.join(repo, "scripts", "mt5_signal_bridge.py"), encoding="utf-8").read()

    def _body(self, func_name):
        m = re.search(rf"\ndef {func_name}\(", self.src)
        self.assertIsNotNone(m, f"{func_name} not found")
        rest = self.src[m.end():]
        nxt = re.search(r"\ndef ", rest)
        return rest[: nxt.start()] if nxt else rest

    def _assert_guarded_reverify_before_send(self, func_name):
        body = self._body(func_name)
        send = body.find("mt5.order_send(request)")
        self.assertGreater(send, -1, f"{func_name}: no order_send(request)")
        pre = body.rfind("verify_execution_binding(", 0, send)
        self.assertGreater(pre, -1, f"{func_name}: no verify_execution_binding before order_send")
        between = body[pre:send]
        # The re-verify RESULT must be enforced (a rejection returned) before the send.
        self.assertIn("binding_rejected", between, f"{func_name}: pre-send verify result not enforced")
        # No account-changing MT5 call may sit between the re-verify and the send.
        self.assertNotIn("mt5.login(", between)
        self.assertNotIn("mt5.initialize(", between)

    def test_poller_path(self):
        self._assert_guarded_reverify_before_send("execute_mt5_trade")

    def test_http_demo_path(self):
        self._assert_guarded_reverify_before_send("execute_demo_order")
