"""ADR-0056 F2 — dedicated DEMO partial-close HOST worker. Modern-creds-only; dedicated endpoints only;
single-use authorization before a SINGLE bridge call that is NEVER retried; credential/identity redaction;
stops safely when any gate is unavailable. Loads the standalone script (scripts/mt5_partial_close_worker.py)
like the bridge tests load the bridge. No DB, no broker."""
import importlib.util
import sys

from django.conf import settings
from django.test import SimpleTestCase

WORKER_PATH = settings.BASE_DIR.parent / "scripts" / "mt5_partial_close_worker.py"


def _load_worker():
    spec = importlib.util.spec_from_file_location("mt5_partial_close_worker", str(WORKER_PATH))
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: ``@dataclass`` under ``from __future__ import annotations`` resolves its string
    # annotations via ``sys.modules[cls.__module__]``, which must exist when the class body runs.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


_INSTRUCTION = {"ok": True, "job_id": 42, "send_nonce": "nonce-1", "bridge_call": "partial_close_position",
                "ticket": 777, "volume": "0.40", "expected_symbol": "XAUUSD",
                "identity": {"expected_login": "2000001", "expected_server": "IS6-Demo",
                             "require_identity_pin": True}}


class FakeClient:
    """Stand-in for BackendClient — scripts responses and records calls. It exposes ONLY the three dedicated
    methods, so the worker has no way to reach a generic endpoint."""
    def __init__(self, *, claim, authorize=(200, {"ok": False}), complete=(200, {"ok": True})):
        self._claim, self._authorize, self._complete = claim, authorize, complete
        self.calls = []

    def claim(self):
        self.calls.append(("claim",)); return self._claim

    def authorize_send(self, job_id, dispatch_token):
        self.calls.append(("authorize", job_id, dispatch_token)); return self._authorize

    def complete(self, job_id, dispatch_token, send_nonce, bridge_result):
        self.calls.append(("complete", job_id, dispatch_token, send_nonce, bridge_result))
        return self._complete


class FakeHttp:
    def __init__(self):
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None, verify=None):
        self.posts.append((url, json, headers))

        class _R:
            status_code = 204

            def json(self_inner):
                return {}
        return _R()


class ConfigTests(SimpleTestCase):
    def setUp(self):
        self.mod = _load_worker()

    def test_disabled_by_default(self):
        with self.assertRaises(self.mod.WorkerDisabled):
            self.mod.load_config(env={})           # flag absent -> inert

    def test_enabled_but_missing_creds_is_misconfigured(self):
        with self.assertRaises(self.mod.WorkerMisconfigured):
            self.mod.load_config(env={"COPIER_PCLOSE_WORKER_ENABLED": "1"})

    def test_enabled_with_creds(self):
        cfg = self.mod.load_config(env={
            "COPIER_PCLOSE_WORKER_ENABLED": "true", "GUVFX_BACKEND_BASE_URL": "https://api.example/",
            "GUVFX_PCLOSE_WORKER_ID": "pcw1", "GUVFX_PCLOSE_WORKER_SECRET": "s"})
        self.assertEqual(cfg.base_url, "https://api.example")      # trailing slash stripped
        self.assertEqual(cfg.worker_id, "pcw1")


