"""
EXEC-E2b tests — shadow worker + bridge order_check() dry-run.

Central guarantee: the shadow path calls mt5.order_check() and NEVER
mt5.order_send(). Proves: order_check called exactly once, order_send called zero
times; shadow_order_check builds the EXACT SAME request as execute_demo_order;
the live execute_demo_order path is unchanged (still calls order_send); demo
enforcement preserved; invalid symbol / market-closed / non-demo fail safely; and
the worker routes SHADOW to order_check (never the live path), fails closed on
non-SHADOW modes, and stores validation (no ticket) on SUCCESS.
"""

import importlib.util
import os
import sys
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

BRIDGE_PATH = settings.BASE_DIR.parent / "scripts" / "mt5_signal_bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("mt5_signal_bridge_e2b", str(BRIDGE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_mt5():
    """A MetaTrader5 stand-in: demo account, valid symbol/tick, order_check OK.

    order_send is left as an auto-mock so tests can assert it is NEVER called.
    """
    m = mock.MagicMock(name="MetaTrader5")
    m.initialize.return_value = True
    acct = mock.MagicMock(); acct.trade_mode = 0  # DEMO
    m.account_info.return_value = acct
    sym = mock.MagicMock(); sym.visible = True
    m.symbol_info.return_value = sym
    tick = mock.MagicMock(); tick.ask = 1.0850; tick.bid = 1.0849
    m.symbol_info_tick.return_value = tick
    check = mock.MagicMock()
    check.retcode = 0; check.margin = 5.0; check.margin_free = 9995.0
    check.comment = "Done"; check.balance = 10000.0
    m.order_check.return_value = check
    # order_send returns a "done" result so the LIVE path regression test passes.
    send = mock.MagicMock(); send.retcode = m.TRADE_RETCODE_DONE
    send.order = 111; send.deal = 222; send.price = 1.0850; send.volume = 0.01
    m.order_send.return_value = send
    return m


ORDER = {"symbol": "EURUSD", "side": "BUY", "lots": 0.01, "comment": "WAY1L1",
         "magic": 7, "sl": 1.0800, "tp": 1.0900}


class ShadowOrderCheckBridgeTests(SimpleTestCase):
    def setUp(self):
        self.bridge = _load_bridge()
        self.mt5 = _fake_mt5()
        sys.modules["MetaTrader5"] = self.mt5
        self.addCleanup(lambda: sys.modules.pop("MetaTrader5", None))

    def test_order_check_called_once_order_send_never(self):
        res = self.bridge.shadow_order_check(dict(ORDER))
        self.assertTrue(res["ok"])
        self.assertTrue(res["shadow"])
        self.assertFalse(res["order_send_called"])
        self.mt5.order_check.assert_called_once()
        self.mt5.order_send.assert_not_called()  # THE guarantee

    def test_returns_validation_diagnostics_no_ticket(self):
        res = self.bridge.shadow_order_check(dict(ORDER))
        self.assertEqual(res["retcode"], 0)
        self.assertEqual(res["margin"], 5.0)
        self.assertEqual(res["free_margin"], 9995.0)
        self.assertIn("request", res)
        self.assertNotIn("order", res)  # no ticket
        self.assertNotIn("deal", res)   # no deal

    def test_shadow_request_identical_to_live_request(self):
        # execute_demo_order (live) and shadow_order_check must build the SAME request.
        self.bridge.execute_demo_order(dict(ORDER))
        self.bridge.shadow_order_check(dict(ORDER))
        live_req = self.mt5.order_send.call_args.args[0]
        shadow_req = self.mt5.order_check.call_args.args[0]
        self.assertEqual(live_req, shadow_req)

    def test_live_path_still_calls_order_send(self):
        # Regression: execute_demo_order is unchanged and still places (mock) order.
        res = self.bridge.execute_demo_order(dict(ORDER))
        self.assertTrue(res["ok"])
        self.mt5.order_send.assert_called_once()

    def test_non_demo_account_fails_and_no_order_check(self):
        self.mt5.account_info.return_value.trade_mode = 2  # REAL
        res = self.bridge.shadow_order_check(dict(ORDER))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "account_not_demo")
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_unsupported_symbol_rejected_by_mt5_no_order(self):
        # GFX-PKT-BROKER-SYMBOL-BRIDGE-ALIGNMENT: the static allowlist is gone; a symbol
        # is now validated against the terminal. One MT5 does not offer is rejected
        # fail-closed with SYMBOL_NOT_AVAILABLE_ON_MT5 — no order_check, no order_send.
        self.mt5.symbol_info.return_value = None
        res = self.bridge.shadow_order_check({**ORDER, "symbol": "BTCUSD"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "SYMBOL_NOT_AVAILABLE_ON_MT5")
        self.mt5.order_check.assert_not_called()
        self.mt5.order_send.assert_not_called()

    def test_lots_over_cap_fails(self):
        res = self.bridge.shadow_order_check({**ORDER, "lots": 0.05})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "lots_out_of_range")
        self.mt5.order_send.assert_not_called()

    def test_market_closed_retcode_fails_safely_no_order_send(self):
        self.mt5.order_check.return_value.retcode = 10018  # TRADE_RETCODE_MARKET_CLOSED
        res = self.bridge.shadow_order_check(dict(ORDER))
        self.assertFalse(res["ok"])
        self.assertEqual(res["retcode"], 10018)
        self.mt5.order_send.assert_not_called()

    def test_tick_failure_handled(self):
        self.mt5.symbol_info_tick.return_value = None
        res = self.bridge.shadow_order_check(dict(ORDER))
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "tick_failed")
        self.mt5.order_send.assert_not_called()


