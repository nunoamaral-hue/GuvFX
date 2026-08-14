"""Beta Launch — the order-dispatch worker MUST forward the Provider-B per-job identity pin.

The backend injects ``require_identity_pin`` / ``expected_login`` / ``expected_server`` / ``is_demo`` onto a
hosted account's PLACE_ORDER ExecutionJob payload (``execution.hosted_pin.inject_identity_pin``). A hosted-
execution bridge runs ``MT5_REQUIRE_IDENTITY_PIN=1`` (mandatory; enforced in ``scripts/mt5_signal_bridge.py``
``verify_execution_binding``), so if the dispatcher (``mt5_trade_ingest_worker``) drops those fields the bridge
fails the hosted order CLOSED with ``identity_pin_required`` — the first beta trade would never place. This
proves the fix: the pin is forwarded for a hosted payload and is a no-op for a legacy one.
"""
from __future__ import annotations

import ast
import inspect

from django.test import SimpleTestCase

# The worker is a top-level module under backend/ (its import runs django.setup(), which is a no-op once the
# test runner has configured Django). We import ONLY the pure helper.
import mt5_trade_ingest_worker as worker
from mt5_trade_ingest_worker import apply_identity_pin


class WorkerIdentityPinForwardingTests(SimpleTestCase):
    def test_hosted_pin_is_forwarded(self):
        agent_payload = {"symbol": "EURUSD", "side": "BUY", "lots": 0.01}
        payload = {
            "require_identity_pin": True, "expected_login": "770077",
            "expected_server": "GuvfxBeta-Demo", "is_demo": True,
        }
        out = apply_identity_pin(agent_payload, payload)
        self.assertIs(out["require_identity_pin"], True)
        self.assertEqual(out["expected_login"], "770077")
        self.assertEqual(out["expected_server"], "GuvfxBeta-Demo")
        self.assertIs(out["is_demo"], True)
        # Non-pin fields are left exactly as they were (in-place, additive).
        self.assertEqual(out["symbol"], "EURUSD")

    def test_legacy_payload_adds_no_pin_keys(self):
        agent_payload = {"symbol": "EURUSD", "side": "BUY", "lots": 0.01}
        out = apply_identity_pin(agent_payload, {"symbol": "EURUSD", "signal_source": "ti_signals"})
        for key in ("require_identity_pin", "expected_login", "expected_server", "is_demo"):
            self.assertNotIn(key, out, f"legacy dispatch must not fabricate {key}")

    def test_partial_pin_is_not_fabricated(self):
        # Only present (non-None) keys forward; a half-pin is never invented (bridge would fail it closed,
        # which is the correct fail-closed behaviour — we must not paper over it by inventing a blank field).
        out = apply_identity_pin({}, {"require_identity_pin": True, "expected_login": "770077",
                                      "expected_server": None})
        self.assertIs(out["require_identity_pin"], True)
        self.assertEqual(out["expected_login"], "770077")
        self.assertNotIn("expected_server", out)

    def test_none_payload_is_safe(self):
        self.assertEqual(apply_identity_pin({"symbol": "EURUSD"}, None), {"symbol": "EURUSD"})


class WorkerIdentityPinWiringTests(SimpleTestCase):
    """Guard the LOAD-BEARING half of the fix: the dispatcher main() must actually CALL apply_identity_pin
    on every hosted dispatch path. The helper being correct (tested above) is worthless if main() never
    invokes it. main() is a monolithic while-loop with no separately-callable per-job unit, so we assert the
    wiring structurally via the AST — deleting a call site then FAILS this test, catching the exact
    regression the fix prevents (a hosted order/mutation dispatched without the pin -> bridge fails it
    CLOSED with identity_pin_required -> the first beta hosted trade never places)."""

    def _main_apply_pin_calls(self):
        tree = ast.parse(inspect.getsource(worker))
        main_fn = next((n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        self.assertIsNotNone(main_fn, "worker main() not found")
        return [n for n in ast.walk(main_fn)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "apply_identity_pin"]

    def test_main_forwards_pin_on_every_hosted_dispatch_path(self):
        # Three mutation dispatch sites must forward the pin: PLACE_ORDER (+ PLACE_TEST_ORDER, shared block),
        # MODIFY_POSITION, CLOSE_TRADE. Fewer means a hosted dispatch path silently drops the pin.
        calls = self._main_apply_pin_calls()
        self.assertGreaterEqual(
            len(calls), 3,
            f"main() must call apply_identity_pin on every hosted dispatch path (PLACE/MODIFY/CLOSE); "
            f"found {len(calls)} — a missing call site drops the identity pin and fails hosted orders closed")

    def test_each_apply_pin_call_passes_the_job_payload(self):
        # Each wiring call must pass the JOB payload as the source (apply_identity_pin(agent_payload, payload)),
        # not an empty/placeholder dict — otherwise nothing is forwarded even though the call is present.
        for call in self._main_apply_pin_calls():
            self.assertEqual(len(call.args), 2, "apply_identity_pin(agent_payload, payload) takes two args")
            second = call.args[1]
            self.assertTrue(isinstance(second, ast.Name) and second.id == "payload",
                            "the pin source must be the job `payload`, not a literal/placeholder")