class RunOnceTests(SimpleTestCase):
    def setUp(self):
        self.mod = _load_worker()

    def _bridge(self):
        calls = []

        def _call(*, ticket, volume, identity, expected_symbol):
            calls.append({"ticket": ticket, "volume": volume, "identity": identity,
                          "expected_symbol": expected_symbol})
            return {"ok": True, "mutated": True, "need_reconcile": True}
        _call.calls = calls
        return _call

    def test_happy_single_bridge_call_then_complete(self):
        client = FakeClient(claim=(200, {"ok": True, "job_id": 42, "dispatch_token": "tok"}),
                            authorize=(200, _INSTRUCTION), complete=(200, {"ok": True, "status": "X"}))
        bridge = self._bridge()
        out = self.mod.run_once(client, bridge_call=bridge)
        self.assertEqual(out["action"], "completed")
        self.assertEqual(len(bridge.calls), 1)                       # EXACTLY one broker call
        self.assertEqual(bridge.calls[0]["ticket"], 777)             # server-issued, used verbatim
        self.assertEqual(bridge.calls[0]["expected_symbol"], "XAUUSD")
        # completion carried the dispatch token + send nonce + the bridge result
        complete_call = [c for c in client.calls if c[0] == "complete"][0]
        self.assertEqual(complete_call[2], "tok")
        self.assertEqual(complete_call[3], "nonce-1")

    def test_authorize_refused_bridge_not_called(self):
        client = FakeClient(claim=(200, {"ok": True, "job_id": 42, "dispatch_token": "tok"}),
                            authorize=(409, {"ok": False, "reason": "flags_off"}))
        bridge = self._bridge()
        out = self.mod.run_once(client, bridge_call=bridge)
        self.assertEqual(out["action"], "authorize_refused")
        self.assertEqual(len(bridge.calls), 0)                       # the gate stopped before any send
        self.assertFalse([c for c in client.calls if c[0] == "complete"])

    def test_no_job_is_idle(self):
        client = FakeClient(claim=(204, {}))
        bridge = self._bridge()
        out = self.mod.run_once(client, bridge_call=bridge)
        self.assertEqual(out["action"], "idle")
        self.assertEqual(len(bridge.calls), 0)

    def test_claim_unavailable_stops_safely(self):
        client = FakeClient(claim=(200, {"ok": False, "reason": "flags_off"}))
        bridge = self._bridge()
        out = self.mod.run_once(client, bridge_call=bridge)
        self.assertEqual(out["action"], "claim_unavailable")
        self.assertEqual(len(bridge.calls), 0)

    def test_bridge_exception_is_not_retried_and_reports_reconcile(self):
        client = FakeClient(claim=(200, {"ok": True, "job_id": 42, "dispatch_token": "tok"}),
                            authorize=(200, _INSTRUCTION), complete=(200, {"ok": True, "status": "X"}))
        attempts = []

        def _boom(*, ticket, volume, identity, expected_symbol):
            attempts.append(1)
            raise TimeoutError("broker timeout")
        out = self.mod.run_once(client, bridge_call=_boom)
        self.assertEqual(out["action"], "completed")
        self.assertEqual(len(attempts), 1)                           # NEVER retried
        complete_call = [c for c in client.calls if c[0] == "complete"][0]
        self.assertTrue(complete_call[4]["need_reconcile"])          # reported for authoritative reconcile
        self.assertEqual(complete_call[4]["error"], "worker_exception_no_retry")


class EndpointIsolationTests(SimpleTestCase):
    def setUp(self):
        self.mod = _load_worker()

    def test_client_posts_only_dedicated_endpoints_with_modern_headers(self):
        cfg = self.mod.WorkerConfig(base_url="https://api.example", worker_id="pcw1", worker_secret="s")
        http = FakeHttp()
        client = self.mod.BackendClient(cfg, http=http)
        client.claim()
        client.authorize_send(1, "tok")
        client.complete(1, "tok", "nonce", {"ok": True})
        urls = [u for (u, _b, _h) in http.posts]
        self.assertEqual(urls, [
            "https://api.example/api/copy-trading/partial-close/claim/",
            "https://api.example/api/copy-trading/partial-close/authorize-send/",
            "https://api.example/api/copy-trading/partial-close/complete/"])
        self.assertNotIn("next", "".join(urls))                      # never the generic next_job endpoint
        for (_u, _b, headers) in http.posts:
            self.assertIn("X-Worker-Id", headers)
            self.assertIn("X-Worker-Secret", headers)
            self.assertNotIn("X-Worker-Token", headers)              # modern auth only; never a legacy token

    def test_identity_redaction_masks_account_sensitive_values(self):
        red = self.mod._redact_identity({"expected_login": "2000001", "expected_server": "IS6-Demo",
                                         "require_identity_pin": True})
        self.assertEqual(red["expected_login"], "***")
        self.assertEqual(red["expected_server"], "***")
        self.assertNotIn("2000001", str(red))
        self.assertNotIn("IS6-Demo", str(red))
