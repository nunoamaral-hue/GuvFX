"""GFX-160 / ADR-0119 — NON-TRADING one-shot ingest-signing probe tests.

Covers the pure signer/verifier (HMAC, timestamp/skew, nonce/replay, account pin, operation pin, all negative
controls), the backend gated endpoint (gate-off 503, no-window 503, positive, replay 409, wrong account/key/op,
value-blind output, and the invariant that a probe creates NO MasterEvent / lifecycle / ExecutionJob / dispatch /
authorization / arm / LiveExecutionAuthorization and never references #48), a static capability scan proving the
probe code imports no broker/order/MT5/dispatch/execution primitive, the host-side one-shot signer, and a smoke
check that the existing ingestion/attestation endpoints are unchanged. Synthetic keys only."""
import importlib.util
import io
import json
import os
import tempfile
import time
import tokenize
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from trade_copier.emitter import ingest_probe as ip

# Clearly-synthetic 40-hex test secret (>= 32 bytes); same pattern as tests_attestation_verify.SECRET.
SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
PROBE_ACCOUNT_ID = 46
ON = dict(COPIER_ENABLED=True, COPIER_EVENT_INGESTION_ENABLED=True, COPIER_INGEST_PROBE_ENABLED=True,
          SECURE_SSL_REDIRECT=False)

_ROOT = Path(__file__).resolve().parents[2]
_SIGNER = _ROOT / "deploy" / "copier-emitter" / "run_ingest_probe.py"


def _burn_once():
    """A fresh in-memory single-use nonce burn (True first time, False on replay)."""
    seen = set()

    def _b(nonce, expiry):
        if nonce in seen:
            return False
        seen.add(nonce)
        return True
    return _b