class ShadowWorkerTests(SimpleTestCase):
    """handle_shadow_job routes SHADOW to order_check-only, never the live path."""

    def setUp(self):
        import mt5_trade_ingest_worker as worker
        self.worker = worker

    def _job(self, mode="SHADOW", **payload):
        base = {"symbol": "EURUSD", "side": "BUY", "lots": "0.01", "comment": "WAY1L1",
                "execution_mode": mode, "sl_price": "1.0800", "tp_price": "1.0900"}
        base.update(payload)
        return {"id": 5, "job_type": "PLACE_ORDER_SHADOW", "account": 1, "payload": base}

    def test_shadow_calls_order_check_not_live(self):
        with mock.patch.object(self.worker, "agent_order_check",
                               return_value={"ok": True, "retcode": 0, "margin": 5.0,
                                             "free_margin": 9995.0, "comment": "Done"}) as chk, \
             mock.patch.object(self.worker, "agent_order") as live, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})) as comp:
            res = self.worker.handle_shadow_job(self._job())
        chk.assert_called_once()
        live.assert_not_called()  # NEVER the live /mt5/order path
        self.assertTrue(res["ok"])
        self.assertFalse(res["order_send_called"])
        # completed SUCCESS with validation; no ticket/deal/order id stored
        args = comp.call_args.args
        self.assertEqual(args[1], "SUCCESS")
        self.assertNotIn("order", args[2])
        self.assertNotIn("deal", args[2])
        self.assertIn("validation_latency_ms", args[2])

    def test_live_mode_fails_closed_no_bridge_call(self):
        with mock.patch.object(self.worker, "agent_order_check") as chk, \
             mock.patch.object(self.worker, "agent_order") as live, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})) as comp:
            res = self.worker.handle_shadow_job(self._job(mode="LIVE"))
        chk.assert_not_called()
        live.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(comp.call_args.args[1], "FAILED")
        self.assertEqual(res["error"], "execution_mode_not_shadow")

    def test_unknown_mode_fails_closed(self):
        with mock.patch.object(self.worker, "agent_order_check") as chk, \
             mock.patch.object(self.worker, "agent_order") as live, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})):
            res = self.worker.handle_shadow_job(self._job(mode="WOBBLE"))
        chk.assert_not_called()
        live.assert_not_called()
        self.assertFalse(res["ok"])

    def test_missing_mode_fails_closed(self):
        job = self._job()
        del job["payload"]["execution_mode"]
        with mock.patch.object(self.worker, "agent_order_check") as chk, \
             mock.patch.object(self.worker, "agent_order") as live, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})):
            res = self.worker.handle_shadow_job(job)
        chk.assert_not_called()
        live.assert_not_called()
        self.assertFalse(res["ok"])

    def test_order_check_failure_marks_failed_no_live_call(self):
        with mock.patch.object(self.worker, "agent_order_check",
                               return_value={"ok": False, "error": "order_rejected",
                                             "retcode": 10018}) as chk, \
             mock.patch.object(self.worker, "agent_order") as live, \
             mock.patch.object(self.worker, "complete_job", return_value=(200, {})) as comp:
            res = self.worker.handle_shadow_job(self._job())
        chk.assert_called_once()
        live.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(comp.call_args.args[1], "FAILED")

    def test_shadow_worker_is_http_only_cannot_call_order_send(self):
        # The worker never imports MetaTrader5, so it CANNOT call mt5.order_send at
        # all — it only POSTs to the bridge. (order_send appears only in docstrings
        # explaining what it never does, and the order_send_called=False key.) The
        # bridge's order_send-zero guarantee is proven by ShadowOrderCheckBridgeTests.
        import ast
        import pathlib
        src = pathlib.Path(self.worker.__file__).read_text()
        self.assertNotIn("MetaTrader5", src)
        # AST: no attribute access named order_send anywhere in code.
        attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
        self.assertNotIn("order_send", attrs)
        self.assertIn("agent_order_check", src)


