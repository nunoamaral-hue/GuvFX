"""ADR-0085 — DARK copier lifecycle-guard BRIDGE WIRING tests.

These pin the WIRING contract of the three guarded mutation entry points in ``scripts/mt5_signal_bridge.py``
(``guarded_close_position`` / ``guarded_modify_position`` / ``guarded_partial_close_position``) around the PURE
guard (``scripts/mt5_copier_lifecycle_guard.py``). The guard's own admit/classify semantics are pinned by
``execution.tests_copier_lifecycle_guard``; here we pin only the integration:

* flag OFF (or malformed) => the wrapper IS the money function, byte-identical, and the guard never runs;
* flag ON + a typed command => PRE-send admission can BLOCK the send (no money call), an idempotent post-state
  is a no-op success, PROCEED runs the UNCHANGED money function, and the result is re-classified;
* an unverifiable pre/post state or a guard-import failure FAILS CLOSED (never sends, never fabricates success);
* UNKNOWN_OUTCOME (indeterminate send) is never success; a terminal classification forces ok=False;
* the guard adds NO new order path and widens NO authority: on PROCEED it calls exactly the money function the
  un-guarded path calls, and the money function's own demo/live wall is untouched.

The bridge module is imported from source (mirroring ``execution.tests_bridge_binding``); MetaTrader5 is never
imported because every MT5 touch (the snapshot read + the money send) is monkeypatched.
"""
import importlib.util
import os
import tempfile
import time

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")

_SYNTH_AGENT = "synthetic-agent-" + "token"
_SYNTH_WORKER = "synthetic-worker-" + "token"
_GUARD_ENV = "COPIER_LIFECYCLE_GUARD_ENABLED"


def _load_bridge():
    """Import a fresh bridge module in a temp cwd (it builds a module-scope FileHandler)."""
    env = {"GUVFX_AGENT_TOKEN": _SYNTH_AGENT, "GUVFX_WORKER_TOKEN": _SYNTH_WORKER}
    prev = os.getcwd()
    prev_env = {k: os.environ.get(k) for k in env}
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        os.environ.update(env)
        try:
            spec = importlib.util.spec_from_file_location("mt5_signal_bridge_wiring_under_test", _BRIDGE_PATH)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        finally:
            os.chdir(prev)
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _cmd(op, **over):
    """A complete, admissible typed copier command for ``op`` (overridable)."""
    base = {
        "copier_operation": op,
        "ticket": 555,
        "symbol": "EURUSD",
        "master_revision": 10,
        "authorization_expires_at": time.time() + 3600,  # far-future epoch
        "require_identity_pin": True,
        "expected_login": "9001",
        "expected_server": "Broker-Demo",
        "expected_pre_state": {"symbol": "EURUSD"},
        "requested_post_state": {},
        "last_applied_revision": -1,
    }
    base.update(over)
    return base


def _snap(**over):
    """A matching follower snapshot (exists, identity + symbol agree with ``_cmd``)."""
    base = {"exists": True, "ticket": 555, "symbol": "EURUSD", "volume": 1.0, "sl": 1.10, "tp": 1.20,
            "login": "9001", "server": "Broker-Demo", "point": 0.0001, "volume_step": 0.01}
    base.update(over)
    return base


class _GuardFlag:
    """Context manager that sets/clears the DARK guard env flag deterministically."""

    def __init__(self, value):
        self._value = value

    def __enter__(self):
        self._prev = os.environ.get(_GUARD_ENV)
        if self._value is None:
            os.environ.pop(_GUARD_ENV, None)
        else:
            os.environ[_GUARD_ENV] = self._value

    def __exit__(self, *a):
        if self._prev is None:
            os.environ.pop(_GUARD_ENV, None)
        else:
            os.environ[_GUARD_ENV] = self._prev


class CopierBridgeWiringTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b = _load_bridge()

    _PATCHED = ("close_position", "modify_position", "partial_close_position",
                "_guard_follower_snapshot", "_copier_lifecycle_guard", "_GUARD_POST_SETTLE_SECONDS")

    def setUp(self):
        self.calls = []
        # Snapshot every attribute a test may monkeypatch, and restore it afterwards so no test poisons the
        # shared (setUpClass-loaded) module for the next one.
        self._orig = {n: getattr(self.b, n) for n in self._PATCHED}
        self.b._GUARD_POST_SETTLE_SECONDS = 0   # no real settle sleep in tests

    def tearDown(self):
        for n, v in self._orig.items():
            setattr(self.b, n, v)

    # ── helpers ────────────────────────────────────────────────────────────────────────────────────────
    def _stub_money(self, name, result):
        """Replace a money function with a recording stub returning ``result``."""
        def _stub(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return dict(result)
        setattr(self.b, name, _stub)

    def _stub_snapshots(self, *snaps):
        """Feed ``_guard_follower_snapshot`` a sequence of ``(ok, snap)`` returns (pre, post, ...)."""
        seq = list(snaps)

        def _reader(ticket):
            return seq.pop(0) if seq else (False, {})
        self.b._guard_follower_snapshot = _reader

    # ── flag OFF / malformed => byte-identical, guard never runs ─────────────────────────────────────────
    def test_flag_off_close_is_byte_identical(self):
        self._stub_money("close_position", {"ok": True, "closed": True})
        with _GuardFlag(None):
            out = self.b.guarded_close_position(555, identity={"expected_login": "x"}, command=_cmd("FULL_CLOSE"))
        self.assertEqual(out, {"ok": True, "closed": True})          # money result verbatim, no lifecycle fields
        self.assertNotIn("lifecycle_outcome", out)
        self.assertEqual(self.calls[0][0], "close_position")         # money fn called directly

    def test_malformed_flag_fails_closed_guard_inert(self):
        self._stub_money("modify_position", {"ok": True, "verified_sl": 1.1})
        with _GuardFlag("maybe"):   # not a truthy token
            out = self.b.guarded_modify_position(555, 1.1, 1.2, command=_cmd("MODIFY_PROTECTION"))
        self.assertNotIn("lifecycle_outcome", out)                   # guard did NOT run
        self.assertEqual(self.calls[0][0], "modify_position")

    def test_no_command_is_byte_identical_even_when_flag_on(self):
        self._stub_money("partial_close_position", {"ok": True, "need_reconcile": True})
        with _GuardFlag("1"):
            out = self.b.guarded_partial_close_position(555, 0.5, expected_symbol="EURUSD", command=None)
        self.assertNotIn("lifecycle_outcome", out)
        self.assertEqual(self.calls[0][0], "partial_close_position")

    # ── flag ON: PROCEED -> money send -> classify ───────────────────────────────────────────────────────
    def test_full_close_proceed_then_applied(self):
        self._stub_money("close_position", {"ok": True, "closed": True})
        self._stub_snapshots((True, _snap()), (True, {"exists": False}))  # pre open, post gone
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                   requested_post_state={"closed": True}))
        self.assertEqual(out["lifecycle_outcome"], "APPLIED")
        self.assertTrue(out["ok"])
        self.assertEqual(self.calls[0][0], "close_position")              # money fn DID run

    def test_modify_proceed_then_applied(self):
        self._stub_money("modify_position", {"ok": True})
        self._stub_snapshots((True, _snap(sl=1.10, tp=1.20)),
                             (True, _snap(sl=1.15, tp=1.20)))             # post reflects requested SL
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, 1.20,
                                                 command=_cmd("MODIFY_PROTECTION",
                                                              requested_post_state={"sl": 1.15, "tp": 1.20}))
        self.assertEqual(out["lifecycle_outcome"], "APPLIED")
        self.assertTrue(out["ok"])

    # ── flag ON: terminal admission BLOCKS the send (no money call) ──────────────────────────────────────
    def test_deadline_exceeded_blocks_send(self):
        self._stub_money("close_position", {"ok": True})
        self._stub_snapshots((True, _snap()))
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                   authorization_expires_at=time.time() - 1))
        self.assertEqual(out["lifecycle_outcome"], "DEADLINE_EXCEEDED")
        self.assertFalse(out["ok"])
        self.assertTrue(out["guard_blocked"])
        self.assertEqual(self.calls, [])                                  # NOTHING sent

    def test_symbol_mismatch_blocks_send(self):
        self._stub_money("modify_position", {"ok": True})
        self._stub_snapshots((True, _snap(symbol="GBPUSD")))
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, command=_cmd("MODIFY_PROTECTION",
                                                 requested_post_state={"sl": 1.15, "tp": 1.2}))
        self.assertEqual(out["lifecycle_outcome"], "PRE_STATE_MISMATCH")
        self.assertEqual(self.calls, [])

    def test_identity_mismatch_blocks_send(self):
        self._stub_money("close_position", {"ok": True})
        self._stub_snapshots((True, _snap(login="OTHER")))
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE"))
        self.assertEqual(out["lifecycle_outcome"], "PRE_STATE_MISMATCH")
        self.assertEqual(self.calls, [])

    def test_out_of_order_revision_blocks_send(self):
        self._stub_money("modify_position", {"ok": True})
        self._stub_snapshots((True, _snap()))
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, command=_cmd(
                "MODIFY_PROTECTION", master_revision=5, last_applied_revision=9,
                requested_post_state={"sl": 1.15, "tp": 1.2}))
        self.assertEqual(out["lifecycle_outcome"], "STALE_REVISION")
        self.assertEqual(self.calls, [])

    def test_modify_missing_position_blocks_send(self):
        self._stub_money("modify_position", {"ok": True})
        self._stub_snapshots((True, {"exists": False}))
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, command=_cmd(
                "MODIFY_PROTECTION", requested_post_state={"sl": 1.15, "tp": 1.2}))
        self.assertEqual(out["lifecycle_outcome"], "POSITION_NOT_FOUND")
        self.assertEqual(self.calls, [])

    # ── idempotent post-state => no-op success, no send ─────────────────────────────────────────────────
    def test_already_applied_is_noop_no_send(self):
        self._stub_money("modify_position", {"ok": True})
        self._stub_snapshots((True, _snap(sl=1.15, tp=1.20)))             # already at requested SL/TP
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, 1.20, command=_cmd(
                "MODIFY_PROTECTION", requested_post_state={"sl": 1.15, "tp": 1.20}))
        self.assertEqual(out["lifecycle_outcome"], "ALREADY_APPLIED")
        self.assertTrue(out["ok"])
        self.assertEqual(self.calls, [])                                  # idempotent -> no send

    # ── fail-closed: unverifiable pre-state / guard import failure ───────────────────────────────────────
    def test_pre_snapshot_unavailable_fails_closed(self):
        self._stub_money("close_position", {"ok": True})
        self._stub_snapshots((False, {}))                                 # read failed
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE"))
        self.assertEqual(out["lifecycle_outcome"], "CONTAINED_FAILURE")
        self.assertFalse(out["ok"])
        self.assertEqual(self.calls, [])

    def test_guard_import_failure_fails_closed(self):
        self._stub_money("close_position", {"ok": True})
        self.b._copier_lifecycle_guard = lambda: None                     # simulate import failure
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE"))
        self.assertEqual(out["lifecycle_outcome"], "CONTAINED_FAILURE")
        self.assertTrue(out["guard_blocked"])
        self.assertEqual(self.calls, [])

    # ── post-classify: UNKNOWN never success; terminal forces ok=False ──────────────────────────────────
    def test_indeterminate_send_is_unknown_not_success(self):
        # money reports a lost ACK (order_send_none) -> send not determinate -> UNKNOWN_OUTCOME.
        self._stub_money("close_position", {"ok": False, "error": "order_send_none"})
        self._stub_snapshots((True, _snap()), (True, _snap()))            # post still open
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                   requested_post_state={"closed": True}))
        self.assertEqual(out["lifecycle_outcome"], "UNKNOWN_OUTCOME")
        self.assertFalse(out["ok"])
        self.assertTrue(out["need_reconcile"])

    def test_close_not_reflected_forces_contained(self):
        # money claims ok but the position is STILL open post-send -> contained, ok forced False.
        self._stub_money("close_position", {"ok": True, "closed": True})
        self._stub_snapshots((True, _snap()), (True, _snap()))            # post still open
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                   requested_post_state={"closed": True}))
        self.assertEqual(out["lifecycle_outcome"], "CONTAINED_FAILURE")
        self.assertFalse(out["ok"])

    def test_applied_reconciles_stale_residual_exposure(self):
        # A lost-ACK close exhausts attempts -> money reports {ok:False, residual_exposure:True, retcode:None},
        # but the authoritative post-read shows the position gone -> APPLIED. The merged result must be
        # self-consistent: ok upgraded to True, the stale residual_exposure marker cleared.
        self._stub_money("close_position", {"ok": False, "error": "close_failed_position_open",
                                            "residual_exposure": True, "retcode": None})
        self._stub_snapshots((True, _snap()), (True, {"exists": False}))   # post gone (lost-ACK close landed)
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                  requested_post_state={"closed": True}))
        self.assertEqual(out["lifecycle_outcome"], "APPLIED")
        self.assertTrue(out["ok"])                      # upgraded on authoritative confirmation
        self.assertNotIn("residual_exposure", out)      # stale marker cleared

    def test_modify_broker_rejected_is_rejected(self):
        self._stub_money("modify_position", {"ok": False, "error": "modify_rejected", "retcode": 10016})
        self._stub_snapshots((True, _snap(sl=1.10)), (True, _snap(sl=1.10)))
        with _GuardFlag("1"):
            out = self.b.guarded_modify_position(555, 1.15, 1.2, command=_cmd(
                "MODIFY_PROTECTION", requested_post_state={"sl": 1.15, "tp": 1.2}))
        self.assertEqual(out["lifecycle_outcome"], "BROKER_REJECTED")
        self.assertFalse(out["ok"])

    def test_post_snapshot_unreadable_is_unknown(self):
        self._stub_money("close_position", {"ok": True, "closed": True})
        self._stub_snapshots((True, _snap()), (False, {}))                # post read failed
        with _GuardFlag("1"):
            out = self.b.guarded_close_position(555, command=_cmd("FULL_CLOSE",
                                                                   requested_post_state={"closed": True}))
        self.assertEqual(out["lifecycle_outcome"], "UNKNOWN_OUTCOME")

    # ── partial close: strictly-reducing admission + reduced classification ─────────────────────────────
    def test_partial_proceed_then_reduced(self):
        self._stub_money("partial_close_position", {"ok": True, "need_reconcile": True})
        self._stub_snapshots((True, _snap(volume=1.0)), (True, _snap(volume=0.4)))
        with _GuardFlag("1"):
            out = self.b.guarded_partial_close_position(555, 0.6, expected_symbol="EURUSD", command=_cmd(
                "PARTIAL_CLOSE",
                expected_pre_state={"symbol": "EURUSD", "expected_current_volume": "1.0"},
                requested_post_state={"reduce_by": "0.6", "expected_remaining": "0.4"}))
        self.assertEqual(out["lifecycle_outcome"], "APPLIED")
        self.assertEqual(self.calls[0][0], "partial_close_position")

    def test_partial_over_close_blocks_send(self):
        self._stub_money("partial_close_position", {"ok": True})
        self._stub_snapshots((True, _snap(volume=0.5)))                   # reduce_by >= current
        with _GuardFlag("1"):
            out = self.b.guarded_partial_close_position(555, 0.6, expected_symbol="EURUSD", command=_cmd(
                "PARTIAL_CLOSE",
                expected_pre_state={"symbol": "EURUSD", "expected_current_volume": "0.5"},
                requested_post_state={"reduce_by": "0.6", "expected_remaining": "-0.1"}))
        self.assertEqual(out["lifecycle_outcome"], "PRE_STATE_MISMATCH")
        self.assertEqual(self.calls, [])

    # ── command extraction / no-widening ────────────────────────────────────────────────────────────────
    def test_command_from_body_only_for_typed_copier_ops(self):
        self.assertIsNone(self.b._copier_command_from_body({"ticket": 1}))          # a plain demo close body
        self.assertIsNone(self.b._copier_command_from_body({"copier_operation": "PLACE_ORDER"}))
        self.assertIsNone(self.b._copier_command_from_body("not a dict"))
        cmd = self.b._copier_command_from_body({"copier_operation": "FULL_CLOSE", "ticket": 9})
        self.assertEqual(cmd["copier_operation"], "FULL_CLOSE")

    def test_proceed_calls_exactly_the_money_function_no_new_path(self):
        # No-widening: on PROCEED the wrapper calls the SAME money function the un-guarded path calls, with the
        # same (ticket, identity) — it never reaches a PLACE_ORDER / open path.
        self._stub_money("close_position", {"ok": True, "closed": True})
        self._stub_snapshots((True, _snap()), (True, {"exists": False}))
        ident = {"require_identity_pin": True, "expected_login": "9001", "expected_server": "Broker-Demo"}
        with _GuardFlag("1"):
            self.b.guarded_close_position(555, identity=ident, command=_cmd("FULL_CLOSE",
                                          requested_post_state={"closed": True}))
        self.assertEqual(len(self.calls), 1)
        name, args, kwargs = self.calls[0]
        self.assertEqual(name, "close_position")                 # same money function, no new path
        self.assertEqual(args, (555,))
        self.assertEqual(kwargs["identity"], ident)
        # The command is also threaded to the money function (consulted only by the ADR-0087 route-pinned
        # live admission; absent/OFF it is inert) — the only addition beyond identity.
        self.assertEqual(kwargs["command"]["copier_operation"], "FULL_CLOSE")
        self.assertEqual(set(kwargs), {"identity", "command"})

    def test_send_signals_conservative(self):
        # An indeterminate error => not determinate; a definite reject => broker_rejected.
        self.assertEqual(self.b._guard_send_signals({"error": "order_send_none"}), (False, False))
        self.assertEqual(self.b._guard_send_signals({"error": "modify_rejected"}), (True, True))
        self.assertEqual(self.b._guard_send_signals({"ok": True}), (True, False))
        self.assertEqual(self.b._guard_send_signals(None), (False, False))
