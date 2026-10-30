"""ADR-0049 — signed-transport contract tests for the LIVE bridge provisioning ops. SimpleTestCase (no DB):
these exercise the real sign/verify/dispatch path with a fake nonce store + captured run_primitive, proving the
account-scoped envelope, the env-payload binding + allow-listing, and the MT5_ALLOW_LIVE posture gate are
enforced host-side without weakening the existing contract."""
import base64

from django.test import SimpleTestCase

from hosted_workspace import host_protocol as P
from hosted_workspace.host_agent_dispatch import OP_PRIMITIVES, dispatch

KR = {"k1": "hmac-secret-value-not-real-01"}
T = 2_000_000
ACTIVATE = "ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED"
RESTORE = "RESTORE_TENANT_BRIDGE_EXECUTION_OFF"

_LIVE_ENV = ("REM generated\r\nset MT5_ACCOUNT_ID=48\r\nset MT5_TERMINAL_PATH=C:\\GuvFX\\accounts\\48\\terminal\\terminal64.exe\r\n"
             "set HTTP_SERVER_PORT=8801\r\nset MT5_EXPECTED_LOGIN=5442\r\nset MT5_EXPECTED_SERVER=IS6Technologies-Live\r\n"
             "set MT5_REQUIRE_IDENTITY_PIN=1\r\nset MT5_GUARDED_ATTACH=1\r\nset MT5_ALLOW_LIVE=1\r\n")
_OFF_ENV = _LIVE_ENV.replace("set MT5_ALLOW_LIVE=1\r\n", "")


def _b64(text):
    return base64.b64encode(text.encode("ascii")).decode("ascii")


def _burner():
    seen = set()
    def burn(nonce, expiry):
        first = nonce not in seen
        seen.add(nonce)
        return first
    return burn


class _Capture:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "account_id": 48, "port": 8801, "allow_live": True,
                                 "require_pin": True, "guarded_attach": True, "env_sha256": "x", "listening": True}
    def __call__(self, primitive, args):
        self.calls.append((primitive, dict(args)))
        return dict(self.result)


def _req(*, operation=ACTIVATE, account_id=48, port=8801, nonce="n1", env=None, now=T):
    env = _LIVE_ENV if env is None else env
    return P.sign_hosted_request(account_id=account_id, operation=operation, correlation_id="c1",
                                 keyring=KR, key_id="k1", now=now, nonce=nonce,
                                 params={"port": port}, payload={"bridge_env_b64": _b64(env)})


def _dispatch(req, cap, now=T, burn=None):
    return dispatch(req, keyring=KR, now=now, nonce_burn=burn or _burner(), run_primitive=cap)


