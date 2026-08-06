"""ADR-0027 — AGENT-side VALIDATE_LOGIN handler tests (imports the deploy/beta-agent bundle).

Drives a REAL signed VALIDATE_LOGIN request end-to-end through the shipped agent core + the isolated-
terminal login handler, with a fake MT5 probe + fake transport (no Windows, MT5, host or network). Proves:
isolated-terminal contract (accept dedicated / reject slot / golden / prod / missing-exe), envelope opened
under the request's own AAD, single-flight lock contention, the full MT5 error → taxonomy mapping,
demo/contest/real classification, guaranteed shutdown on EVERY path, the probe exposes NO order/symbol API,
integrity-drift + unconfigured fail-closed, nonce replay, payload tamper, and that no plaintext/ciphertext/
host-path ever appears in the response or logs.
"""
import io
import json
import logging
import os
import sys
import threading
import types
from contextlib import contextmanager

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

from lib import mgmt_protocol as proto          # noqa: E402
from lib.mgmt_agent_core import BetaProvisioningAgent  # noqa: E402
import broker_cred_envelope as cred              # noqa: E402
import validate_login as vl                      # noqa: E402

KEYRING = {"k1": "agent-secret-key"}
RUUID = "abcdef01-2345-6789-abcd-ef0123456789"
VDIR = r"C:\GuvFX\beta\validation\vt"
SECRET = "s3cret-broker-pw!never-real"
_PRIV = X25519PrivateKey.generate()


# ── fakes ──
class _Nonce:
    def __init__(self): self._s = set()
    def seen(self, n): return n in self._s
    def burn(self, n, e):
        if n in self._s:
            return False
        self._s.add(n)
        return True


class _Idem:
    def __init__(self): self._d = {}
    def get(self, j, op): return self._d.get((j, op))
    def put(self, j, op, resp): self._d[(j, op)] = resp


class _Locks:
    @contextmanager
    def acquire(self, u):
        yield


class FakeProbe:
    """Minimal MT5 stand-in exposing ONLY the login/classify surface — deliberately NO order/symbol/position
    method, so a test can assert the probe cannot be used to trade."""
    def __init__(self, *, ok=True, err=(0, ""), trade_mode=0, account=True, raise_on_init=False):
        self._ok, self._err, self._trade_mode = ok, err, trade_mode
        self._account, self._raise = account, raise_on_init
        self.init_kwargs = None
        self.shutdown_called = False

    def initialize(self, **kw):
        self.init_kwargs = kw
        if self._raise:
            raise RuntimeError("boom")
        return self._ok

    def last_error(self):
        return self._err

    def account_info(self):
        if not self._account:
            return None
        return types.SimpleNamespace(trade_mode=self._trade_mode)

    def shutdown(self):
        self.shutdown_called = True


def _handler(holder, *, validation_dir=VDIR, lock=None,
             path_exists=lambda p: p.lower().endswith("terminal64.exe")):
    """Build a real LoginValidationHandler wired to a captured fake probe + the test private key."""
    holder.setdefault("factory_calls", 0)

    def factory():
        holder["factory_calls"] += 1
        return holder["probe"]

    return vl.LoginValidationHandler(
        open_envelope=lambda sealed, aad: cred.open_envelope(sealed, aad=aad, recipient_private_key=_PRIV),
        bind_aad=cred.bind_aad, mt5_probe_factory=factory, path_exists=path_exists,
        validation_dir=validation_dir, lock=lock)


def _agent(handler, *, manifest=None, login_validator=True):
    manifest = manifest or {f"op_{o.lower()}": "ok" for o in proto.ALLOWED_OPERATIONS}
    return BetaProvisioningAgent(
        keyring=KEYRING, nonce_store=_Nonce(), idempotency_store=_Idem(), op_impls={},
        agent_version="agent-1.0", script_manifest=manifest,
        script_versions={f"op_{o.lower()}": "ps-1.0" for o in proto.ALLOWED_OPERATIONS},
        resolve_real_path=lambda p: None, runtime_locks=_Locks(), manifest_version="m",
        now_fn=lambda: 1_000_010, login_validator=handler if login_validator else None)


