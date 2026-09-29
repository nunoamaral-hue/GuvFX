"""P0 bridge resilience: DARK-by-default ThreadingHTTPServer + serialized MT5 access + non-blocking /health.

Root cause (2026-09-29): the single-threaded HTTPServer let a wedged MT5 call head-of-line-block /health, so the
tenant watchdog's liveness probe timed out and a wedged bridge looked dead. Fix (opt-in MT5_BRIDGE_THREADED):
ThreadingHTTPServer + one reentrant lock serializing MT5 (timeout-acquire on HTTP paths => 503 mt5_busy on a wedge),
with /health NEVER taking the lock. These tests pin: DARK-by-default (no lock, byte-identical), the timeout->busy
contract, blocking serialization for the order path, and that /health never blocks. No MT5/network is touched.
"""
import importlib.util
import os
import tempfile
import threading
import time
from unittest import mock

from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BRIDGE_PATH = os.path.join(_REPO, "scripts", "mt5_signal_bridge.py")


def _load_bridge(**env):
    """Import a FRESH bridge module under a controlled environment (flags are read at import time). Loaded from a
    temp cwd because the bridge builds a module-scope FileHandler."""
    # A fake, obviously-non-secret token so the bridge imports with auth configured. The value carries the "fake"
    # placeholder marker so the repo secret scanner (check_no_secrets.py) recognises it as a non-credential; the
    # tests stub _validate_token anyway, so the actual value is never used to authenticate.
    base = {"GUVFX_AGENT_TOKEN": "fake-token-not-a-secret-threading-tests"}
    base.update(env)
    prev = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            with mock.patch.dict(os.environ, base, clear=False):
                spec = importlib.util.spec_from_file_location("mt5_signal_bridge_threading_under_test", _BRIDGE_PATH)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
        finally:
            os.chdir(prev)
    return mod


class BridgeThreadingDarkDefault(SimpleTestCase):
    def test_threaded_disabled_by_default(self):
        mod = _load_bridge()
        self.assertFalse(mod.bridge_threaded_enabled(), "MT5_BRIDGE_THREADED must be OFF by default (DARK)")

    def test_threaded_enabled_when_flag_set(self):
        for v in ("1", "true", "YES", "on"):
            mod = _load_bridge(MT5_BRIDGE_THREADED=v)
            self.assertTrue(mod.bridge_threaded_enabled(), f"flag value {v!r} must enable threaded mode")

    def test_serialize_is_noop_when_dark(self):
        """With threaded mode OFF, _mt5_serialized never acquires the lock: even while another thread 'holds' it,
        a block=False context enters immediately and never raises Mt5Busy (byte-identical single-thread behaviour)."""
        mod = _load_bridge()  # DARK
        held = threading.Event()
        release = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire()
            held.set()
            release.wait(2)
            mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(held.wait(2))
        try:
            entered = False
            with mod._mt5_serialized(block=False):
                entered = True
            self.assertTrue(entered, "DARK mode must enter without acquiring the lock")
        finally:
            release.set(); t.join(2)


class BridgeThreadingSerialization(SimpleTestCase):
    def test_block_false_raises_busy_on_contention(self):
        """Threaded mode: if another thread holds the MT5 lock longer than the per-call timeout, a block=False
        acquire must raise Mt5Busy (mapped to 503 mt5_busy by the handler) instead of hanging."""
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="0.3")
        held = threading.Event()
        release = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire()
            held.set()
            release.wait(3)
            mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(held.wait(2))
        try:
            t0 = time.time()
            with self.assertRaises(mod.Mt5Busy):
                with mod._mt5_serialized(block=False):
                    pass
            dt = time.time() - t0
            self.assertGreaterEqual(dt, 0.25, "must wait ~the timeout before declaring busy")
            self.assertLess(dt, 2.0, "must fail fast (not hang) once the timeout elapses")
        finally:
            release.set(); t.join(3)

    def test_block_true_serializes_and_does_not_raise(self):
        """The order/poll path (block=True) waits for the lock and never raises busy; once the holder releases,
        it acquires and records liveness."""
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="0.3")
        release = threading.Event()
        acquired = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire()
            release.wait(1.0)          # hold briefly, then release
            mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        time.sleep(0.1)                 # ensure holder has the lock first

        def waiter():
            with mod._mt5_serialized(block=True):
                acquired.set()

        w = threading.Thread(target=waiter, daemon=True); w.start()
        time.sleep(0.2)
        self.assertFalse(acquired.is_set(), "block=True must WAIT while the lock is held")
        release.set()
        self.assertTrue(acquired.wait(3), "block=True must acquire once the holder releases (no busy raise)")
        t.join(2); w.join(2)
        self.assertGreater(mod._MT5_LAST_OK["ts"], 0, "a clean serialized op must stamp liveness")