class IngestProbePureTests(SimpleTestCase):
    """The pure, Django-free signer/verifier — every negative control fails closed."""

    def _env(self, **kw):
        kw.setdefault("secret", SECRET)
        kw.setdefault("account_id", PROBE_ACCOUNT_ID)
        kw.setdefault("nonce", "n-" + str(time.time()))
        kw.setdefault("now", time.time())
        return ip.build_ingest_probe(**kw)

    def _verify(self, env, *, now=None, burn=None, expected_account_id=PROBE_ACCOUNT_ID):
        return ip.verify_ingest_probe(env, keyring={"copier-ingest": SECRET}, now=(now or time.time()),
                                      nonce_burn=(burn or _burn_once()), expected_account_id=expected_account_id)

    def test_happy_roundtrip(self):
        out = self._verify(self._env())
        self.assertEqual(out["signed"]["operation"], ip.INGEST_PROBE_OPERATION)
        self.assertEqual(out["signed"]["account_id"], PROBE_ACCOUNT_ID)

    def test_envelope_has_no_trade_fields(self):
        signed = self._env()["signed"]
        for forbidden in ("symbol", "side", "volume", "price", "ticket", "order", "sl", "tp", "lot",
                          "position", "operation_type", "deal"):
            self.assertNotIn(forbidden, signed, f"probe envelope must not carry a trade field: {forbidden}")
        self.assertEqual(set(signed.keys()),
                         {"protocol_version", "operation", "key_id", "account_id", "nonce", "timestamp", "expiry"})

    def test_bad_signature(self):
        env = self._env()
        env["signature"] = "0" * 64
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_unknown_key_id(self):
        env = ip.build_ingest_probe(secret=SECRET, account_id=PROBE_ACCOUNT_ID, nonce="n", now=time.time(),
                                    key_id="not-the-ingest-key")
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "unknown_key_id")

    def test_tamper_after_signing(self):
        env = self._env()
        env["signed"]["account_id"] = 999      # mutate a signed field -> signature no longer matches
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env, expected_account_id=None)
        self.assertEqual(c.exception.reason, "bad_signature")

    def test_wrong_account_pin(self):
        env = ip.build_ingest_probe(secret=SECRET, account_id=48, nonce="n", now=time.time())
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env, expected_account_id=PROBE_ACCOUNT_ID)
        self.assertEqual(c.exception.reason, "account_mismatch")

    def test_bad_operation(self):
        env = self._env()
        # Re-sign a different operation so the signature matches but the operation is wrong.
        env["signed"]["operation"] = "copier_open_trade"
        env["signature"] = ip._signature(SECRET, env["signed"])
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "bad_operation")

    def test_bad_protocol(self):
        env = self._env()
        env["signed"]["protocol_version"] = "copier-ingest-probe-999"
        env["signature"] = ip._signature(SECRET, env["signed"])
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "bad_protocol_version")

    def test_stale_timestamp(self):
        env = self._env(now=time.time() - 120)
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "probe_stale")

    def test_future_timestamp(self):
        env = self._env(now=time.time() + 120)
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "timestamp_in_future")

    def test_expired(self):
        now = time.time()
        env = ip.build_ingest_probe(secret=SECRET, account_id=PROBE_ACCOUNT_ID, nonce="n", now=now, ttl_seconds=10)
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env, now=now + 20)
        self.assertEqual(c.exception.reason, "probe_expired")

    def test_expiry_too_far(self):
        now = time.time()
        env = self._env(now=now)
        env["signed"]["expiry"] = now + 10000     # beyond MAX_EXPIRY_WINDOW_SECONDS
        env["signature"] = ip._signature(SECRET, env["signed"])
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env, now=now)
        self.assertEqual(c.exception.reason, "expiry_too_far")

    def test_replay(self):
        burn = _burn_once()
        env = self._env(nonce="fixed")
        self._verify(env, burn=burn)
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env, burn=burn)
        self.assertEqual(c.exception.reason, "nonce_replayed")

    def test_nonce_too_long(self):
        env = self._env(nonce="x" * 200)
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "nonce_too_long")

    def test_missing_signed_field(self):
        env = self._env()
        del env["signed"]["nonce"]
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify(env)
        self.assertEqual(c.exception.reason, "missing_signed_field")

    def test_malformed_envelope(self):
        with self.assertRaises(ip.IngestProbeError) as c:
            self._verify({"not": "an envelope"})
        self.assertEqual(c.exception.reason, "malformed_envelope")

    def test_nonce_not_burned_on_bad_signature(self):
        # A bad signature must NEVER consume a nonce (no unauthenticated nonce oracle).
        burned = []

        def _b(n, e):
            burned.append(n)
            return True
        env = self._env(nonce="should-not-burn")
        env["signature"] = "0" * 64
        with self.assertRaises(ip.IngestProbeError):
            ip.verify_ingest_probe(env, keyring={"copier-ingest": SECRET}, now=time.time(),
                                   nonce_burn=_b, expected_account_id=PROBE_ACCOUNT_ID)
        self.assertEqual(burned, [])