def _payload(*, login="1302575", server="IS6Technologies-Demo", password=SECRET,
            op="VALIDATE_LOGIN", ruuid=RUUID, corr="corr-1", nonce="n-1", key_id="k1"):
    aad = cred.bind_aad(operation=op, runtime_uuid=ruuid, correlation_id=corr, nonce=nonce)
    sealed = cred.seal(password.encode("utf-8"), aad=aad, recipient_public_key=_PRIV.public_key())
    sealed["key_id"] = key_id
    return {"login": login, "server": server, "enc_key_id": key_id, "password_env": sealed}


def _req(payload=None, *, op="VALIDATE_LOGIN", ruuid=RUUID, corr="corr-1", nonce="n-1", now=1_000_010,
         job_id=0):
    if payload is None:
        payload = _payload(op=op, ruuid=ruuid, corr=corr, nonce=nonce)
    return proto.sign_request(provisioning_job_id=job_id, runtime_uuid=ruuid, operation=op,
                              correlation_id=corr, keyring=KEYRING, key_id="k1", now=now, nonce=nonce,
                              payload=payload)


class NegotiateTests(SimpleTestCase):
    def test_negotiate_advertises_validate_login(self):
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder)).handle(
            proto.sign_request(provisioning_job_id=0, runtime_uuid=proto.NIL_UUID, operation="NEGOTIATE",
                               correlation_id="c", keyring=KEYRING, key_id="k1", now=1_000_010))
        self.assertEqual(r["outcome"], "ok")
        self.assertIn("VALIDATE_LOGIN", r["supported_operations"])


class SuccessPathTests(SimpleTestCase):
    def test_demo_login_ok(self):
        holder = {"probe": FakeProbe(ok=True, trade_mode=0)}
        r = _agent(_handler(holder)).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"], r["is_demo"]), ("ok", "demo_ok", True))
        # the probe was pointed at the DEDICATED validation terminal, with the recovered password + int login
        kw = holder["probe"].init_kwargs
        self.assertEqual(kw["path"], VDIR + r"\terminal64.exe")
        self.assertEqual(kw["login"], 1302575)
        self.assertEqual(kw["password"], SECRET)
        self.assertEqual(kw["server"], "IS6Technologies-Demo")
        self.assertTrue(holder["probe"].shutdown_called)

    def test_contest_and_real_are_live_detected(self):
        for tm in (1, 2):
            holder = {"probe": FakeProbe(ok=True, trade_mode=tm)}
            r = _agent(_handler(holder)).handle(_req())
            self.assertEqual((r["outcome"], r["reason_code"], r["is_demo"]),
                             ("ok", "live_detected", False), f"trade_mode={tm}")
            self.assertTrue(holder["probe"].shutdown_called)

    def test_account_info_absent_is_could_not_verify(self):
        holder = {"probe": FakeProbe(ok=True, account=False)}
        r = _agent(_handler(holder)).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "could_not_verify"))
        self.assertNotIn("is_demo", r)
        self.assertTrue(holder["probe"].shutdown_called)

    def test_trade_mode_absent_is_could_not_verify(self):
        probe = FakeProbe(ok=True)
        probe.account_info = lambda: types.SimpleNamespace()   # no trade_mode attribute
        holder = {"probe": probe}
        r = _agent(_handler(holder)).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "could_not_verify"))
        self.assertTrue(probe.shutdown_called)


