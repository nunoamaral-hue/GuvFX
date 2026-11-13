"""ADR-0104 - the INDEPENDENT host-emitter one-shot identity-attestation TRIGGER, end to end (repository checks;
no host, no PowerShell, no secret).

Proves the narrow, fail-closed contract for ``TRIGGER_EMITTER_ONESHOT_ATTEST``:
  * the signed op is wired across ALL FOUR layers (host_protocol / host_agent_dispatch / primitive_runner +
    .ps1 / stage-manifest), carries NO credential and NO caller params, and the host derives identity server-side;
  * Customer Zero is refused at the dispatcher AND (defence in depth) in the .ps1, which ALSO re-pins the sole
    emitter account (46) and ALWAYS re-disables the one-shot task in a finally (fail-closed re-disable);
  * the Django executor method mirrors ``observe()`` (no confinement args), pins account 46 before any host
    contact, makes EXACTLY ONE host round-trip (no retry) and fails closed on any transport/host fault;
  * the DARK flag defaults OFF and is ANDed with the master gate;
  * the operator command refuses (no host contact) when the flag is OFF / the account is not 46 / the executor is
    unconfigured, is dry-run by default, and on --apply invokes the executor exactly once with a sanitised result;
  * the non-interactive task entrypoint reads the ingest secret ONLY from a host-local ephemeral key file (absent
    => SECRET_UNAVAILABLE, no attest, no POST), attests + POSTs EXACTLY ONCE, writes a SANITISED result (no
    envelope/secret), and shreds the key file after one use.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P
from hosted_workspace.host_executor import SignedHostExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
_EMITTER_BUNDLE = os.path.join(_REPO, "deploy", "copier-emitter")
for _p in (_BUNDLE, _LIB, _EMITTER_BUNDLE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import primitive_runner as pr  # noqa: E402
import run_oneshot_attest as roa  # noqa: E402

_WINDOWS_SCRIPTS = os.path.join(_REPO, "backend", "terminal_provisioning", "windows")
_PRIM = os.path.join(_WINDOWS_SCRIPTS, "Trigger-GuvfxCopierEmitterOneShot.ps1")
_STAGE_MANIFEST = os.path.join(_BUNDLE, "stage-manifest.json")

_OP = "TRIGGER_EMITTER_ONESHOT_ATTEST"
_PRIMITIVE = "trigger_emitter_oneshot_attest"
_SCRIPT = "Trigger-GuvfxCopierEmitterOneShot.ps1"

KR = {"k1": "oneshot-secret"}
T = 4_000_000


def _burner():
    seen = set()

    def burn(n, e):
        first = n not in seen
        seen.add(n)
        return first
    return burn


def _req(op, account_id=46, params=None, key_id="k1"):
    return P.sign_hosted_request(account_id=account_id, operation=op, correlation_id="c1", keyring=KR,
                                 key_id=key_id, now=T, params=params, nonce=None)


def _strip_ps_comments(src: str) -> str:
    """Return the .ps1 with block ``<# .. #>`` and line ``#`` comments removed, so a static scan sees CODE only
    (a '#' inside a quoted string is preserved)."""
    src = re.sub(r"<#.*?#>", "", src, flags=re.DOTALL)
    out = []
    for line in src.splitlines():
        res, quote, i = [], None, 0
        while i < len(line):
            ch = line[i]
            if quote:
                res.append(ch)
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                res.append(ch)
            elif ch == "#":
                break
            else:
                res.append(ch)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


# ─────────────────────────────── Phase 1: four-layer signed-op parity ───────────────────────────────

class OneShotAllowlistParity(SimpleTestCase):
    """A host op is only real when wired across ALL FOUR layers + the stage manifest (memory: catalogue_v1)."""

    def test_wired_across_all_four_layers(self):
        self.assertIn(_OP, P.HOSTED_OPERATIONS)
        self.assertIn(_OP, D.OP_PRIMITIVES)
        self.assertEqual(D.OP_PRIMITIVES[_OP]["primitive"], _PRIMITIVE)
        self.assertIn(_PRIMITIVE, pr.CONTRACT)
        self.assertEqual(pr.CONTRACT[_PRIMITIVE].script, _SCRIPT)
        self.assertTrue(os.path.isfile(_PRIM))
        with open(_STAGE_MANIFEST) as fh:
            self.assertIn(_SCRIPT, json.load(fh)["scripts"])

    def test_op_carries_no_credential_and_no_params(self):
        self.assertNotIn(_OP, P.CREDENTIALED_HOSTED_OPERATIONS)
        self.assertNotIn(_OP, P.ACCOUNT_INDEPENDENT_OPERATIONS)
        self.assertEqual(D.OP_PRIMITIVES[_OP]["params_allow"], ())

    def test_op_primitives_still_covers_hosted_operations(self):
        self.assertEqual(set(D.OP_PRIMITIVES), set(P.HOSTED_OPERATIONS))
        self.assertEqual(set(pr.CONTRACT), {v["primitive"] for v in D.OP_PRIMITIVES.values()})


class OneShotProtocolSign(SimpleTestCase):
    def test_sign_verify_round_trip_account46(self):
        req = _req(_OP, account_id=46)
        out = P.verify_hosted_request(req, keyring=KR, now=T, nonce_burn=_burner())
        self.assertEqual(out["operation"], _OP)
        self.assertEqual(out["account_id"], 46)


# ─────────────────────────────── Phase 1: host-side dispatch ───────────────────────────────

class OneShotDispatch(SimpleTestCase):
    def _run(self, op, reserved_ids="", **kw):
        calls = []

        def run_primitive(name, args):
            calls.append((name, args))
            return {"ok": True, "primitive": name, "verified": True, "http_status": 200}

        resp = D.dispatch(_req(op, **kw), keyring=KR, now=T, nonce_burn=_burner(),
                          run_primitive=run_primitive, reserved_ids=reserved_ids)
        return calls, resp

    def test_maps_to_primitive_with_server_derived_identity(self):
        calls, resp = self._run(_OP, account_id=46)
        self.assertEqual(calls[0][0], _PRIMITIVE)
        self.assertEqual(calls[0][1]["username"], "guvfx_u_46")
        self.assertEqual(calls[0][1]["runtime_root"], r"C:\GuvFX\accounts\46")
        self.assertEqual(calls[0][1]["terminal_root"], r"C:\GuvFX\accounts\46\terminal")
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce=resp["nonce"], keyring=KR)
        self.assertTrue(out["ok"])

    def test_customer_zero_refused_host_side(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run(_OP, account_id=1, reserved_ids=None)
        self.assertEqual(cm.exception.reason_code, "reserved_identity")

    def test_no_caller_param_smuggling(self):
        for bad in ({"account_id": 48}, {"verifier_url": "http://evil"}, {"key_file": "x"}, {"script": "y"}):
            with self.assertRaises(P.HostProtocolError) as cm:
                self._run(_OP, account_id=46, params=bad)
            self.assertEqual(cm.exception.reason_code, "params_not_allowed")


# ─────────────────────────────── Phase 1: Django signed executor ───────────────────────────────

def _signing_transport(result_by_op, *, calls=None):
    def transport(base_url, request):
        if calls is not None:
            calls.append(request["operation"])
        result = dict(result_by_op.get(request["operation"], {"ok": True}))
        return P.sign_hosted_response(result=result, correlation_id=request["correlation_id"],
                                      nonce=request["nonce"], keyring=KR, key_id="k1")
    return transport


def _fake_seal(pw_bytes, *, account_id, correlation_id, nonce):
    return {"v": 1, "epk": "PUB", "ct": "OPAQUE", "nonce": nonce}


def _executor(account_id=46, result_by_op=None, transport=None, reserved_ids=""):
    return SignedHostExecutor(
        account_id=account_id, rdp_host="", transport=transport or _signing_transport(result_by_op or {}),
        keyring=KR, key_id="k1", base_url="https://host.invalid", seal_password=_fake_seal,
        reserved_ids=reserved_ids, clock=lambda: T)


class OneShotExecutor(SimpleTestCase):
    def test_sends_op_and_returns_verified_result(self):
        calls = []
        ex = _executor(account_id=46, transport=_signing_transport(
            {_OP: {"ok": True, "verified": True, "http_status": 200, "reason": "verified"}}, calls=calls))
        res = ex.trigger_emitter_oneshot_attest()
        self.assertTrue(res["ok"])
        self.assertTrue(res["verified"])
        self.assertEqual(res["http_status"], 200)
        self.assertEqual(calls, [_OP])          # exactly one host round-trip (no retry)

    def test_pins_account_46_without_contacting_host(self):
        calls = []
        ex = _executor(account_id=47, transport=_signing_transport({}, calls=calls))
        res = ex.trigger_emitter_oneshot_attest()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "account_not_emitter_pinned")
        self.assertEqual(calls, [])              # refused BEFORE any transport call

    def test_reserved_zero_refused_without_contacting_host(self):
        # A Customer-Zero executor (reserved default) fails closed in _send. (46 != 1, but prove the reserved
        # guard independently with the account-46 pin lifted to account 1 would never pass the pin; so assert the
        # reserved path via the pinned account's _send using a reserved override that includes 46.)
        calls = []
        ex = _executor(account_id=46, transport=_signing_transport({}, calls=calls), reserved_ids="46")
        res = ex.trigger_emitter_oneshot_attest()
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "reserved_identity")
        self.assertEqual(calls, [])

    def test_fails_closed_on_transport_error(self):
        def boom(base_url, request):
            raise ConnectionError("host down")
        ex = _executor(account_id=46, transport=boom)
        res = ex.trigger_emitter_oneshot_attest()
        self.assertFalse(res["ok"])
        self.assertNotIn("envelope", res)
        self.assertNotIn("signature", res)

    def test_fails_closed_on_forged_response_signature(self):
        def forged(base_url, request):
            r = P.sign_hosted_response(result={"ok": True}, correlation_id=request["correlation_id"],
                                       nonce=request["nonce"], keyring=KR, key_id="k1")
            r["signature"] = "0" * 64                     # tamper
            return r
        ex = _executor(account_id=46, transport=forged)
        self.assertFalse(ex.trigger_emitter_oneshot_attest()["ok"])


# ─────────────────────────────── Phase 1: primitive runner argv ───────────────────────────────

class OneShotRunnerArgv(unittest.TestCase):
    def test_injects_accountid_from_username_and_no_stdin(self):
        calls = []

        def fake_run(argv, *, input_bytes, timeout_s):
            calls.append({"argv": list(argv), "input": input_bytes})
            class _P:
                stdout = b'{"ok":true,"account_id":46,"verified":true,"http_status":200}'
                stderr = b""
                returncode = 0
            return _P()

        runner = pr.PrimitiveRunner(scripts_dir="/scripts", powershell="powershell",
                                    run_subprocess=fake_run, parse_validator=lambda p: (True, "ok"))
        runner.run(_PRIMITIVE, {"username": "guvfx_u_46", "runtime_root": r"C:\GuvFX\accounts\46",
                                "terminal_root": r"C:\GuvFX\accounts\46\terminal"})
        argv = calls[0]["argv"]
        self.assertIn(_SCRIPT, argv[argv.index("-File") + 1])
        self.assertEqual(argv[argv.index("-Username") + 1], "guvfx_u_46")
        self.assertEqual(argv[argv.index("-RuntimeRoot") + 1], r"C:\GuvFX\accounts\46")
        self.assertEqual(argv[argv.index("-TerminalRoot") + 1], r"C:\GuvFX\accounts\46\terminal")
        self.assertEqual(argv[argv.index("-AccountId") + 1], "46")      # injected from username
        self.assertEqual(calls[0]["input"], b"")                        # no stdin: the secret is host-local

    def test_required_scripts_includes_new_script_and_is_known(self):
        runner = pr.PrimitiveRunner(scripts_dir="/scripts")
        self.assertIn(_SCRIPT, runner.required_scripts())
        self.assertTrue(D.is_known_primitive(_PRIMITIVE))


# ─────────────────────────────── Phase 1b: DARK flag ───────────────────────────────

class OneShotFlag(SimpleTestCase):
    @override_settings(COPIER_ENABLED="", COPIER_EMITTER_ONESHOT_ENABLED="")
    def test_default_off(self):
        from trade_copier.flags import copier_emitter_oneshot_enabled
        self.assertFalse(copier_emitter_oneshot_enabled())

    def test_requires_both_master_and_own(self):
        from trade_copier.flags import copier_emitter_oneshot_enabled
        with override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED=""):
            self.assertFalse(copier_emitter_oneshot_enabled())         # own off
        with override_settings(COPIER_ENABLED="", COPIER_EMITTER_ONESHOT_ENABLED="1"):
            self.assertFalse(copier_emitter_oneshot_enabled())         # master off
        with override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED="1"):
            self.assertTrue(copier_emitter_oneshot_enabled())          # both on


# ─────────────────────────────── Phase 3: operator command ───────────────────────────────

class OneShotCommand(SimpleTestCase):
    def _call(self, **kw):
        out = StringIO()
        call_command("request_copier_emitter_oneshot_attest", stdout=out, **kw)
        return json.loads(out.getvalue())

    @override_settings(COPIER_ENABLED="", COPIER_EMITTER_ONESHOT_ENABLED="")
    def test_refused_when_flag_off_no_host_contact(self):
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor") as rse:
            res = self._call(apply=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "copier_emitter_oneshot_disabled")
        self.assertFalse(res["host_contacted"])
        rse.assert_not_called()

    def test_refused_when_account_not_46(self):
        with override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED="1"):
            with self.assertRaises(CommandError):
                self._call(account=47, apply=True)

    @override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED="1")
    def test_dryrun_makes_no_host_contact_even_when_flag_on(self):
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor") as rse:
            res = self._call()                      # no --apply
        self.assertTrue(res["plan"])
        self.assertFalse(res["host_contacted"])
        rse.assert_not_called()

    @override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED="1")
    def test_executor_dark_refused_no_host_contact(self):
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor", return_value=None):
            res = self._call(apply=True)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "host_executor_unavailable")
        self.assertFalse(res["host_contacted"])

    @override_settings(COPIER_ENABLED="1", COPIER_EMITTER_ONESHOT_ENABLED="1")
    def test_apply_invokes_executor_once_and_sanitises(self):
        fake = mock.Mock()
        fake.trigger_emitter_oneshot_attest.return_value = {
            "ok": True, "verified": True, "http_status": 200, "reason": "verified",
            "envelope": {"secret": "LEAK"}, "signature": "LEAK"}      # must be stripped by the command
        with mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor", return_value=fake):
            res = self._call(apply=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["host_contacted"])
        fake.trigger_emitter_oneshot_attest.assert_called_once_with()
        self.assertNotIn("envelope", res["result"])
        self.assertNotIn("signature", res["result"])
        self.assertEqual(res["result"]["http_status"], 200)


# ─────────────────────────────── Phase 2: Windows primitive static (RULE 9/11) ───────────────────────────────

class OneShotPrimitiveScriptStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_PRIM, encoding="ascii") as fh:          # encoding="ascii" itself asserts ASCII-only (RULE 9)
            cls.src = fh.read()
        cls.code = _strip_ps_comments(cls.src)

    def test_is_ascii_only(self):
        self.src.encode("ascii")

    def test_guards_present_and_precede_enable(self):
        s = self.src
        self.assertIn('$EMITTER_ONESHOT_ACCOUNT = 46', s)
        self.assertIn('if ($AccountId -le 0 -or $AccountId -eq 1) { Emit "reserved_or_invalid_account" }', s)
        self.assertIn('if ($AccountId -ne $EMITTER_ONESHOT_ACCOUNT) { Emit "account_not_emitter_pinned" }', s)
        self.assertIn('$expectedUser = "guvfx_u_$AccountId"', s)
        self.assertIn('if ($Username -ne $expectedUser)', s)
        self.assertIn('if ($currentXml -notmatch "<Enabled>false</Enabled>") { Emit "oneshot_task_not_disabled" }', s)
        i_cz = s.index('Emit "reserved_or_invalid_account"')
        i_pin = s.index('Emit "account_not_emitter_pinned"')
        i_user = s.index('if ($Username -ne $expectedUser)')
        i_enable = s.index("/enable 2>$null")
        self.assertLess(i_cz, i_enable)
        self.assertLess(i_pin, i_enable)
        self.assertLess(i_user, i_enable)

    def test_action_trust_pins_entrypoint_and_attest_route(self):
        s = self.src
        self.assertIn("run_oneshot_attest\\.py", s)
        self.assertIn("attest-verify", s)
        self.assertIn('-notmatch "event-ingest"', s)         # never the event-ingest route

    def test_redisable_in_finally_failclosed_and_no_exit_in_enable_region(self):
        s = self.src
        i_enable = s.index("/enable 2>$null")
        i_finally = s.index("finally {", i_enable)
        i_disable = s.index("/disable 2>$null", i_finally)
        i_emit_final = s.rindex('Emit ""')
        # re-disable happens inside the finally, after it, and is verified locale-independently + fail-closed
        self.assertLess(i_finally, i_disable)
        self.assertIn('else { $v.task_redisabled = $false; $v.ok = $false; $v.reason = "redisable_failed" }', s)
        self.assertLess(i_disable, s.index('redisable_failed'))
        # the final single Emit runs AFTER the finally (so the task is always re-disabled before the verdict)
        self.assertLess(i_finally, i_emit_final)
        # CRITICAL: no Emit() (which calls exit, skipping the finally) anywhere in the enable->finally region
        region = s[i_enable:i_finally]
        self.assertNotIn("Emit ", region, "no Emit/exit may occur in the enable region (would skip the re-disable)")

    def test_no_envelope_or_secret_token_in_code(self):
        low = self.code.lower()
        for tok in ("envelope", "signature", "secret", "ingest_key", "hmac"):
            self.assertNotIn(tok, low, f"the primitive must never handle {tok} (it only reads a sanitised result)")

    def test_static_scan_has_positive_control(self):
        """RULE 11: the comment-stripping measurement path must FIRE on known-bad input, or a degraded strip helper
        would make every 'token absent' scan pass vacuously."""
        bad = ('# comment only\n$x = "keep"\n$secret = "LEAK"   # inline comment\n')
        stripped = _strip_ps_comments(bad)
        self.assertIn("secret", stripped.lower())            # the scan FIRES on real code
        self.assertIn('$x = "keep"', stripped)               # real code survives
        self.assertNotIn("inline comment", stripped)         # comments removed


# ─────────────────────────────── Phase 2: non-interactive task entrypoint ───────────────────────────────

class _FakeApp:
    def __init__(self, attest_out):
        self._out = attest_out

    def attest(self):
        return self._out


class OneShotEntrypoint(unittest.TestCase):
    def _args(self, tmp, key_present, account_id=46):
        cfg = os.path.join(tmp, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"account_id": account_id, "ingest_key_env": "COPIER_INGEST_HMAC_KEY"}, fh)
        key = os.path.join(tmp, "ingest.key")
        if key_present:
            with open(key, "w") as fh:
                fh.write("k" * 40)                           # >= 32 chars: a usable host-local secret
        result = os.path.join(tmp, "out", "attest_result.json")
        return ["--config", cfg, "--verifier-url", "https://api.guvfx.com/api/copy-trading/attest-verify/",
                "--key-file", key, "--result-file", result], result, key

    def test_secret_unavailable_when_no_key_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, result, key = self._args(tmp, key_present=False)
            posts = []
            rc = roa.main(argv, env={}, transport=lambda u, b: posts.append((u, b)) or (200, True, "verified"),
                          app_factory=lambda c, e: _FakeApp({"ok": True, "envelope": {"signed": {}, "signature": "x"}}))
            self.assertEqual(rc, 1)
            out = json.load(open(result))
            self.assertEqual(out["reason"], "SECRET_UNAVAILABLE")
            self.assertFalse(out["posted"])
            self.assertFalse(out["verified"])
            self.assertEqual(posts, [])                      # nothing attested, nothing posted
            self.assertNotIn("envelope", out)

    def test_attest_and_post_happy_path_shreds_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv, result, key = self._args(tmp, key_present=True)
            posts = []

            def transport(url, body):
                posts.append((url, json.loads(body.decode("utf-8"))))
                return 200, True, "verified"

            app = _FakeApp({"ok": True, "observed_identity_probed": True,
                            "envelope": {"signed": {"account_id": 46}, "signature": "sig"}})
            rc = roa.main(argv, env={}, transport=transport, app_factory=lambda c, e: app)
            self.assertEqual(rc, 0)
            out = json.load(open(result))
            self.assertTrue(out["ok"])
            self.assertTrue(out["verified"])
            self.assertTrue(out["posted"])
            self.assertEqual(out["account_id"], 46)
            self.assertNotIn("envelope", out)                # sanitised result
            self.assertNotIn("signature", out)
            self.assertEqual(len(posts), 1)                  # exactly one POST, no retry
            self.assertEqual(posts[0][1], {"signed": {"account_id": 46}, "signature": "sig"})  # the signed envelope only
            self.assertFalse(os.path.exists(key), "the ephemeral key file must be shredded after one use")

    def test_account_pin_and_no_post_on_attest_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            # (a) a config pinned to the wrong account is refused before any attest/post
            argv, result, key = self._args(tmp, key_present=True, account_id=47)
            posts = []
            roa.main(argv, env={}, transport=lambda u, b: posts.append(1) or (200, True, "verified"),
                     app_factory=lambda c, e: _FakeApp({"ok": True, "envelope": {"signed": {}, "signature": "x"}}))
            self.assertEqual(json.load(open(result))["reason"], "account_not_emitter_pinned")
            self.assertEqual(posts, [])
        with tempfile.TemporaryDirectory() as tmp:
            # (b) attest produced no usable envelope -> NO post; key-unavailable reason normalises to SECRET_UNAVAILABLE
            argv, result, key = self._args(tmp, key_present=True)
            posts = []
            roa.main(argv, env={}, transport=lambda u, b: posts.append(1) or (200, True, "verified"),
                     app_factory=lambda c, e: _FakeApp({"ok": False, "reason": "attestation_key_unavailable"}))
            out = json.load(open(result))
            self.assertEqual(out["reason"], "SECRET_UNAVAILABLE")
            self.assertFalse(out["posted"])
            self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
