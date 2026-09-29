"""P3 reboot-recovery readiness - the read-only PROBE_SESSION host-op (host_protocol / host_agent_dispatch /
primitive_runner / stage-manifest + Probe-GuvfxSession.ps1).

Proves the narrow no-RCE contract for a READ-ONLY session probe: the op is allow-listed across all four layers;
the host derives the guvfx_u_<id> identity server-side (no caller username/session target); Customer Zero is
refused at BOTH the dispatcher and (defence in depth) in the .ps1; the runner injects -AccountId from the
username and pipes no stdin; and the .ps1 is ASCII-only, mutates nothing (read-only qwinsta), and never trusts a
'no session' negative unless qwinsta actually produced output (RULE 11). Pure; no host, no PowerShell.
"""
import os
import re
import sys
import unittest

from django.test import SimpleTestCase

from hosted_workspace import host_agent_dispatch as D
from hosted_workspace import host_protocol as P

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "hosted-executor")
_LIB = os.path.join(_BUNDLE, "lib")
for _p in (_BUNDLE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import primitive_runner as pr  # noqa: E402

_WINDOWS_SCRIPTS = os.path.join(_REPO, "backend", "terminal_provisioning", "windows")
_PROBE = os.path.join(_WINDOWS_SCRIPTS, "Probe-GuvfxSession.ps1")


def _strip_ps_comments(src: str) -> str:
    """Return the .ps1 with block ``<# .. #>`` and line ``#`` comments removed, so a static scan sees CODE only
    (a '#' inside a quoted string is preserved). Prose in comments must not trip the read-only / argument scans."""
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
                break                       # rest of the line is a comment
            else:
                res.append(ch)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


# The read-only denylist + the qwinsta-argument scan, as reusable functions so a POSITIVE control can exercise the
# EXACT measurement path against a known-bad snippet (RULE 11: a 'no forbidden verb' negative is not authoritative
# until the same scan is shown to FIRE on a known positive - guards against a degraded _strip_ps_comments that
# would make every scan pass vacuously). A denylist is a tripwire, not a proof of read-only; the authoritative
# read-only guarantee is source review + the host ParseFile/smoke gate.
_FORBIDDEN_VERBS = [
    "Stop-Process", "Start-Process", "New-Item", "Remove-Item", "Set-Content", "Out-File",
    "Set-ItemProperty", "New-ItemProperty", "Stop-Service", "Start-Service", "Restart-",
    "Stop-ScheduledTask", "Start-ScheduledTask", "Register-ScheduledTask", "Invoke-Expression",
    "Invoke-Command", "logoff", "tsdiscon", "rwinsta", "reset session", "shutdown", "mt5.initialize",
]


def _readonly_hits(code: str) -> list:
    low = code.lower()
    return [tok for tok in _FORBIDDEN_VERBS if tok.lower() in low]


def _qwinsta_arg_hit(code: str):
    # a qwinsta INVOCATION carrying any argument other than the 2> redirection (whitespace required after the
    # command, so the reason string "qwinsta_no_output" is not matched; "qwinsta 2>$null" excluded by lookahead)
    return re.search(r"qwinsta\s+(?!2>)\S", code) or ("qwinsta $" if "qwinsta $" in code else None)

KR = {"k1": "probe-secret"}
T = 3_000_000


def _burner():
    seen = set()

    def burn(n, e):
        first = n not in seen
        seen.add(n)
        return first
    return burn


def _req(op, account_id=14, params=None, key_id="k1"):
    return P.sign_hosted_request(account_id=account_id, operation=op, correlation_id="c1", keyring=KR,
                                 key_id=key_id, now=T, params=params, nonce=None)


class ProbeSessionAllowlistParity(SimpleTestCase):
    """A new host op is only real when it is wired across ALL FOUR layers (memory: catalogue_v1_is6_launcher)."""

    def test_wired_across_all_four_layers(self):
        self.assertIn("PROBE_SESSION", P.HOSTED_OPERATIONS)
        self.assertIn("PROBE_SESSION", D.OP_PRIMITIVES)
        self.assertEqual(D.OP_PRIMITIVES["PROBE_SESSION"]["primitive"], "probe_session")
        self.assertIn("probe_session", pr.CONTRACT)
        self.assertEqual(pr.CONTRACT["probe_session"].script, "Probe-GuvfxSession.ps1")
        self.assertTrue(os.path.isfile(_PROBE))

    def test_probe_carries_no_credential_and_no_params(self):
        self.assertNotIn("PROBE_SESSION", P.CREDENTIALED_HOSTED_OPERATIONS)
        self.assertEqual(D.OP_PRIMITIVES["PROBE_SESSION"]["params_allow"], ())


class ProbeSessionDispatch(SimpleTestCase):
    def _run(self, op, reserved_ids="", **kw):
        calls = []

        def run_primitive(name, args):
            calls.append((name, args))
            return {"ok": True, "primitive": name, "session_found": False, "sessions_seen": 3}

        resp = D.dispatch(_req(op, **kw), keyring=KR, now=T, nonce_burn=_burner(),
                          run_primitive=run_primitive, reserved_ids=reserved_ids)
        return calls, resp

    def test_maps_to_primitive_with_server_derived_identity(self):
        calls, resp = self._run("PROBE_SESSION", account_id=14)
        self.assertEqual(calls[0][0], "probe_session")
        # identity is DERIVED server-side from account_id; no caller-supplied username/session target exists
        self.assertEqual(calls[0][1]["username"], "guvfx_u_14")
        out = P.verify_hosted_response(resp, correlation_id="c1", nonce=resp["nonce"], keyring=KR)
        self.assertTrue(out["ok"])

    def test_customer_zero_refused_host_side(self):
        with self.assertRaises(P.HostProtocolError) as cm:
            self._run("PROBE_SESSION", account_id=1, reserved_ids=None)
        self.assertEqual(cm.exception.reason_code, "reserved_identity")

    def test_no_caller_param_smuggling(self):
        # empty allow-list: even a plausible scalar (a forged username/session id) is refused
        for bad in ({"username": "guvfx_u_999"}, {"session_id": 7}, {"target": "console"}):
            with self.assertRaises(P.HostProtocolError) as cm:
                self._run("PROBE_SESSION", account_id=14, params=bad)
            self.assertEqual(cm.exception.reason_code, "params_not_allowed")


class ProbeSessionRunnerArgv(unittest.TestCase):
    def _argv(self, primitive, args, proc=None):
        calls = []

        def fake_run(argv, *, input_bytes, timeout_s):
            calls.append({"argv": list(argv), "input": input_bytes})
            class _P:
                stdout = proc if proc is not None else b'{"ok":true,"session_found":false}'
                stderr = b""
                returncode = 0
            return _P()

        runner = pr.PrimitiveRunner(scripts_dir="/scripts", powershell="powershell",
                                    run_subprocess=fake_run, parse_validator=lambda p: (True, "ok"))
        result = runner.run(primitive, args)
        self.assertEqual(len(calls), 1)
        return calls[0], result

    def test_injects_accountid_from_username_and_no_stdin(self):
        call, res = self._argv("probe_session", {
            "username": "guvfx_u_14", "runtime_root": r"C:\GuvFX\accounts\14",
            "terminal_root": r"C:\GuvFX\accounts\14\terminal"})
        argv = call["argv"]
        self.assertIn("Probe-GuvfxSession.ps1", argv[argv.index("-File") + 1])
        self.assertEqual(argv[argv.index("-Username") + 1], "guvfx_u_14")
        self.assertIn("-AccountId", argv)                              # injected from username
        self.assertEqual(argv[argv.index("-AccountId") + 1], "14")
        # read-only probe carries NO password/stdin, and drops runtime_root/terminal_root (not in the argmap)
        self.assertEqual(call["input"], b"")
        self.assertNotIn("-RuntimeRoot", argv)
        self.assertNotIn("-TerminalRoot", argv)

    def test_account_id_underivable_from_bad_username_fails_closed(self):
        call_result = pr.PrimitiveRunner(scripts_dir="/scripts",
                                         run_subprocess=lambda *a, **k: None,
                                         parse_validator=lambda p: (True, "ok")).run(
            "probe_session", {"username": "administrator"})
        self.assertFalse(call_result["ok"])
        self.assertEqual(call_result["reason"], "account_id_underivable")


class ProbeSessionScriptStatic(unittest.TestCase):
    """We cannot run Windows PowerShell here; the authoritative ParseFile gate runs on the host (RULE 9). These
    static assertions pin the security-load-bearing shape of the reviewed .ps1 so a regression is caught in CI."""

    @classmethod
    def setUpClass(cls):
        with open(_PROBE, encoding="ascii") as fh:   # encoding="ascii" itself asserts ASCII-only (RULE 9)
            cls.src = fh.read()

    def test_is_ascii_only(self):
        self.src.encode("ascii")   # raises if any non-ASCII byte slipped in

    def test_customer_zero_and_identity_guards_present(self):
        # Reserve BOTH sacred identities (Customer Zero + the account-18 demo-control), matching sibling ops.
        self.assertIn("$RESERVED_ACCOUNT_IDS = @(1, 18)", self.src)
        self.assertIn("$RESERVED_ACCOUNT_IDS -contains $AccountId", self.src)
        self.assertIn('Fail "refusing_reserved_identity"', self.src)
        self.assertIn("$AccountId -le 0", self.src)
        self.assertIn('$Username -ne ("guvfx_u_" + $AccountId)', self.src)

    def test_is_read_only_no_mutating_or_session_control_verbs(self):
        # A read-only probe must never launch, log off, reset, or write. Scan CODE only (comments legitimately say
        # "no ... logoff/reset"). This is a tripwire (denylist), positive-controlled below.
        self.assertEqual(_readonly_hits(_strip_ps_comments(self.src)), [],
                         "read-only probe must contain no mutating/session-control verb")

    def test_uses_qwinsta_without_caller_or_derived_argument(self):
        # qwinsta is run BARE (all sessions) and filtered in PowerShell; the username is never appended to the
        # native command line (no argument surface at all). Scan CODE only (comments mention 'qwinsta to ...').
        code = _strip_ps_comments(self.src)
        self.assertIn("qwinsta 2>$null", self.src)
        self.assertIsNone(_qwinsta_arg_hit(code),
                          "qwinsta must take no argument other than the 2>$null redirection")

    def test_static_scans_have_positive_control(self):
        """RULE 11: the read-only and qwinsta-arg scans must be shown to FIRE on a known-bad script, and
        _strip_ps_comments must preserve real code - otherwise a degraded strip helper would green every scan."""
        bad = (
            "# a comment mentioning nothing bad\n"
            '$x = "keep this"\n'
            "Stop-Process -Id 4 -Force   # mutating!\n"
            "qwinsta $Username\n"
        )
        stripped = _strip_ps_comments(bad)
        # the scans FIRE on the known-bad code (measurement path proven live)
        self.assertIn("Stop-Process", _readonly_hits(stripped))
        self.assertIsNotNone(_qwinsta_arg_hit(stripped))
        # the strip helper does NOT over-strip: real code (and quoted content) survives, comments are removed
        self.assertIn('$x = "keep this"', stripped)
        self.assertIn("Stop-Process -Id 4 -Force", stripped)
        self.assertNotIn("mutating!", stripped)
        # and it preserves the '#' that lives INSIDE the probe's single-quoted regex (would break the qwinsta scan)
        self.assertIn("'^[>#\\s]+'", _strip_ps_comments(self.src))

    def test_guards_and_rule11_precede_use_by_source_order(self):
        """Presence is not enough (project review-fakes standard): the guards must run BEFORE the probe, and the
        RULE-11 blind check BEFORE the negative is trusted. Assert ordering by source offset (behaviour is proven
        by the host smoke gate, which Windows-less CI cannot run)."""
        s = self.src
        i_reserved = s.index("$RESERVED_ACCOUNT_IDS -contains $AccountId")
        i_username = s.index('$Username -ne ("guvfx_u_" + $AccountId)')
        i_qwinsta = s.index("qwinsta 2>$null")
        i_blind = s.index('Fail "qwinsta_no_output"')
        i_trust = s.index("$result.session_found = $found")
        self.assertLess(i_reserved, i_qwinsta, "CZ/reserved guard must run before qwinsta")
        self.assertLess(i_username, i_qwinsta, "identity guard must run before qwinsta")
        self.assertLess(i_blind, i_trust, "RULE-11 blind-measurement check must precede trusting session_found")

    def test_rule11_blind_vs_genuine_negative_tokens_present(self):
        self.assertIn('Fail "qwinsta_no_output"', self.src)
        self.assertIn("$dataRows -lt 1", self.src)
        self.assertIn('"no_session_for_account"', self.src)
        self.assertIn("sessions_seen", self.src)

    def test_ps1_param_block_matches_runner_argmap(self):
        """A param-name drift (e.g. renaming -Username) would only surface at host runtime; pin the .ps1 param()
        names against what the runner passes: -Username (argmap) + -AccountId (injected)."""
        self.assertRegex(self.src, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Username")
        self.assertRegex(self.src, r"\[Parameter\(Mandatory = \$true\)\]\[int\]\$AccountId")
        spec = pr.CONTRACT["probe_session"]
        self.assertEqual(spec.argmap, {"username": "-Username"})
        self.assertTrue(spec.inject_account_id)

    def test_emits_single_compact_json_verdict(self):
        self.assertIn("ConvertTo-Json -Compress", self.src)
        self.assertIn("$result.ok = $true", self.src)
