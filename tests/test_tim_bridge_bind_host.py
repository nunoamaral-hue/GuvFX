"""Tests for the explicit, fail-closed HTTP bind host + HTTP-only mode added to
scripts/mt5_signal_bridge.py (GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121).

Standard-library unittest only. The bridge's only non-stdlib top-level imports (requests /
urllib3) are mocked so it loads in the dependency-free governance CI; MetaTrader5 is imported
lazily inside a function and is never triggered here. No network, no MT5, no credentials.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest import mock

for _m in ("requests", "requests.adapters", "urllib3", "urllib3.util", "urllib3.util.retry"):
    sys.modules.setdefault(_m, mock.MagicMock())

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_PATH = os.path.join(_HERE, "..", "scripts", "mt5_signal_bridge.py")

_spec = importlib.util.spec_from_file_location("mt5_signal_bridge_bindhost_uut", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
_cwd = os.getcwd()
_tmp = tempfile.mkdtemp()
os.chdir(_tmp)  # the module creates mt5_signal_bridge.log in cwd at import time
try:
    _spec.loader.exec_module(bridge)
finally:
    os.chdir(_cwd)


class _Env:
    """Set/clear BRIDGE_BIND_HOST (or any var) for one test, restoring afterwards."""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class TestResolveBindHostDefault(unittest.TestCase):
    def test_default_is_exactly_0_0_0_0(self):
        with _Env(BRIDGE_BIND_HOST=None):
            self.assertEqual(bridge.resolve_bind_host(), "0.0.0.0")

    def test_explicit_0_0_0_0(self):
        with _Env(BRIDGE_BIND_HOST="0.0.0.0"):
            self.assertEqual(bridge.resolve_bind_host(), "0.0.0.0")


class TestResolveBindHostLoopback(unittest.TestCase):
    def test_loopback_ipv4(self):
        with _Env(BRIDGE_BIND_HOST="127.0.0.1"):
            self.assertEqual(bridge.resolve_bind_host(), "127.0.0.1")

    def test_loopback_ipv6(self):
        with _Env(BRIDGE_BIND_HOST="::1"):
            self.assertEqual(bridge.resolve_bind_host(), "::1")

    def test_whitespace_is_stripped(self):
        with _Env(BRIDGE_BIND_HOST="  127.0.0.1  "):
            self.assertEqual(bridge.resolve_bind_host(), "127.0.0.1")

    def test_loopback_socket_binds_loopback_only(self):
        with _Env(BRIDGE_BIND_HOST="127.0.0.1"):
            host = bridge.resolve_bind_host()
        srv = HTTPServer((host, 0), BaseHTTPRequestHandler)
        try:
            self.assertEqual(srv.server_address[0], "127.0.0.1")
        finally:
            srv.server_close()


class TestResolveBindHostFailClosed(unittest.TestCase):
    def test_wildcard_and_malformed_and_hostname_refused(self):
        for bad in ("", " ", "*", "0", "localhost", "evil.example.com",
                    "0.0.0.0.0", "127.0.0.1:1", "10.0.0.5", "::"):
            with _Env(BRIDGE_BIND_HOST=bad):
                with self.assertRaises(SystemExit, msg="expected refusal for %r" % bad):
                    bridge.resolve_bind_host()


class TestHttpOnlyMode(unittest.TestCase):
    def test_default_off(self):
        with _Env(BRIDGE_HTTP_ONLY=None):
            self.assertFalse(bridge.http_only_enabled())

    def test_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "on", "On"):
            with _Env(BRIDGE_HTTP_ONLY=v):
                self.assertTrue(bridge.http_only_enabled(), v)

    def test_falsy_values(self):
        for v in ("0", "no", "off", "", "false"):
            with _Env(BRIDGE_HTTP_ONLY=v):
                self.assertFalse(bridge.http_only_enabled(), v)


class TestSourceWiring(unittest.TestCase):
    def setUp(self):
        with open(_BRIDGE_PATH, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_bind_sites_use_resolver_not_hardcoded(self):
        # No HTTP-server construction may keep a hard-coded 0.0.0.0 tuple.
        self.assertNotIn('ThreadingHTTPServer(("0.0.0.0"', self.src)
        self.assertNotIn('HTTPServer(("0.0.0.0"', self.src)
        self.assertIn("ThreadingHTTPServer((bind_host, HTTP_SERVER_PORT)", self.src)
        self.assertIn("HTTPServer((bind_host, HTTP_SERVER_PORT)", self.src)
        self.assertIn("bind_host = resolve_bind_host()", self.src)

    def test_http_only_refuses_post_mutation(self):
        self.assertIn("http_only_mode_no_mutation", self.src)

    def test_http_only_main_skips_poller(self):
        # In http-only mode main() serves the HTTP server and never enters main_loop().
        self.assertIn("if http_only_enabled():", self.src)
        self.assertIn("start_http_server()  # blocking serve_forever", self.src)


if __name__ == "__main__":
    unittest.main()
