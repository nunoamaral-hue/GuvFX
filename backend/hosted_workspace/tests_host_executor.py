"""Beta Readiness Stream 5 — host-side dispatcher (host_agent_dispatch) + Django SignedHostExecutor.

Proves the no-RCE narrow contract end to end: the host derives identity/paths server-side (no caller path),
refuses Customer Zero at BOTH layers, maps each allow-listed op to exactly one primitive, opens the sealed
password without echoing it, and fails closed on host-unavailable / malformed / forged responses. Pure, no host.
"""
import json
import os

from django.test import SimpleTestCase, override_settings

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace.host_executor import SignedHostExecutor, resolve_signed_host_executor
from hosted_workspace.workspace_acl import build_workspace_acl_plan

KR = {"k1": "dispatch-secret"}
T = 2_000_000


def _burner():
    seen = set()

    def burn(n, e):
        first = n not in seen
        seen.add(n)
        return first
    return burn


def _req(op, account_id=14, params=None, payload=None, key_id="k1"):
    return P.sign_hosted_request(account_id=account_id, operation=op, correlation_id="c1", keyring=KR,
                                 key_id=key_id, now=T, params=params, nonce=None, payload=payload)


class DispatchTests(SimpleTestCase):
    def _run(self, op, reserved_ids="", **kw):
        calls = []

        def run_primitive(name, args):
            calls.append((name, args))
            return {"ok": True, "primitive": name}

        def envelope_open(payload, *, account_id, correlation_id, nonce):
            return "OPENED_PW"

        resp = D.dispatch(_req(op, **kw), keyring=KR, now=T, nonce_burn=_burner(),
                          run_primitive=run_primitive, reserved_ids=reserved_ids, envelope_open=envelope_open)
        return calls, resp

    def test_maps_op_to_primitive_with_derived_paths(self):
        calls, resp = self._run("VERIFY_SLOT", account_id=14)
        self.assertEqual(calls[0][0], "verify_slot")
        self.assertEqual(calls[0][1]["username"], "guvfx_u_14")
        self.assertEqual(calls[0][1]["runtime_root"], r"C:\GuvFX\accounts\14")
        # response is signed + verifies
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce=resp["nonce"], keyring=KR)
        self.assertTrue(out["ok"])

    def test_customer_zero_refused_host_side(self):
        # With the default reserved set (None → {1}), account #1 is refused host-side.
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run("VERIFY_SLOT", account_id=1, reserved_ids=None)
        self.assertEqual(cm.exception.reason_code, "reserved_identity")

    def test_reserved_fail_safe_on_garbled_override(self):
        # A garbled reserved override must still protect account #1.
        self.assertEqual(D.reserved_ids_from("none"), frozenset({1}))
        self.assertEqual(D.reserved_ids_from(None), frozenset({1}))
        self.assertEqual(D.reserved_ids_from(""), frozenset())
        self.assertEqual(D.reserved_ids_from("1 2 5"), frozenset({1, 2, 5}))
        # Customer Zero is a HARD FLOOR: a partial-garble override that omits 1 still protects it.
        self.assertEqual(D.reserved_ids_from("1o 2"), frozenset({1, 2}))
        self.assertEqual(D.reserved_ids_from("2 3"), frozenset({1, 2, 3}))

    def test_params_not_allowed_blocks_smuggling(self):
        # There is no path field on the wire; even a smuggled scalar param is refused (empty allow-list).
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run("VERIFY_SLOT", account_id=14, params={"runtime_root": r"C:\evil"})
        self.assertEqual(cm.exception.reason_code, "params_not_allowed")

    def test_provision_identity_opens_but_never_echoes_password(self):
        payload = {"epk": "x", "ct": "sealed", "nonce": "n", "v": 1}
        calls, resp = self._run("PROVISION_IDENTITY", account_id=14, payload=payload)
        # the opened password reached the primitive…
        self.assertEqual(calls[0][1]["password"], "OPENED_PW")
        # …but is NOT anywhere in the signed response over the wire.
        self.assertNotIn("OPENED_PW", json.dumps(resp))

    def test_result_secret_keys_stripped(self):
        def run_primitive(name, args):
            return {"ok": True, "password": "LEAK", "payload": {"x": 1}}

        resp = D.dispatch(_req("VERIFY_SLOT"), keyring=KR, now=T, nonce_burn=_burner(),
                          run_primitive=run_primitive, reserved_ids="")
        self.assertNotIn("LEAK", json.dumps(resp))

    def test_derive_slot_is_the_only_path_source(self):
        s = D.derive_slot(7)
        self.assertEqual(s["username"], "guvfx_u_7")
        self.assertEqual(s["runtime_root"], r"C:\GuvFX\accounts\7")
        self.assertEqual(s["terminal_root"], r"C:\GuvFX\accounts\7\terminal")
        with self.assertRaises(P.HostProtocolError):
            D.derive_slot(0)

    def test_only_known_primitives(self):
        self.assertTrue(D.is_known_primitive("apply_workspace_acl"))
        self.assertFalse(D.is_known_primitive("cmd.exe"))
        self.assertFalse(D.is_known_primitive("apply_workspace_acl; rm -rf"))

    def test_op_primitive_map_covers_all_ops(self):
        self.assertEqual(set(D.OP_PRIMITIVES), set(P.HOSTED_OPERATIONS))