class TaxonomyTests(SimpleTestCase):
    CASES = {
        (-6, "Terminal: Authorization failed"): "invalid_password",
        (None, "Invalid account"): "invalid_login",
        (None, "Account disabled"): "account_disabled",
        (-6, ""): "invalid_password",
        # Phase-4 WS-C: -10005 is RES_E_INTERNAL_FAIL_TIMEOUT — the LOCAL IPC call timing out, the sibling of
        # -10004. It is an INTERNAL timeout, never a broker/login timeout, so it maps to validation_ipc_
        # unavailable (was mislabelled login_timeout, which renders as a broker outage).
        (-10005, ""): "validation_ipc_unavailable",
        # WS-A (2026-08-05): -10004 is RES_E_INTERNAL_FAIL_CONNECT — the LOCAL Python↔terminal IPC connect,
        # BEFORE any broker contact. It must never be reported as a broker outage (was "server_unavailable").
        (-10004, ""): "validation_ipc_unavailable",
        # A NAMED broker server that is unreachable IS a genuine broker-server-unavailable result (preserved).
        (None, "No connection to trade server"): "server_unavailable",
        (None, "unknown server"): "server_not_found",
        (999, "some brand-new text"): "could_not_verify",
    }

    def test_init_failure_taxonomy_and_shutdown(self):
        for err, expected in self.CASES.items():
            holder = {"probe": FakeProbe(ok=False, err=err)}
            r = _agent(_handler(holder)).handle(_req())
            self.assertEqual((r["outcome"], r["reason_code"]), ("denied", expected), err)
            self.assertTrue(holder["probe"].shutdown_called, err)

    def test_probe_exception_maps_safely_and_shuts_down(self):
        holder = {"probe": FakeProbe(raise_on_init=True)}
        r = _agent(_handler(holder)).handle(_req())
        # the CORE never leaks a raw exception; the probe layer maps to a retryable code and STILL shut down
        self.assertEqual(r["outcome"], "denied")
        self.assertEqual(r["reason_code"], "could_not_verify")
        self.assertTrue(holder["probe"].shutdown_called)

    def test_probe_factory_raise_maps_safely(self):
        # a factory fault (before any terminal exists) must still return a dict, never raise
        def boom():
            raise RuntimeError("factory down")
        h = vl.LoginValidationHandler(
            open_envelope=lambda sealed, aad: cred.open_envelope(sealed, aad=aad, recipient_private_key=_PRIV),
            bind_aad=cred.bind_aad, mt5_probe_factory=boom,
            path_exists=lambda p: p.lower().endswith("terminal64.exe"), validation_dir=VDIR)
        r = _agent(h).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "could_not_verify"))


class IsolationTests(SimpleTestCase):
    def _run(self, validation_dir, path_exists=lambda p: True):
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder, validation_dir=validation_dir, path_exists=path_exists)).handle(_req())
        return r, holder

    def test_accepts_dedicated_dir(self):
        r, holder = self._run(VDIR, path_exists=lambda p: p.lower().endswith("terminal64.exe"))
        self.assertEqual(r["outcome"], "ok")

    def test_rejects_slot_path(self):
        r, holder = self._run(r"C:\GuvFX\beta\slots\2\terminal")
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"))
        self.assertEqual(holder["factory_calls"], 0)          # NO probe against a slot

    def test_rejects_golden_path(self):
        for golden in (r"C:\GuvFX\beta\golden\newMT5", r"C:\GuvFX\golden\newMT5"):
            r, holder = self._run(golden)
            self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"), golden)
            self.assertEqual(holder["factory_calls"], 0)

    def test_rejects_prod_and_accounts_path(self):
        for prod in (r"C:\Program Files\MetaTrader 5", r"C:\GuvFX\beta\accounts\x\terminal"):
            r, holder = self._run(prod)
            self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"), prod)
            self.assertEqual(holder["factory_calls"], 0)

    def test_rejects_missing_executable(self):
        r, holder = self._run(VDIR, path_exists=lambda p: False)
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"))
        self.assertEqual(holder["factory_calls"], 0)

    def test_rejects_relative_dir(self):
        r, holder = self._run(r"beta\validation\vt")
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"))
        self.assertEqual(holder["factory_calls"], 0)

    def test_rejects_dotdot_traversal_into_a_slot(self):
        # lexically beneath the validation root, but `..` resolves into a live slot — must be refused
        r, holder = self._run(r"C:\GuvFX\beta\validation\..\slots\1\terminal")
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "isolation_check_failed"))
        self.assertEqual(holder["factory_calls"], 0)


class IsolationContractUnitTests(SimpleTestCase):
    """Direct unit tests of the isolation contract, incl. the review-hardening for `..` and a bare-drive root."""
    def _assert(self, d, **kw):
        return vl.assert_isolated_validation_terminal(d, path_exists=lambda p: True, **kw)

    def test_accepts_clean_dedicated(self):
        self.assertTrue(self._assert(VDIR).endswith(r"\terminal64.exe"))

    def test_rejects_dotdot_in_dir(self):
        with self.assertRaises(vl.IsolationError) as c:
            self._assert(r"C:\GuvFX\beta\validation\..\slots\1")
        self.assertEqual(c.exception.reason_code, "validation_terminal_not_isolated")

    def test_rejects_bare_drive_root(self):
        # a bare-drive validation ROOT would make every absolute path "isolated" — refuse it
        with self.assertRaises(vl.IsolationError) as c:
            self._assert(r"C:\anything\vt", validation_root="C:\\")
        self.assertEqual(c.exception.reason_code, "validation_terminal_unconfigured")

    def test_rejects_dotdot_in_root(self):
        with self.assertRaises(vl.IsolationError):
            self._assert(r"C:\GuvFX\beta\validation\vt",
                         validation_root=r"C:\GuvFX\beta\..\beta\validation")