class ShadowPollGateTests(SimpleTestCase):
    """EXEC-E2b-R2: shadow-worker mode (MT5_SHADOW_WORKER) is SHADOW-ONLY.

    A dedicated shadow worker (flag ON) claims ONLY PLACE_ORDER_SHADOW — never
    the executable PLACE_TEST_ORDER/PLACE_ORDER types and never the default SYNC.
    That is structural: a shadow worker can never win a real order and route it
    to the live order_send path, and its poll rate is one claim/loop (well under
    the throttle). The normal worker (flag OFF, default) claims its executable
    sequence (PLACE_TEST_ORDER → PLACE_ORDER → MODIFY_POSITION → CLOSE_TRADE →
    default SYNC; the risk-reducing MODIFY_POSITION (WS-B breakeven) and CLOSE_TRADE
    (WS-E provider commands) rank ahead of the default SYNC) and never claims a shadow job.
    """

    def setUp(self):
        import mt5_trade_ingest_worker as worker
        self.worker = worker

    def _run_claim(self, shadow_enabled, responder=None):
        """Run claim_worker_job under the given flag; return (job, calls) where
        calls is the ordered list of job_type args passed to claim_next_job.
        ``responder(job_type)`` supplies the return value for each claim (default
        None -> nothing claimable)."""
        calls = []

        def fake_claim(job_type=None):
            calls.append(job_type)
            return responder(job_type) if responder else None

        with mock.patch.object(self.worker, "claim_next_job", side_effect=fake_claim), \
             mock.patch.object(self.worker, "SHADOW_WORKER_ENABLED", shadow_enabled):
            job = self.worker.claim_worker_job()
        return job, calls

    # --- normal mode (flag OFF, default) -----------------------------------
    def test_normal_mode_claim_sequence_unchanged(self):
        # Req 1/6: executable claim sequence (now incl. the risk-reducing MODIFY_POSITION),
        # no shadow claim.
        job, calls = self._run_claim(shadow_enabled=False)
        self.assertIsNone(job)
        self.assertEqual(calls, ["PLACE_TEST_ORDER", "PLACE_ORDER", "MODIFY_POSITION", "CLOSE_TRADE", None])
        self.assertNotIn("PLACE_ORDER_SHADOW", calls)

    def test_normal_mode_short_circuits(self):
        # If PLACE_TEST_ORDER yields a job, no further claims are made.
        job, calls = self._run_claim(
            shadow_enabled=False,
            responder=lambda jt: {"id": 1} if jt == "PLACE_TEST_ORDER" else None,
        )
        self.assertEqual(job, {"id": 1})
        self.assertEqual(calls, ["PLACE_TEST_ORDER"])

    # --- shadow mode (flag ON) ---------------------------------------------
    def test_shadow_mode_claims_only_shadow(self):
        # Req 2: the ONLY claim a shadow worker makes is PLACE_ORDER_SHADOW.
        job, calls = self._run_claim(shadow_enabled=True)
        self.assertIsNone(job)
        self.assertEqual(calls, ["PLACE_ORDER_SHADOW"])

    def test_shadow_mode_never_claims_executable_or_sync(self):
        # Req 3/4/5: never PLACE_TEST_ORDER, never PLACE_ORDER, never default sync.
        _, calls = self._run_claim(shadow_enabled=True)
        self.assertNotIn("PLACE_TEST_ORDER", calls)
        self.assertNotIn("PLACE_ORDER", calls)
        self.assertNotIn(None, calls)  # None == the default SYNC_POSITIONS claim

    def test_shadow_mode_returns_claimed_shadow_job(self):
        # Shadow mode short-circuit: the single shadow claim's job is returned,
        # and exactly one claim is made.
        job, calls = self._run_claim(
            shadow_enabled=True,
            responder=lambda jt: {"id": 7, "job_type": "PLACE_ORDER_SHADOW"}
            if jt == "PLACE_ORDER_SHADOW" else None,
        )
        self.assertEqual(job.get("id"), 7)
        self.assertEqual(calls, ["PLACE_ORDER_SHADOW"])

    def test_shadow_mode_single_claim_per_loop(self):
        # Req 7 (poll rate): shadow mode makes exactly ONE API claim per loop →
        # ~30/min at the default 2s sleep, well under the 100/min throttle.
        _, calls = self._run_claim(shadow_enabled=True)
        self.assertEqual(len(calls), 1)

    # --- env-flag parsing ---------------------------------------------------
    def test_flag_default_off_when_env_absent(self):
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("MT5_SHADOW_WORKER", None)
            self.assertFalse(self.worker._env_truthy("MT5_SHADOW_WORKER"))

    def test_flag_truthy_values_enable(self):
        for v in ("1", "true", "TRUE", "yes", "on", " on "):
            with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER": v}):
                self.assertTrue(self.worker._env_truthy("MT5_SHADOW_WORKER"), v)

    def test_flag_falsey_values_disable(self):
        for v in ("", "0", "false", "no", "off", "disabled"):
            with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER": v}):
                self.assertFalse(self.worker._env_truthy("MT5_SHADOW_WORKER"), v)

    def test_module_global_off_by_default_and_on_with_env(self):
        # Requirement 1 at the MODULE level: SHADOW_WORKER_ENABLED is computed
        # once at import from MT5_SHADOW_WORKER. Re-exec the module under a
        # controlled environment and assert the ACTUAL global (not just the
        # helper), so a "default ON" regression is caught even if the ambient
        # test/CI environment happens to have the var set. Restore afterwards.
        import importlib
        try:
            with mock.patch.dict(os.environ, clear=False):
                os.environ.pop("MT5_SHADOW_WORKER", None)
                reloaded = importlib.reload(self.worker)
                self.assertFalse(reloaded.SHADOW_WORKER_ENABLED)  # default OFF
            with mock.patch.dict(os.environ, {"MT5_SHADOW_WORKER": "1"}):
                reloaded = importlib.reload(self.worker)
                self.assertTrue(reloaded.SHADOW_WORKER_ENABLED)  # opt-in ON
        finally:
            importlib.reload(self.worker)  # restore module global to ambient env