@override_settings(**ON)
class IngestProbeBackendTests(TestCase):
    """The gated backend endpoint + the no-side-effect invariant."""

    URL = "/api/copy-trading/ingest-probe/"

    def _env(self, **kw):
        kw.setdefault("secret", SECRET)
        kw.setdefault("account_id", PROBE_ACCOUNT_ID)
        kw.setdefault("nonce", "n-" + str(time.time()))
        kw.setdefault("now", time.time())
        return ip.build_ingest_probe(**kw)

    def _post(self, env, *, open_window=True, purpose="INGEST", key=SECRET):
        from trade_copier.verifier_window import verification_window
        if open_window:
            with verification_window(key, purpose=purpose):
                return self.client.post(self.URL, data=json.dumps(env), content_type="application/json")
        return self.client.post(self.URL, data=json.dumps(env), content_type="application/json")

    def test_gate_off_master_503(self):
        with override_settings(COPIER_ENABLED=False):
            r = self._post(self._env())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "disabled")

    def test_gate_off_ingestion_503(self):
        with override_settings(COPIER_EVENT_INGESTION_ENABLED=False):
            r = self._post(self._env())
        self.assertEqual(r.status_code, 503)

    def test_gate_off_probe_flag_503(self):
        with override_settings(COPIER_INGEST_PROBE_ENABLED=False):
            r = self._post(self._env())
        self.assertEqual(r.status_code, 503)

    def test_no_window_503_unavailable(self):
        r = self._post(self._env(), open_window=False)
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["status"], "unavailable")

    def test_wrong_purpose_window_503(self):
        # An ATTEST-only window does not cover INGEST -> fail closed.
        r = self._post(self._env(), purpose="ATTEST")
        self.assertEqual(r.status_code, 503)

    def test_positive_verified_and_value_blind(self):
        env = self._env()
        r = self._post(env)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["status"], "verified")
        self.assertTrue(body["verified"])
        self.assertEqual(body["account_id"], PROBE_ACCOUNT_ID)
        self.assertEqual(body["operation"], ip.INGEST_PROBE_OPERATION)
        # Value-blind: never the signature, secret, nonce or any trade data.
        self.assertEqual(set(body.keys()), {"status", "verified", "account_id", "operation", "protocol_version"})
        blob = json.dumps(body)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(env["signature"], blob)
        self.assertNotIn(env["signed"]["nonce"], blob)

    def _exec_counts(self):
        from execution.models import ExecutionJob, LiveExecutionAuthorization
        from trade_copier.models import (CopierLiveArm, CopierLiveAuthorization, CopierLifecycleDispatch,
                                         CopierPositionLifecycle, MasterEvent)
        return (MasterEvent.objects.count(), CopierPositionLifecycle.objects.count(),
                CopierLifecycleDispatch.objects.count(), ExecutionJob.objects.count(),
                CopierLiveAuthorization.objects.count(), CopierLiveArm.objects.count(),
                LiveExecutionAuthorization.objects.count())

    def test_no_side_effects_on_every_path(self):
        # The central safety claim: NO probe path (verified, bad-signature, wrong account, wrong operation,
        # replay, malformed) creates ANY execution/lifecycle/authz/arm row. Asserted across ALL outcomes, not
        # only the 200 happy path.
        base = self._exec_counts()
        # 1) verified 200
        self.assertEqual(self._post(self._env()).status_code, 200)
        # 2) bad signature 401
        bad = self._env()
        bad["signature"] = "0" * 64
        self.assertEqual(self._post(bad).status_code, 401)
        # 3) wrong account 401
        self.assertEqual(self._post(
            ip.build_ingest_probe(secret=SECRET, account_id=99, nonce="n", now=time.time())).status_code, 401)
        # 4) wrong operation 400
        wop = self._env()
        wop["signed"]["operation"] = "copier_open_trade"
        wop["signature"] = ip._signature(SECRET, wop["signed"])
        self.assertEqual(self._post(wop).status_code, 400)
        # 5) replay 409
        rp = self._env(nonce="replay-side-effect")
        self._post(rp)
        self.assertEqual(self._post(rp).status_code, 409)
        self.assertEqual(self._exec_counts(), base,
                         "NO probe path (verified/reject/replay/malformed) may create an execution/lifecycle row")

    def test_replay_409(self):
        env = self._env(nonce="fixed-probe-nonce")
        r1 = self._post(env)
        r2 = self._post(env)
        self.assertEqual(r1.status_code, 200, r1.content)
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["status"], "replayed")

    def test_wrong_account_401(self):
        env = ip.build_ingest_probe(secret=SECRET, account_id=48, nonce="n", now=time.time())
        r = self._post(env)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "account_not_probe_pinned")

    def test_wrong_key_401(self):
        # Signed with a different secret than the window carries.
        other = "f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0"
        env = ip.build_ingest_probe(secret=other, account_id=PROBE_ACCOUNT_ID, nonce="n", now=time.time())
        r = self._post(env, key=SECRET)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "bad_signature")

    def test_wrong_operation_400(self):
        env = self._env()
        env["signed"]["operation"] = "copier_open_trade"
        env["signature"] = ip._signature(SECRET, env["signed"])
        r = self._post(env)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["reason"], "bad_operation")

    def test_malformed_json_400(self):
        from trade_copier.verifier_window import verification_window
        with verification_window(SECRET, purpose="INGEST"):
            r = self.client.post(self.URL, data="{not json", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["reason"], "malformed_json")

    def test_stale_timestamp_401(self):
        r = self._post(self._env(now=time.time() - 120))
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["reason"], "probe_stale")

    def test_no_account_48_reference_in_output(self):
        r = self._post(self._env())
        self.assertNotIn("48", json.dumps(r.json()))