class IsolationReportTests(SimpleTestCase):
    """Runner-isolation diagnostics packet (2026-08-06): the structured, secret-safe ``isolation_report`` and
    its wiring into the handler denial. The authoritative fail-closed decision and the customer reason
    (``isolation_check_failed``) are UNCHANGED; the report is operator-only (carried under ``_isolation``, never
    ``_operator``, so the runner's stage derivation is unaffected)."""
    def _rep(self, d, **kw):
        kw.setdefault("path_exists", lambda p: True)
        return vl.isolation_report(d, **kw)

    def test_missing_dir_is_unconfigured(self):
        r = self._rep("")
        self.assertEqual((r["result"], r["sub_reason"]), ("fail", "validation_terminal_unconfigured"))
        self.assertFalse(r["checks"]["absolute"])

    def test_relative_dir_is_unconfigured(self):
        self.assertEqual(self._rep(r"relative\vt")["sub_reason"], "validation_terminal_unconfigured")

    def test_root_invalid(self):
        self.assertEqual(self._rep(r"C:\anything\vt", validation_root="C:\\")["sub_reason"],
                         "validation_root_invalid")

    def test_traversal(self):
        r = self._rep(r"C:\GuvFX\beta\validation\..\slots\1", validation_root=r"C:\GuvFX\beta\validation")
        self.assertEqual(r["sub_reason"], "validation_terminal_traversal")
        self.assertFalse(r["checks"]["no_traversal"])

    def test_outside_root(self):
        r = self._rep(r"C:\Other\vt", validation_root=r"C:\GuvFX\beta\validation")
        self.assertEqual(r["sub_reason"], "validation_terminal_outside_root")
        self.assertFalse(r["checks"]["beneath_root"])

    def test_beneath_forbidden_root_records_match(self):
        r = self._rep(r"C:\GuvFX\beta\validation-5833\terminal",
                      validation_root=r"C:\GuvFX\beta\validation-5833", forbidden_roots=(r"C:\GuvFX\beta",))
        self.assertEqual((r["sub_reason"], r["matched_forbidden_root"]),
                         ("validation_terminal_not_isolated", r"C:\GuvFX\beta"))
        self.assertFalse(r["checks"]["disjoint"])

    def test_missing_exe(self):
        r = self._rep(VDIR, path_exists=lambda p: False)
        self.assertEqual(r["sub_reason"], "validation_terminal_missing")
        self.assertFalse(r["checks"]["terminal_present"])

    def test_valid_passes(self):
        r = self._rep(VDIR)
        self.assertEqual((r["result"], r["sub_reason"], r["matched_forbidden_root"]), ("pass", None, None))
        self.assertTrue(all(r["checks"].values()))

    def test_canonical_paths_recorded(self):
        r = self._rep(r"C:/GuvFX/BETA/validation/VT/")
        self.assertEqual(r["validation_dir_canonical"], r"c:\guvfx\beta\validation\vt")

    def test_report_carries_no_secret_and_is_json_safe(self):
        blob = json.dumps(self._rep(VDIR)).lower()
        for bad in ("password", "secret", "token", "-----begin", "bearer "):
            self.assertNotIn(bad, blob)

    def test_never_raises_on_bad_path_exists(self):
        def boom(_p):
            raise OSError("io")
        self.assertFalse(vl.isolation_report(VDIR, path_exists=boom)["checks"]["terminal_present"])

    def _iso_handler(self, vdir, root, forbidden):
        def _no_probe():
            raise AssertionError("the MT5 probe must never be built on an isolation failure")
        return vl.LoginValidationHandler(
            open_envelope=lambda s, a: b"pw", bind_aad=lambda **k: b"aad", mt5_probe_factory=_no_probe,
            path_exists=lambda p: True, validation_dir=vdir, validation_root=root, forbidden_roots=forbidden)

    def test_handler_attaches_operator_isolation_and_keeps_customer_contract(self):
        h = self._iso_handler(r"C:\GuvFX\beta\slots\1\terminal", r"C:\GuvFX\beta\slots\1",
                              (r"C:\GuvFX\beta\slots",))
        out = h.validate(operation="VALIDATE_LOGIN", runtime_uuid="u", correlation_id="c", nonce="n",
                         payload={"login": "1", "server": "s", "password_env": {}})
        # customer contract UNCHANGED (no MT5 launched — probe factory would have raised)
        self.assertEqual((out["ok"], out["reason_code"], out["is_demo"]),
                         (False, "isolation_check_failed", None))
        self.assertNotIn("_operator", out)                       # not the probe path → stage model intact
        self.assertEqual(out["_isolation"]["result"], "fail")
        self.assertEqual(out["_isolation"]["sub_reason"], "validation_terminal_not_isolated")
        self.assertEqual(out["_isolation"]["matched_forbidden_root"], r"C:\GuvFX\beta\slots")

    def test_regression_service_valid_but_runner_forbidden_differs(self):
        # The discrepancy class that produced the incident: a validation_dir that PASSES with the default
        # forbidden set but FAILS once the runner's richer forbidden set (beta_root) is applied. The report
        # localises the ACTUAL failing rule + offending root even though a manual/default check looked clean.
        vdir, vroot = r"C:\GuvFX\beta\validation-5833\terminal", r"C:\GuvFX\beta\validation-5833"
        default_ok = vl.isolation_report(vdir, validation_root=vroot,
                                         forbidden_roots=vl.DEFAULT_FORBIDDEN_ROOTS, path_exists=lambda p: True)
        with_beta = vl.isolation_report(vdir, validation_root=vroot,
                                        forbidden_roots=vl.DEFAULT_FORBIDDEN_ROOTS + (r"C:\GuvFX\beta",),
                                        path_exists=lambda p: True)
        self.assertEqual(default_ok["result"], "pass")
        self.assertEqual((with_beta["result"], with_beta["sub_reason"], with_beta["matched_forbidden_root"]),
                         ("fail", "validation_terminal_not_isolated", r"C:\GuvFX\beta"))