class LiveBridgeDispatchTests(SimpleTestCase):
    def test_op_primitives_cover_operations(self):
        self.assertEqual(set(OP_PRIMITIVES), set(P.HOSTED_OPERATIONS))
        self.assertEqual(OP_PRIMITIVES[ACTIVATE]["primitive"], "activate_tenant_bridge_live_authorized")
        self.assertEqual(OP_PRIMITIVES[RESTORE]["primitive"], "restore_tenant_bridge_execution_off")
        # existing demo activation untouched
        self.assertEqual(OP_PRIMITIVES["ACTIVATE_TENANT_BRIDGE"]["primitive"], "activate_tenant_bridge")

    def test_activate_round_trip_derives_server_args_and_passes_env(self):
        cap = _Capture()
        resp = _dispatch(_req(), cap)
        prim, args = cap.calls[0]
        self.assertEqual(prim, "activate_tenant_bridge_live_authorized")
        self.assertEqual(args["account_id"], 48)                       # server-derived from slot
        self.assertEqual(args["terminal_root"], r"C:\GuvFX\accounts\48\terminal")
        self.assertEqual(args["port"], 8801)
        self.assertEqual(args["bridge_env_b64"], _b64(_LIVE_ENV))      # validated env passed to the runner (stdin)
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce="n1", keyring=KR)
        self.assertTrue(out["ok"])

    def test_restore_round_trip(self):
        cap = _Capture({"ok": True, "account_id": 48, "port": 8801, "allow_live": False})
        resp = _dispatch(_req(operation=RESTORE, env=_OFF_ENV), cap)
        self.assertEqual(cap.calls[0][0], "restore_tenant_bridge_execution_off")
        P.verify_hosted_response(resp, correlation_id="c1", nonce="n1", keyring=KR)

    def test_customer_zero_refused(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(account_id=1), _Capture())
        self.assertEqual(cm.exception.reason_code, "reserved_identity")

    def test_port_out_of_range_refused(self):
        for bad in (8788, 8789, 8799, 8900):
            with self.assertRaises(P.HostProtocolError) as cm:
                _dispatch(_req(port=bad), _Capture())
            self.assertEqual(cm.exception.reason_code, "params_not_allowed")

    def test_extra_param_refused(self):
        req = P.sign_hosted_request(account_id=48, operation=ACTIVATE, correlation_id="c1", keyring=KR, key_id="k1",
                                    now=T, nonce="n2", params={"port": 8801, "evil": "x"},
                                    payload={"bridge_env_b64": _b64(_LIVE_ENV)})
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "params_not_allowed")

    def test_replay_refused(self):
        burn = _burner()
        _dispatch(_req(nonce="dup"), _Capture(), burn=burn)
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(nonce="dup"), _Capture(), burn=burn)
        self.assertEqual(cm.exception.reason_code, "nonce_replayed")

    def test_params_tamper_fails_digest(self):
        req = _req()
        req["params"] = {"port": 8899}                                 # edit after signing
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "params_digest_mismatch")

    def test_payload_tamper_fails_digest(self):
        req = _req()
        req["payload"] = {"bridge_env_b64": _b64(_OFF_ENV)}            # swap the env after signing
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_digest_mismatch")

    def test_bad_request_signature_fails(self):
        req = _req()
        req["signature"] = "0" * 64
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "bad_signature")

    def test_expired_request_fails(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(now=T), _Capture(), now=T + 10_000)
        self.assertIn(cm.exception.reason_code, ("request_expired", "timestamp_skew"))

    def test_bad_response_signature_detected(self):
        cap = _Capture()
        resp = _dispatch(_req(), cap)
        resp["signature"] = "0" * 64
        with self.assertRaises(P.HostProtocolError) as cm:
            P.verify_hosted_response(resp, correlation_id="c1", nonce="n1", keyring=KR)
        self.assertEqual(cm.exception.reason_code, "bad_response_signature")

    # --- the MT5_ALLOW_LIVE posture + arbitrary-key gate (the new defence) ---
    def test_activate_requires_allow_live_present(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(env=_OFF_ENV), _Capture())                  # LIVE op but env has no MT5_ALLOW_LIVE
        self.assertEqual(cm.exception.reason_code, "payload_not_live_authorized")

    def test_restore_refuses_residual_allow_live(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(operation=RESTORE, env=_LIVE_ENV), _Capture())   # OFF op but env still live
        self.assertEqual(cm.exception.reason_code, "payload_unexpected_live")

    def test_arbitrary_env_key_refused(self):
        bad = _LIVE_ENV + "set EVIL_KEY=1\r\n"
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(env=bad), _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_env_key_not_allowed")

    def test_env_value_cmd_metachar_refused(self):
        # A cmd metacharacter smuggled into an allow-listed value must be refused (would start a 2nd statement
        # when the bridge `call`s the .bat). LOW-1 hardening.
        bad = _LIVE_ENV.replace("set MT5_EXPECTED_SERVER=IS6Technologies-Live",
                                "set MT5_EXPECTED_SERVER=IS6 & set MT5_ALLOW_LIVE=1")
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(_req(env=bad), _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_env_value_unsafe")

    def test_missing_payload_refused(self):
        req = P.sign_hosted_request(account_id=48, operation=ACTIVATE, correlation_id="c1", keyring=KR, key_id="k1",
                                    now=T, nonce="n3", params={"port": 8801})   # no payload at all
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertEqual(cm.exception.reason_code, "payload_missing")

    def test_non_ascii_env_refused(self):
        req = _req()
        req["payload"] = {"bridge_env_b64": base64.b64encode("set MT5_ALLOW_LIVE=1\r\nset X=\u00e9\r\n".encode("utf-8")).decode("ascii")}
        # re-sign so the digest matches (we are testing the content gate, not the digest)
        req = P.sign_hosted_request(account_id=48, operation=ACTIVATE, correlation_id="c1", keyring=KR, key_id="k1",
                                    now=T, nonce="n4", params={"port": 8801}, payload=req["payload"])
        with self.assertRaises(P.HostProtocolError) as cm:
            _dispatch(req, _Capture())
        self.assertIn(cm.exception.reason_code, ("payload_not_ascii", "payload_malformed"))