def _signing_transport(result_by_op):
    """A fake host transport that verifies the request and returns a correctly-signed response for the op."""
    def transport(base_url, request):
        op = request["operation"]
        result = dict(result_by_op.get(op, {"ok": True}))
        return P.sign_hosted_response(result=result, correlation_id=request["correlation_id"],
                                      nonce=request["nonce"], keyring=KR, key_id="k1")
    return transport


def _fake_seal(pw_bytes, *, account_id, correlation_id, nonce):
    # A stand-in envelope that does NOT contain the plaintext (proves the executor sends only the sealed form).
    return {"v": 1, "epk": "PUB", "ct": "OPAQUE-CIPHERTEXT", "nonce": nonce}


def _executor(account_id=14, result_by_op=None):
    return SignedHostExecutor(
        account_id=account_id, rdp_host="10.0.0.9", transport=_signing_transport(result_by_op or {}),
        keyring=KR, key_id="k1", base_url="https://host.invalid", seal_password=_fake_seal,
        reserved_ids="", clock=lambda: T)


class SignedHostExecutorTests(SimpleTestCase):
    def test_apply_workspace_acl_maps_and_returns_readback(self):
        rows = [{"sid": "S-1-5-18", "type": "Allow", "rights": "FullControl", "inherited": False}]
        ex = _executor(result_by_op={"APPLY_WORKSPACE_ACL": {"ok": True, "rows": rows,
                                                             "user_sid": "S-1-5-21-x", "protected": True}})
        plan = build_workspace_acl_plan(r"C:\GuvFX\accounts\14", "guvfx_u_14")
        res = ex.apply_workspace_acl(plan)
        self.assertTrue(res["ok"])
        self.assertEqual(res["rows"], rows)
        self.assertTrue(res["protected"])

    def test_materialise_identity_seals_and_never_transmits_plaintext(self):
        captured = {}

        def transport(base_url, request):
            captured["req"] = request
            return P.sign_hosted_response(result={"ok": True}, correlation_id=request["correlation_id"],
                                          nonce=request["nonce"], keyring=KR, key_id="k1")
        ex = SignedHostExecutor(account_id=14, rdp_host="10.0.0.9", transport=transport, keyring=KR,
                                key_id="k1", base_url="x", seal_password=_fake_seal, reserved_ids="",
                                clock=lambda: T)
        spec = {"account_id": 14, "windows_username": "guvfx_u_14",
                "runtime_root": r"C:\GuvFX\accounts\14", "password": "SUPERSECRETpw!"}
        res = ex.materialise_identity(spec)
        self.assertTrue(res["ok"])
        blob = json.dumps(captured["req"])
        self.assertNotIn("SUPERSECRETpw!", blob)          # plaintext never on the wire
        self.assertEqual(captured["req"]["operation"], "PROVISION_IDENTITY")
        self.assertIn("payload", captured["req"])          # sealed envelope carried

    def test_confinement_mismatch_does_not_send(self):
        sent = {"n": 0}

        def transport(base_url, request):
            sent["n"] += 1
            return {}
        ex = SignedHostExecutor(account_id=14, rdp_host="x", transport=transport, keyring=KR, key_id="k1",
                                base_url="x", seal_password=_fake_seal, reserved_ids="", clock=lambda: T)
        res = ex.grant_rdp("guvfx_u_99")                   # wrong identity for account 14
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "confinement_mismatch")
        self.assertEqual(sent["n"], 0)                     # nothing sent

    def test_customer_zero_executor_refuses(self):
        ex = SignedHostExecutor(account_id=1, rdp_host="x", transport=_signing_transport({}), keyring=KR,
                                key_id="k1", base_url="x", seal_password=_fake_seal, reserved_ids=None,
                                clock=lambda: T)
        self.assertEqual(ex.enforce_single_session()["reason"], "reserved_identity")

    def test_host_unavailable_fails_closed(self):
        def boom(base_url, request):
            raise OSError("connection refused")
        ex = SignedHostExecutor(account_id=14, rdp_host="x", transport=boom, keyring=KR, key_id="k1",
                                base_url="x", seal_password=_fake_seal, reserved_ids="", clock=lambda: T)
        res = ex.verify_slot()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "host_unavailable")

    def test_forged_response_fails_closed(self):
        def transport(base_url, request):
            resp = P.sign_hosted_response(result={"ok": True}, correlation_id=request["correlation_id"],
                                          nonce=request["nonce"], keyring=KR, key_id="k1")
            resp["result"]["ok"] = False                   # tamper after signing
            return resp
        ex = SignedHostExecutor(account_id=14, rdp_host="x", transport=transport, keyring=KR, key_id="k1",
                                base_url="x", seal_password=_fake_seal, reserved_ids="", clock=lambda: T)
        res = ex.verify_slot()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "bad_response_signature")