class LockTests(SimpleTestCase):
    def test_single_flight_busy_when_lock_held(self):
        lock = threading.Lock()
        lock.acquire()                                         # a probe is already "in flight"
        holder = {"probe": FakeProbe()}
        try:
            r = _agent(_handler(holder, lock=lock)).handle(_req())
        finally:
            lock.release()
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "validation_busy"))
        self.assertEqual(holder["factory_calls"], 0)          # never touched the terminal
        self.assertFalse(holder["probe"].shutdown_called)

    def test_lock_released_after_success(self):
        lock = threading.Lock()
        holder = {"probe": FakeProbe()}
        _agent(_handler(holder, lock=lock)).handle(_req())
        self.assertTrue(lock.acquire(blocking=False))         # freed again after the probe
        lock.release()


class FailClosedTests(SimpleTestCase):
    def test_unconfigured_validator_fails_closed(self):
        r = _agent(None, login_validator=False).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "validation_unconfigured"))

    def test_integrity_drift_blocks_validate(self):
        manifest = {f"op_{o.lower()}": "ok" for o in proto.ALLOWED_OPERATIONS}
        manifest["op_validate_login"] = "APPROVED"
        manifest["op_validate_login:actual"] = "DRIFTED"
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder), manifest=manifest).handle(_req())
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "impl_integrity_mismatch"))
        self.assertEqual(holder["factory_calls"], 0)

    def test_missing_login_or_server(self):
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder)).handle(_req(_payload(login="")))
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "invalid_login"))
        r2 = _agent(_handler(holder)).handle(_req(_payload(server="")))
        self.assertEqual((r2["outcome"], r2["reason_code"]), ("denied", "broker_server_missing"))
        self.assertEqual(holder["factory_calls"], 0)