class IngestProbeCapabilityScanTests(SimpleTestCase):
    """Static proof: the probe code (docstrings/comments stripped) imports/uses no broker/order/MT5/dispatch/
    execution primitive, so it is structurally incapable of placing or driving a trade."""

    MODULES = [
        _ROOT / "backend" / "trade_copier" / "emitter" / "ingest_probe.py",
        _ROOT / "backend" / "trade_copier" / "ingest_probe_verify.py",
        _ROOT / "backend" / "trade_copier" / "ingest_probe_api.py",
        _ROOT / "deploy" / "copier-emitter" / "run_ingest_probe.py",
    ]
    FORBIDDEN = (
        "order_send", "place_order", "ordersend", "broker_order", "close_position", "modify_position",
        "ExecutionJob", "MasterEvent", "CopierPositionLifecycle", "CopierLifecycleDispatch",
        "LiveExecutionAuthorization", "CopierLiveArm", "CopierLiveAuthorization", "dispatch_master",
        "produce_", "admit_", "mt5_adapter", "MetaTrader", "mt5", "AllowLiveTrading",
    )

    @staticmethod
    def _code_only(path):
        """Return the module source with ALL string literals and comments removed (so a docstring that NAMES a
        forbidden primitive to say it is never used cannot false-trip the scan)."""
        toks = []
        with open(path, "rb") as fh:
            for tok in tokenize.tokenize(fh.readline):
                if tok.type in (tokenize.STRING, tokenize.COMMENT, tokenize.ENCODING):
                    continue
                toks.append(tok.string)
        return " ".join(toks)

    def test_no_execution_or_broker_primitive_in_code(self):
        for path in self.MODULES:
            code = self._code_only(path)
            for tok in self.FORBIDDEN:
                self.assertNotIn(tok, code, f"{Path(path).name} code references a forbidden primitive: {tok}")

    def test_no_account_48_in_code(self):
        for path in self.MODULES:
            code = self._code_only(path)
            self.assertNotIn("48", code, f"{Path(path).name} code references account 48")

    def test_no_secret_literal(self):
        import re
        secret_re = re.compile(r"\b[0-9a-fA-F]{40,}\b")
        for path in self.MODULES:
            self.assertFalse(secret_re.search(Path(path).read_text()), f"secret-looking literal in {Path(path).name}")

    def test_imports_pull_in_no_execution_or_broker_module(self):
        # Transitive-hardening of the lexical scan: parse the AST and assert NO probe module imports a module whose
        # name reaches execution/dispatch/order/MT5/broker-order machinery (an imported helper could otherwise
        # smuggle execution authority past the token denylist).
        import ast
        forbidden = ("execution_producer", "lifecycle_arming", "live_authz", "dispatch", "mt5_adapter",
                     "bridge_emitter", "order_send", "producer", "broker_order", "consumer_api", "operator_api")
        for path in self.MODULES:
            tree = ast.parse(Path(path).read_text())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        names.add(node.module)
            for name in names:
                low = name.lower()
                for tok in forbidden:
                    self.assertNotIn(tok, low, f"{Path(path).name} imports a forbidden module: {name}")


class ExistingPathsUnchangedTests(TestCase):
    """The probe must not disturb the existing ingestion/attestation endpoints: both still fail closed (503) when
    their own gates are off, proving the new route did not alter their wiring."""

    def test_attest_and_ingest_still_dark_by_default(self):
        env = {"signed": {}, "signature": "x"}
        ra = self.client.post("/api/copy-trading/attest-verify/", data=json.dumps(env),
                              content_type="application/json")
        ri = self.client.post("/api/copy-trading/event-ingest/", data=json.dumps(env),
                              content_type="application/json")
        self.assertEqual(ra.status_code, 503)
        self.assertEqual(ri.status_code, 503)