class BridgeCallTimeoutParsing(SimpleTestCase):
    """The per-call timeout is a tuning knob, not a credential: a malformed/non-positive value must fall back to
    the 8s default instead of crashing the bridge at import (a fat-finger during arming must not brick startup)."""
    def test_valid_positive_timeout_used(self):
        self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS="0.5")._MT5_CALL_TIMEOUT, 0.5)

    def test_malformed_timeout_falls_back_to_default(self):
        self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS="abc")._MT5_CALL_TIMEOUT, 8.0)

    def test_nonpositive_timeout_falls_back_to_default(self):
        self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS="0")._MT5_CALL_TIMEOUT, 8.0)
        self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS="-3")._MT5_CALL_TIMEOUT, 8.0)

    def test_nonfinite_timeout_falls_back_to_default(self):
        """inf/nan must NOT pass through: RLock.acquire(timeout=inf) raises OverflowError at call time (a 500, not
        the intended fast 503). Both must fall back to the 8s default; a huge finite value is clamped."""
        for bad in ("inf", "Infinity", "1e999", "nan", "-inf"):
            self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS=bad)._MT5_CALL_TIMEOUT, 8.0, f"{bad!r}")
        self.assertEqual(_load_bridge(MT5_CALL_TIMEOUT_SECONDS="100000")._MT5_CALL_TIMEOUT, 300.0)


def _make_handler(mod):
    """Build an OHLCRequestHandler WITHOUT running BaseHTTPRequestHandler.__init__ (no socket), with auth stubbed
    open and responses recorded, so the real do_GET/do_POST dispatch + _mt5_serialized wiring can be exercised."""
    h = object.__new__(mod.OHLCRequestHandler)
    h.responses = []
    h._validate_token = lambda: True
    h._send_json_response = lambda data, status_code=200: h.responses.append((status_code, data))
    return h