class ProtocolBindingTests(SimpleTestCase):
    def test_payload_tamper_fails_before_probe(self):
        payload = _payload()
        req = _req(payload)
        req["payload"] = dict(payload, login="9999999")        # tamper AFTER signing
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder)).handle(req)
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "payload_digest_mismatch"))
        self.assertEqual(holder["factory_calls"], 0)

    def test_stripped_payload_fails_closed(self):
        req = _req()
        del req["payload"]                                     # signed payload_digest remains, body gone
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder)).handle(req)
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "payload_missing"))
        self.assertEqual(holder["factory_calls"], 0)

    def test_ciphertext_tamper_is_unsealable(self):
        payload = _payload()
        import base64
        ct = bytearray(base64.b64decode(payload["password_env"]["ct"]))
        ct[0] ^= 0x01
        payload["password_env"]["ct"] = base64.b64encode(bytes(ct)).decode()
        req = _req(payload)                                    # re-sign so payload_digest matches the tamper
        holder = {"probe": FakeProbe()}
        r = _agent(_handler(holder)).handle(req)
        self.assertEqual((r["outcome"], r["reason_code"]), ("denied", "credential_unsealable"))
        self.assertEqual(holder["factory_calls"], 0)          # never reached the terminal

    def test_replayed_nonce_rejected(self):
        agent = _agent(_handler({"probe": FakeProbe()}))
        req = _req()
        self.assertEqual(agent.handle(req)["outcome"], "ok")
        self.assertEqual(agent.handle(req)["reason_code"], "nonce_replayed")


class NoLeakTests(SimpleTestCase):
    def test_probe_exposes_no_trading_api(self):
        for attr in ("order_send", "order_check", "symbol_select", "symbol_info", "positions_get",
                     "history_deals_get", "buy", "sell"):
            self.assertFalse(hasattr(vl.RealMt5Probe, attr), attr)

    def test_no_secret_in_response_or_logs(self):
        buf = io.StringIO(); h = logging.StreamHandler(buf)
        root = logging.getLogger(); root.addHandler(h)
        try:
            ok = _agent(_handler({"probe": FakeProbe(ok=True, trade_mode=0)})).handle(_req())
            bad = _agent(_handler({"probe": FakeProbe(ok=False, err=(-6, "auth"))})).handle(
                _req(nonce="n-2"))
        finally:
            root.removeHandler(h)
        blob = json.dumps(ok) + json.dumps(bad) + buf.getvalue()
        self.assertNotIn(SECRET, blob)
        for r in (ok, bad):
            self.assertNotIn("password", r)
            self.assertNotIn("password_env", json.dumps(r))
            self.assertNotIn("ct", r)
            # the isolated-terminal host path is never echoed to the backend
            self.assertNotIn(VDIR, json.dumps(r))


class BuildWiringTests(SimpleTestCase):
    """The assembly in ``agent._build_login_validator`` + config parsing — the seam between config and the
    handler. Absent the validation dir OR the envelope private key, NO validator is built (fail closed)."""

    def test_no_validator_without_validation_dir(self):
        import agent as agent_mod
        self.assertIsNone(agent_mod._build_login_validator({"validation_terminal_dir": ""}))

    def test_no_validator_without_private_key(self):
        import agent as agent_mod
        # dir configured but no BROKER_CRED_ENC_PRIVKEYS in env → cannot decrypt → no validator
        with _clear_env("BROKER_CRED_ENC_PRIVKEYS"):
            self.assertIsNone(agent_mod._build_login_validator({"validation_terminal_dir": VDIR}))

    def test_validator_built_and_rejects_non_isolated(self):
        import agent as agent_mod
        priv_b64 = _b64(_PRIV.private_bytes_raw())
        with _set_env("BROKER_CRED_ENC_PRIVKEYS", json.dumps({"k1": priv_b64})):
            v = agent_mod._build_login_validator({
                "validation_terminal_dir": r"C:\GuvFX\beta\slots\1\terminal",   # a SLOT — must be rejected
                "slots_root": r"C:\GuvFX\beta\slots", "golden_dir": r"C:\GuvFX\beta\golden",
                "validation_forbidden_roots": (), "login_timeout_ms": 30000})
        self.assertIsNotNone(v)
        out = v.validate(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c", nonce="n",
                         payload=_payload(corr="c", nonce="n"))
        self.assertEqual(out["reason_code"], "isolation_check_failed")

    def test_beta_root_included_in_forbidden_union(self):
        import agent as agent_mod
        priv_b64 = _b64(_PRIV.private_bytes_raw())
        with _set_env("BROKER_CRED_ENC_PRIVKEYS", json.dumps({"k1": priv_b64})):
            # validation_root overlaps the (relocated) per-account runtime root — the disjoint check must
            # still catch it because build unions cfg["beta_root"] into the forbidden set.
            v = agent_mod._build_login_validator({
                "validation_terminal_dir": r"D:\runtimes\acct1\terminal",
                "validation_root": r"D:\runtimes", "beta_root": r"D:\runtimes",
                "slots_root": r"C:\GuvFX\beta\slots", "golden_dir": r"C:\GuvFX\beta\golden",
                "validation_forbidden_roots": (), "login_timeout_ms": 30000})
        out = v.validate(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c", nonce="n",
                         payload=_payload(corr="c", nonce="n"))
        self.assertEqual(out["reason_code"], "isolation_check_failed")

    def test_config_parses_validation_keys_and_forbidden_list_fails_closed(self):
        import config as agent_config
        base = {"BETA_AGENT_BIND_HOST": "100.79.101.19", "BETA_AGENT_BIND_PORT": "8791",
                "BETA_AGENT_KEYRING": json.dumps({"k1": "s"}), "BETA_AGENT_KEY_ID": "k1"}
        cfg = agent_config.load_config(dict(base, BETA_AGENT_VALIDATION_TERMINAL_DIR=VDIR))
        self.assertEqual(cfg["validation_terminal_dir"], VDIR)
        self.assertEqual(cfg["login_timeout_ms"], 120000)   # ADR-0027 Phase 2 canonical default
        self.assertEqual(cfg["cleanup_grace_s"], 45)         # ADR-0027 Phase 2 timeout contract
        with self.assertRaises(agent_config.ConfigError):
            agent_config.load_config(dict(base, BETA_AGENT_VALIDATION_FORBIDDEN_ROOTS="{not json"))