class ResolveDarkTests(SimpleTestCase):
    def test_dark_when_flag_off(self):
        self.assertIsNone(resolve_signed_host_executor(account_id=14))

    @override_settings(HOSTED_HOST_EXECUTOR_ENABLED="1")
    def test_dark_when_unconfigured_even_if_flagged(self):
        # flag on but no keyring/base_url → still None (fail closed, never half-armed)
        with _clean_env("HOSTED_EXECUTOR_KEYRING", "HOSTED_EXECUTOR_KEY_ID", "HOSTED_EXECUTOR_BASE_URL"):
            self.assertIsNone(resolve_signed_host_executor(account_id=14))

    @override_settings(HOSTED_HOST_EXECUTOR_ENABLED="1",
                       HOSTED_EXECUTOR_KEYRING='{"k1": "s"}', HOSTED_EXECUTOR_KEY_ID="k1",
                       HOSTED_EXECUTOR_BASE_URL="https://host.invalid")
    def test_builds_executor_when_fully_configured(self):
        ex = resolve_signed_host_executor(account_id=14, rdp_host="10.0.0.9")
        self.assertIsInstance(ex, SignedHostExecutor)
        self.assertEqual(ex.account_id, 14)


class _clean_env:
    def __init__(self, *names):
        self.names = names
        self.saved = {}

    def __enter__(self):
        for n in self.names:
            self.saved[n] = os.environ.pop(n, None)

    def __exit__(self, *a):
        for n, v in self.saved.items():
            if v is not None:
                os.environ[n] = v