def _load_signer():
    spec = importlib.util.spec_from_file_location("run_ingest_probe", str(_SIGNER))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class IngestProbeHostSignerTests(SimpleTestCase):
    """The host-side one-shot signer: reads the standing key locally, signs ONE non-trading envelope, posts once
    (optional one replay), emits redacted JSON, retains the standing key, never leaks the secret."""

    def setUp(self):
        self.signer = _load_signer()
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.json")
        self.key_path = os.path.join(self.tmp, "ingest.key")
        with open(self.config_path, "w") as fh:
            json.dump({"account_id": PROBE_ACCOUNT_ID,
                       "ingest_url": "https://api.example.invalid/api/copy-trading/event-ingest/"}, fh)
        with open(self.key_path, "w") as fh:
            fh.write(SECRET)

    def _run(self, argv, transport):
        buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(buf):
            rc = self.signer.main(argv, transport=transport)
        return rc, json.loads(buf.getvalue().strip()), buf.getvalue()

    def test_happy_single_probe(self):
        posts = []

        def t(url, body):
            posts.append((url, body))
            return (200, True, "verified", False)
        rc, out, raw = self._run(["--config", self.config_path], transport=t)
        self.assertEqual(rc, 0)
        self.assertTrue(out["verified"])
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(out["account_id"], PROBE_ACCOUNT_ID)
        self.assertEqual(len(posts), 1)
        # fixed probe route derived from the config origin (never event-ingest/attest)
        self.assertTrue(posts[0][0].endswith("/api/copy-trading/ingest-probe/"))
        # the key value never appears in output, and the standing key file is RETAINED
        self.assertNotIn(SECRET, raw)
        self.assertTrue(os.path.isfile(self.key_path), "standing key must be retained (never shredded)")
        with open(self.key_path) as fh:
            self.assertEqual(fh.read(), SECRET)

    def test_replay_attempt(self):
        seq = [(200, True, "verified", False), (409, False, "replayed", False)]

        def t(url, body):
            return seq.pop(0)
        rc, out, _ = self._run(["--config", self.config_path, "--replay"], transport=t)
        self.assertEqual(out["attempts"], 2)
        self.assertEqual(out["replay_reason"], "replayed")

    def test_secret_unavailable_when_key_absent(self):
        os.remove(self.key_path)
        rc, out, _ = self._run(["--config", self.config_path], transport=lambda u, b: (200, True, "verified", False))
        self.assertEqual(rc, 1)
        self.assertEqual(out["reason"], "SECRET_UNAVAILABLE")
        self.assertFalse(out["posted"])

    def test_secret_unavailable_when_key_too_short(self):
        with open(self.key_path, "w") as fh:
            fh.write("short")
        rc, out, _ = self._run(["--config", self.config_path], transport=lambda u, b: (200, True, "verified", False))
        self.assertEqual(out["reason"], "SECRET_UNAVAILABLE")

    def test_account_pin(self):
        with open(self.config_path, "w") as fh:
            json.dump({"account_id": 48, "ingest_url": "https://api.example.invalid/x/"}, fh)
        rc, out, _ = self._run(["--config", self.config_path], transport=lambda u, b: (200, True, "verified", False))
        self.assertEqual(out["reason"], "account_not_probe_pinned")
        self.assertFalse(out["posted"])

    def test_envelope_posted_is_non_trading(self):
        captured = {}

        def t(url, body):
            captured["body"] = json.loads(body.decode("utf-8"))
            return (200, True, "verified", False)
        self._run(["--config", self.config_path], transport=t)
        signed = captured["body"]["signed"]
        self.assertEqual(signed["operation"], ip.INGEST_PROBE_OPERATION)
        for forbidden in ("symbol", "volume", "side", "price", "ticket", "order"):
            self.assertNotIn(forbidden, signed)

    def test_transport_ambiguous_skips_replay(self):
        calls = []

        def t(url, body):
            calls.append(1)
            return (0, False, "post_error:URLError", True)
        rc, out, _ = self._run(["--config", self.config_path, "--replay"], transport=t)
        self.assertTrue(out["transport_ambiguous"])
        self.assertEqual(out["attempts"], 1)      # a replay is NOT attempted on an ambiguous transport
        self.assertEqual(len(calls), 1)