import base64 as _b64mod                                          # noqa: E402


def _b64(b):
    return _b64mod.b64encode(b).decode("ascii")


@contextmanager
def _set_env(key, value):
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


@contextmanager
def _clear_env(key):
    old = os.environ.pop(key, None)
    try:
        yield
    finally:
        if old is not None:
            os.environ[key] = old


class AgentEnvelopeEnvKeyTests(SimpleTestCase):
    """The PRODUCTION agent path: the private key is resolved from ``BROKER_CRED_ENC_PRIVKEYS`` in the
    environment by the envelope's own key_id (no injected key). Fail closed on unknown/malformed/absent."""

    def test_open_via_env_key(self):
        priv = X25519PrivateKey.generate()
        with _set_env("BROKER_CRED_ENC_PRIVKEYS", json.dumps({"kX": _b64(priv.private_bytes_raw())})):
            self.assertTrue(cred.agent_enc_configured())
            aad = cred.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c",
                                nonce="n")
            sealed = cred.seal(SECRET.encode(), aad=aad, recipient_public_key=priv.public_key())
            sealed["key_id"] = "kX"
            self.assertEqual(cred.open_envelope(sealed, aad=aad), SECRET.encode())   # resolves kX from env

    def test_unknown_key_id_fails_closed(self):
        priv = X25519PrivateKey.generate()
        with _set_env("BROKER_CRED_ENC_PRIVKEYS", json.dumps({"kX": _b64(priv.private_bytes_raw())})):
            aad = cred.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c",
                                nonce="n")
            sealed = cred.seal(SECRET.encode(), aad=aad, recipient_public_key=priv.public_key())
            sealed["key_id"] = "kMISSING"
            with self.assertRaises(cred.EnvelopeError) as c:
                cred.open_envelope(sealed, aad=aad)
            self.assertEqual(c.exception.reason_code, "envelope_unknown_key_id")

    def test_absent_and_malformed_env_fail_closed(self):
        with _clear_env("BROKER_CRED_ENC_PRIVKEYS"):
            self.assertFalse(cred.agent_enc_configured())
        with _set_env("BROKER_CRED_ENC_PRIVKEYS", "{not json"):
            self.assertFalse(cred.agent_enc_configured())
            with self.assertRaises(cred.EnvelopeError):
                cred.recipient_private_key_for("kX")


class CrossSideParityTests(SimpleTestCase):
    def test_bind_aad_matches_backend(self):
        from terminal_provisioning import broker_cred_envelope as backend_cred
        a = cred.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c", nonce="n")
        b = backend_cred.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c",
                                  nonce="n")
        self.assertEqual(a, b)
        self.assertEqual(cred.ENVELOPE_VERSION, backend_cred.ENVELOPE_VERSION)

    def test_backend_sealed_password_opens_in_agent(self):
        from terminal_provisioning import broker_cred_envelope as backend_cred
        aad = backend_cred.bind_aad(operation="VALIDATE_LOGIN", runtime_uuid=RUUID, correlation_id="c",
                                    nonce="n")
        sealed = backend_cred.seal(SECRET.encode(), aad=aad, key_id="k1",
                                   recipient_public_key=_PRIV.public_key())
        self.assertEqual(cred.open_envelope(sealed, aad=aad, recipient_private_key=_PRIV),
                         SECRET.encode())