class BridgeHandlerWiring(SimpleTestCase):
    """Pin the ACTUAL handler dispatch, not just the primitive: mutating POST routes WAIT (block=True) so a live
    order is never bounced by transient snapshot contention, while read-only GET data routes fail-fast (503)."""

    def test_mutating_post_waits_for_lock_and_does_not_503(self):
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="0.3")
        h = _make_handler(mod)
        h.path = "/mt5/close-position"
        ran = threading.Event()
        h._handle_close_position_request = lambda: (ran.set(), h._send_json_response({"ok": True}))[1]

        release = threading.Event()
        holder_has_lock = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire(); holder_has_lock.set(); release.wait(3); mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(holder_has_lock.wait(2))

        p = threading.Thread(target=h.do_POST, daemon=True); p.start()
        # While the lock is held, the mutating POST must WAIT well past the 0.3s call-timeout (proving block=True,
        # not a fast 503): the route handler must NOT have run and no 503 recorded.
        time.sleep(0.6)
        self.assertFalse(ran.is_set(), "block=True mutating POST must WAIT for the lock, not fail fast")
        self.assertFalse(any(s == 503 for s, _ in h.responses), "a live order POST must never 503 on contention")

        release.set()
        self.assertTrue(ran.wait(3), "mutating POST must run once the lock is released")
        p.join(2); t.join(2)
        self.assertIn((200, {"ok": True}), h.responses)
        self.assertFalse(any(s == 503 for s, _ in h.responses))

    def test_readonly_get_data_route_503s_on_wedge(self):
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="0.3")
        h = _make_handler(mod)
        h.path = "/mt5/positions"        # read-only snapshot route -> block=False -> fast 503 on a held lock
        release = threading.Event()
        holder_has_lock = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire(); holder_has_lock.set(); release.wait(3); mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(holder_has_lock.wait(2))
        try:
            t0 = time.time()
            h.do_GET()                    # Mt5Busy raised in __enter__ (lock held past timeout) -> 503, no MT5 call
            dt = time.time() - t0
            self.assertLess(dt, 2.0, "read-only GET must fail fast on a wedge, not hang")
            self.assertIn((503, {"ok": False, "error": "mt5_busy"}), h.responses)
        finally:
            release.set(); t.join(3)

    def test_dark_mode_dispatch_is_true_noop_end_to_end(self):
        """DARK (flag OFF): do_POST/do_GET must dispatch EXACTLY as before - the _mt5_serialized wrapper acquires
        nothing, so a route runs even while another thread 'holds' _MT5_LOCK, and /health returns ok=true. This
        proves the flag-off path is behaviourally identical through the real handler dispatch, not just the CM."""
        mod = _load_bridge()  # DARK
        # A mutating POST runs immediately even though _MT5_LOCK is held by another thread (no serialization).
        h = _make_handler(mod)
        h.path = "/mt5/order"
        ran = threading.Event()
        h._handle_order_request = lambda: (ran.set(), h._send_json_response({"ok": True}))[1]
        held = threading.Event(); release = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire(); held.set(); release.wait(2); mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(held.wait(2))
        try:
            h.do_POST()
            self.assertTrue(ran.is_set(), "DARK do_POST must run the route without acquiring the MT5 lock")
            self.assertIn((200, {"ok": True}), h.responses)
        finally:
            release.set(); t.join(2)

        # /health in DARK still returns ok=true (with additive keys) and threaded=false.
        h2 = _make_handler(mod); h2.path = "/health"; h2.do_GET()
        self.assertEqual(h2.responses[0][0], 200)
        self.assertIs(h2.responses[0][1]["ok"], True)
        self.assertFalse(h2.responses[0][1]["threaded"])

    def test_health_get_served_lockfree_while_held(self):
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="5")
        h = _make_handler(mod)
        h.path = "/health"
        release = threading.Event()
        holder_has_lock = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire(); holder_has_lock.set(); release.wait(3); mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(holder_has_lock.wait(2))
        try:
            t0 = time.time()
            h.do_GET()                    # /health must answer instantly even while the MT5 lock is held
            dt = time.time() - t0
            self.assertLess(dt, 0.5, "/health must never take the MT5 lock")
            self.assertEqual(len(h.responses), 1)
            status, body = h.responses[0]
            self.assertEqual(status, 200)
            self.assertIs(body["ok"], True)
            self.assertTrue(body["threaded"])
        finally:
            release.set(); t.join(3)


class BridgeHealthNeverBlocks(SimpleTestCase):
    def test_health_payload_ok_and_lockfree_even_while_mt5_held(self):
        """/health must answer instantly and ok=True even while the MT5 lock is held by a (wedged) call -> the
        watchdog treats a RESPONSIVE bridge as healthy; a true wedge is /health not answering at all."""
        mod = _load_bridge(MT5_BRIDGE_THREADED="1", MT5_CALL_TIMEOUT_SECONDS="5")

        class _H:  # minimal stand-in exposing the handler method
            _health_payload = mod.OHLCRequestHandler._health_payload

        holder_has_lock = threading.Event()
        release = threading.Event()

        def holder():
            mod._MT5_LOCK.acquire()
            holder_has_lock.set()
            release.wait(3)
            mod._MT5_LOCK.release()

        t = threading.Thread(target=holder, daemon=True); t.start()
        self.assertTrue(holder_has_lock.wait(2))
        try:
            t0 = time.time()
            payload = _H()._health_payload()
            dt = time.time() - t0
            self.assertLess(dt, 0.5, "/health must not block on the MT5 lock")
            self.assertIs(payload["ok"], True)
            self.assertEqual(payload["status"], "healthy")
            self.assertTrue(payload["threaded"])
            self.assertIn("mt5_last_ok_age_s", payload)
        finally:
            release.set(); t.join(3)
