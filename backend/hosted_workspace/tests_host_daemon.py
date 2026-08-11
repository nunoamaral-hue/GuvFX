"""Stream 7C - tests for the hosted executor daemon: RULE-3 config, the dispatch handler (signed/CZ/replay/
skew/unknown-key), the HTTP listener (routes/health/oversize), and the stop drain."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
for _p in (_BUNDLE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import daemon as hxd  # noqa: E402
import daemon_config as cfgmod  # noqa: E402
from nonce_store import SqliteNonceStore  # noqa: E402
from hosted_workspace.host_protocol import sign_hosted_request, verify_hosted_response, HostProtocolError  # noqa: E402

_NOW = 1_760_000_000
_KEYRING = {"hx-1": "unit-test-hmac-secret-value-01"}


class FakeRunner:
    def __init__(self, delay=0.0):
        self.delay = delay

    def run(self, primitive, args):
        if self.delay:
            time.sleep(self.delay)
        return {"ok": True, "primitive": primitive}


def _cfg(**over):
    base = {
        "bind_host": "127.0.0.1", "expected_bind_host": "127.0.0.1", "bind_port": 0,
        "keyring": _KEYRING, "key_id": "hx-1", "max_skew_seconds": 30, "reserved_account_ids": None,
        "drain_timeout_s": 5.0, "max_body_bytes": 4096, "request_timeout_s": 5.0, "max_connections": 4,
        "log_dir": None,
    }
    base.update(over)
    return base


def _signed(operation="VERIFY_SLOT", account_id=14, nonce="n-1", corr="c-1", now=_NOW):
    return sign_hosted_request(account_id=account_id, operation=operation, correlation_id=corr,
                               keyring=_KEYRING, key_id="hx-1", now=now, nonce=nonce)


# ── RULE-3 config ──────────────────────────────────────────────────────────────────────────────────────────
class ConfigTests(unittest.TestCase):
    def _env(self, **over):
        env = {
            "HOSTED_EXECUTOR_BIND_HOST": "100.79.101.19",
            "HOSTED_EXECUTOR_KEYRING": '{"hx-1":"unit-test-hmac-secret-value-01"}',
            "HOSTED_EXECUTOR_KEY_ID": "hx-1",
            "HOSTED_EXECUTOR_ENC_PRIVKEYS": '{"enc-1":"c29tZS1iNjQta2V5"}',
        }
        env.update(over)
        return env

    def test_happy_path(self):
        cfg = cfgmod.load_config(self._env())
        self.assertEqual(cfg["bind_port"], 8790)
        self.assertEqual(cfg["key_id"], "hx-1")
        self.assertIn("hx-1", cfg["keyring"])

    def test_missing_keyring_fails_closed(self):
        env = self._env(); del env["HOSTED_EXECUTOR_KEYRING"]
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(env)

    def test_placeholder_secret_fails_closed(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_KEYRING='{"hx-1":"changeme"}'))

    def test_key_id_not_in_keyring_fails_closed(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_KEY_ID="nope"))

    def test_missing_enc_privkeys_fails_closed(self):
        env = self._env(); del env["HOSTED_EXECUTOR_ENC_PRIVKEYS"]
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(env)

    def test_placeholder_enc_privkey_fails_closed_at_boot(self):
        # symmetric with the HMAC keyring: an un-substituted install placeholder is caught at boot, not at
        # the first PROVISION_IDENTITY request
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_ENC_PRIVKEYS='{"enc-1":"__set_at_install__"}'))

    def test_forbidden_port_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_BIND_PORT="8791"))   # the beta agent's port

    def test_wildcard_bind_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_BIND_HOST="0.0.0.0",
                                         HOSTED_EXECUTOR_EXPECTED_BIND_HOST="0.0.0.0"))

    def test_non_expected_interface_refused(self):
        with self.assertRaises(cfgmod.ConfigError):
            cfgmod.load_config(self._env(HOSTED_EXECUTOR_BIND_HOST="127.0.0.1"))   # private but != expected


# ── dispatch handler ───────────────────────────────────────────────────────────────────────────────────────
class HandlerTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.nonce = SqliteNonceStore(os.path.join(self._dir, "n.sqlite"))
        self.handle = hxd.build_dispatch_handler(
            _cfg(), nonce_store=self.nonce, runner=FakeRunner(), envelope_opener=None, clock=lambda: _NOW)

    def tearDown(self):
        import shutil
        self.nonce.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_signed_request_returns_verified_signed_response(self):
        code, body = self.handle(_signed())
        self.assertEqual(code, 200)
        result = verify_hosted_response(body, correlation_id="c-1", nonce="n-1", keyring=_KEYRING)
        self.assertTrue(result["ok"])

    def test_customer_zero_refused(self):
        code, body = self.handle(_signed(account_id=1, nonce="cz"))
        self.assertEqual(code, 200)
        self.assertEqual(body.get("reason_code"), "reserved_identity")
        with self.assertRaises(HostProtocolError):     # a denial is unsigned -> backend fails closed
            verify_hosted_response(body, correlation_id="c-1", nonce="cz", keyring=_KEYRING)

    def test_replay_refused(self):
        req = _signed(nonce="dup")
        self.assertEqual(self.handle(req)[0], 200)                       # first burn ok
        _, body = self.handle(req)                                       # replay
        self.assertEqual(body.get("reason_code"), "nonce_replayed")

    def test_bad_signature_refused(self):
        req = _signed(nonce="sig")
        req["signature"] = "0" * 64
        self.assertEqual(self.handle(req)[1].get("reason_code"), "bad_signature")

    def test_timestamp_skew_refused(self):
        req = _signed(nonce="skew", now=_NOW - 10_000)                   # far from the handler's clock
        self.assertEqual(self.handle(req)[1].get("reason_code"), "timestamp_skew")

    def test_unknown_key_id_refused(self):
        req = sign_hosted_request(account_id=14, operation="VERIFY_SLOT", correlation_id="c",
                                  keyring={"other": "k2"}, key_id="other", now=_NOW, nonce="uk")
        self.assertEqual(self.handle(req)[1].get("reason_code"), "unknown_key_id")

    def test_configured_skew_is_enforced(self):
        # a tightened HOSTED_EXECUTOR_MAX_SKEW_SECONDS actually narrows the accept window (not dead config)
        tight = hxd.build_dispatch_handler(
            _cfg(max_skew_seconds=2), nonce_store=self.nonce, runner=FakeRunner(),
            envelope_opener=None, clock=lambda: _NOW)
        req = _signed(nonce="tightskew", now=_NOW + 5)          # 5s off, outside the 2s window
        self.assertEqual(tight(req)[1].get("reason_code"), "timestamp_skew")

    def test_default_skew_accepts_within_30s(self):
        req = _signed(nonce="okskew", now=_NOW + 5)             # 5s off, inside the default 30s window
        self.assertEqual(self.handle(req)[0], 200)

    def test_reserved_empty_string_still_refuses_customer_zero(self):
        # the daemon's Customer-Zero floor {1} is UNCONDITIONAL: an empty/whitespace reserved config can only
        # ADD to the floor, never remove it (the dispatch-level test-only opt-out is not reachable here)
        for raw in ("", "   ", "\t\n"):
            handle = hxd.build_dispatch_handler(
                _cfg(reserved_account_ids=raw), nonce_store=self.nonce, runner=FakeRunner(),
                envelope_opener=None, clock=lambda: _NOW)
            _, body = handle(_signed(account_id=1, nonce=f"cz-{len(raw)}-{raw!r}"))
            self.assertEqual(body.get("reason_code"), "reserved_identity", f"empty reserved {raw!r} must keep CZ floor")


# ── HTTP listener ──────────────────────────────────────────────────────────────────────────────────────────
class HttpTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.nonce = SqliteNonceStore(os.path.join(self._dir, "n.sqlite"))
        handle = hxd.build_dispatch_handler(
            _cfg(), nonce_store=self.nonce, runner=FakeRunner(), envelope_opener=None, clock=lambda: _NOW)
        self.httpd = hxd.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), hxd.make_handler(handle, max_body_bytes=2048),
            request_timeout_s=5, max_connections=4)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        import shutil
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.nonce.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _post(self, path, body_bytes):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", path, body=body_bytes, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_provision_route_round_trip(self):
        status, data = self._post("/hosted/provision", json.dumps(_signed(nonce="http")).encode())
        self.assertEqual(status, 200)
        result = verify_hosted_response(json.loads(data), correlation_id="c-1", nonce="http", keyring=_KEYRING)
        self.assertTrue(result["ok"])

    def test_unknown_route_404(self):
        status, _ = self._post("/nope", b"{}")
        self.assertEqual(status, 404)

    def test_oversize_body_413(self):
        status, _ = self._post("/hosted/provision", b"x" * 5000)     # > max_body_bytes 2048
        self.assertEqual(status, 413)

    def test_malformed_json_400(self):
        status, _ = self._post("/hosted/provision", b"{not json")
        self.assertEqual(status, 400)

    def test_health_ok(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/hosted/health")
        r = conn.getresponse()
        body = json.loads(r.read())
        conn.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(body["status"], "ok")


# ── drain on stop ──────────────────────────────────────────────────────────────────────────────────────────
class DrainTests(unittest.TestCase):
    def test_inflight_clears_and_sets_drained(self):
        d = tempfile.mkdtemp()
        try:
            nonce = SqliteNonceStore(os.path.join(d, "n.sqlite"))
            server = hxd.DaemonServer(_cfg(), nonce_store=nonce, runner=FakeRunner(delay=0.3),
                                      envelope_opener=(lambda payload, **k: b"pw"), clock=lambda: _NOW)
            self.assertTrue(server._drained.is_set())                # initially drained
            out = []

            def call():
                out.append(server._handle(_signed(nonce="drain")))

            th = threading.Thread(target=call)
            th.start()
            time.sleep(0.1)
            self.assertFalse(server._drained.is_set())               # cleared while in-flight
            th.join()
            self.assertTrue(server._drained.is_set())                # set again after completion
            self.assertEqual(out[0][0], 200)
            nonce.close()
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class SlowlorisTests(unittest.TestCase):
    """A trickle connection must be force-closed by the wall-clock read-deadline watchdog so it cannot hold a
    connection permit indefinitely and exhaust the (deliberately small) cap pre-auth."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.nonce = SqliteNonceStore(os.path.join(self._dir, "n.sqlite"))
        handle = hxd.build_dispatch_handler(
            _cfg(), nonce_store=self.nonce, runner=FakeRunner(), envelope_opener=None, clock=lambda: _NOW)
        self.httpd = hxd.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), hxd.make_handler(handle, max_body_bytes=2048),
            request_timeout_s=0.5, max_connections=1)        # tiny cap + short deadline make the exhaustion fast
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        import shutil
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.nonce.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_trickle_is_force_closed_and_permit_released(self):
        import socket
        stop = threading.Event()

        def dribble():
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                s.sendall(b"POST /hosted/provision HTTP/1.1\r\nHost: x\r\n")   # headers never terminated
                while not stop.is_set():
                    s.sendall(b"X")                    # one byte < the 0.5s per-recv timeout: never completes
                    time.sleep(0.15)
            except OSError:
                pass                                    # the watchdog force-closes us; expected

        t = threading.Thread(target=dribble, daemon=True)
        t.start()
        time.sleep(1.2)                                 # > request_timeout_s (0.5s): the watchdog has force-closed
        # cap is 1; if the trickle still held the permit, this legit request would be refused (connection reset).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/hosted/provision", body=json.dumps(_signed(nonce="afterloris")).encode(),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        stop.set()
        t.join(timeout=2)
        self.assertEqual(r.status, 200)
        result = verify_hosted_response(json.loads(data), correlation_id="c-1", nonce="afterloris", keyring=_KEYRING)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