class TestClassifyInitErrorIpc(SimpleTestCase):
    """WS-A (2026-08-05) — a LOCAL MT5 IPC failure (Python↔terminal, pre-broker) must never be reported as a
    broker outage. ``server_unavailable`` is preserved ONLY for genuine broker-server-reached evidence."""

    def test_no_ipc_connection_is_never_server_unavailable(self):
        # The exact attempt #7 evidence: MT5 initialize failed with (-10004, "No IPC connection").
        self.assertEqual(vl.classify_init_error(-10004, "No IPC connection"), "validation_ipc_unavailable")

    def test_code_minus_10004_always_local_ipc_regardless_of_text(self):
        for text in ("", "No IPC connection", "connection to server lost", "something unexpected"):
            self.assertEqual(vl.classify_init_error(-10004, text), "validation_ipc_unavailable")
            self.assertNotEqual(vl.classify_init_error(-10004, text), "server_unavailable")

    def test_ipc_text_markers_map_local_even_without_code(self):
        for text in ("No IPC connection", "IPC timeout", "IPC initialize failed", "IPC recv failed",
                     "no ipc connection"):
            self.assertEqual(vl.classify_init_error(None, text), "validation_ipc_unavailable")

    def test_genuine_broker_server_unavailable_is_preserved(self):
        # Broker-server-reached-and-unavailable evidence still maps to server_unavailable.
        for text in ("Trade server is unavailable", "No connection to trade server",
                     "Trade server is busy", "Server is not responding"):
            self.assertEqual(vl.classify_init_error(None, text), "server_unavailable")

    def test_credential_and_config_reasons_unchanged(self):
        self.assertEqual(vl.classify_init_error(None, "Invalid password"), "invalid_password")
        self.assertEqual(vl.classify_init_error(-6, ""), "invalid_password")
        self.assertEqual(vl.classify_init_error(None, "Invalid account"), "invalid_login")
        self.assertEqual(vl.classify_init_error(None, "account disabled"), "account_disabled")
        self.assertEqual(vl.classify_init_error(None, "Unknown server"), "server_not_found")
        # Phase-4 WS-C: a GENERIC text timeout (no IPC marker, no code) remains login_timeout — the ambiguous
        # bucket (its customer wording is now broker-neutral). The CODE -10005 is handled separately below.
        self.assertEqual(vl.classify_init_error(None, "connection timed out"), "login_timeout")

    def test_code_minus_10005_is_local_ipc_not_broker_timeout(self):
        # Phase-4 WS-C: -10005 (RES_E_INTERNAL_FAIL_TIMEOUT) is the internal-IPC-timeout sibling of -10004 —
        # a LOCAL timeout, never a broker/login timeout. It must classify as validation_ipc_unavailable
        # (previously mislabelled login_timeout, which renders to the customer as a broker outage), regardless
        # of accompanying text, and must NEVER become a broker reason.
        for text in ("", "timeout", "connection timed out", "network"):
            self.assertEqual(vl.classify_init_error(-10005, text), "validation_ipc_unavailable", text)
            self.assertNotIn(vl.classify_init_error(-10005, text), {"login_timeout", "server_unavailable"}, text)

    def test_ambiguous_connection_text_is_conservative_not_broker_blame(self):
        # A bare connection/network token, no IPC marker and no broker-server evidence → could_not_verify
        # (never a false broker outage, never a credential blame).
        for text in ("connection reset", "network error", "connect failed"):
            self.assertEqual(vl.classify_init_error(None, text), "could_not_verify")
            self.assertNotEqual(vl.classify_init_error(None, text), "server_unavailable")

    def test_local_ipc_inputs_never_yield_server_unavailable(self):
        # Regression sweep: no local-IPC evidence may ever be classified as a broker outage.
        for code, text in [(-10004, "No IPC connection"), (-10004, ""), (None, "IPC timeout"),
                           (None, "no ipc connection"), (-10004, "network")]:
            self.assertNotEqual(vl.classify_init_error(code, text), "server_unavailable")