class RemoteAppAliasTests(SimpleTestCase):
    """Stream 6 (M2): the per-account RemoteApp alias — the single server-derived source of truth."""

    def test_customer_zero_keeps_legacy_alias(self):
        self.assertEqual(D.remoteapp_alias(1), "terminal64")

    def test_per_account_alias_is_deterministic_and_unique(self):
        self.assertEqual(D.remoteapp_alias(2), "guvfx_mt5_2")
        self.assertEqual(D.remoteapp_alias(100), "guvfx_mt5_100")
        aliases = {D.remoteapp_alias(n) for n in (1, 2, 3, 10, 100)}
        self.assertEqual(len(aliases), 5)                      # one unique alias per account

    def test_bad_account_rejected(self):
        for bad in (0, -1):
            with self.assertRaises(P.HostProtocolError):
                D.remoteapp_alias(bad)

    def test_dispatch_derives_alias_server_side(self):
        # ENSURE_REMOTEAPP for account 2 must carry the SERVER-derived guvfx_mt5_2 (never a caller value).
        calls = []
        D.dispatch(_req("ENSURE_REMOTEAPP", account_id=2), keyring=KR, now=T, nonce_burn=_burner(),
                   run_primitive=lambda name, args: calls.append((name, args)) or {"ok": True}, reserved_ids="")
        self.assertEqual(calls[0][0], "ensure_remoteapp")
        self.assertEqual(calls[0][1]["alias"], "guvfx_mt5_2")
        self.assertEqual(calls[0][1]["terminal_root"], r"C:\GuvFX\accounts\2\terminal")

    def test_dispatch_applocker_maps_to_tenant_merge(self):
        calls = []
        D.dispatch(_req("APPLY_APPLOCKER_AUDIT", account_id=7), keyring=KR, now=T, nonce_burn=_burner(),
                   run_primitive=lambda name, args: calls.append((name, args)) or {"ok": True}, reserved_ids="")
        self.assertEqual(calls[0][0], "applocker_tenant_merge")
        self.assertEqual(calls[0][1], {"username": "guvfx_u_7", "account_id": 7})


class NewScriptHygieneTests(SimpleTestCase):
    def test_host_scripts_are_ascii_only(self):
        import hosted_workspace
        base = os.path.join(os.path.dirname(os.path.dirname(hosted_workspace.__file__)),
                            "terminal_provisioning", "windows")
        for name in ["Set-GuvfxAutoTradingConfig.ps1", "Set-GuvfxRemoteApp.ps1", "Set-GuvfxObserver.ps1",
                     "Set-GuvfxAppLockerTenant.ps1"]:
            data = open(os.path.join(base, name), "rb").read()
            self.assertEqual([i for i, b in enumerate(data) if b > 127], [], f"{name}: non-ASCII")


class _FakeResp:
    def __init__(self, body=b'{"ok": true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class HttpTransportTimeoutTests(SimpleTestCase):
    """Stream 7D pre-flight: MATERIALISE_RUNTIME (~378MB golden copy) must get a read timeout longer than the
    host's 600s primitive timeout so a slow copy is not falsely reported as host-unavailable; every other op
    keeps the short default. There is no repost/retry loop, so a longer wait can never re-run the op."""

    def _timeout_for(self, operation):
        from unittest import mock
        from hosted_workspace import host_executor as HE
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["timeout"] = timeout
            return _FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            HE._http_transport("http://host:8790", {"operation": operation})
        return seen["timeout"]

    def test_materialise_gets_long_timeout(self):
        from hosted_workspace import host_executor as HE
        self.assertEqual(self._timeout_for("MATERIALISE_RUNTIME"), HE._OP_HTTP_TIMEOUTS_S["MATERIALISE_RUNTIME"])
        self.assertGreater(self._timeout_for("MATERIALISE_RUNTIME"), 600)

    def test_other_ops_get_default_timeout(self):
        from hosted_workspace import host_executor as HE
        for op in ("APPLY_WORKSPACE_ACL", "PROVISION_IDENTITY", "VERIFY_SLOT", ""):
            self.assertEqual(self._timeout_for(op), HE._DEFAULT_HTTP_TIMEOUT_S)
