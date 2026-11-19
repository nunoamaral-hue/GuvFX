"""Synthetic-only tests for deploy/tim-winter-tz/run_winter_tz_probe.bat
(GFX-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011).

Standard-library unittest only (the governance CI job installs no dependencies).
These are STATIC checks on the launcher's bytes/text: no batch execution, no
network, no broker, no MT5, no host contact, no credentials. The launcher's real
runtime behaviour is proved separately by the windows-latest synthetic gate,
which uses only a neutral synthetic token and a stub probe.

The checks prove the launcher is value-blind, fixed-path, argument-free,
fail-closed, exit-code-propagating, and free of any forbidden capability
(persistence, task/service/process control, trading/order/withdrawal, arbitrary
paths), using a leading-token ALLOW-LIST rather than a deny-list.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LAUNCHER = os.path.join(_HERE, "..", "deploy", "tim-winter-tz", "run_winter_tz_probe.bat")

SECRETS_LOADER = r"C:\GuvFX\secrets\bridge.tokens.bat"
PROBE_PATH = r"C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py"
APPROVED_SERVER = "TradersWay-Demo"
TOKEN_NAME = "GUVFX_AGENT_TOKEN"
ON_HOST = "TIM_WINTER_TZ_PROBE_ON_HOST"

# Commands a value-blind, read-only launcher is permitted to begin a statement
# with. Anything outside this set (schtasks, sc, net, taskkill, setx, reg, del,
# copy, move, type, more, findstr, for, goto, start, powershell, curl, ...) fails.
ALLOWED_LEADING_TOKENS = frozenset({
    "echo", "setlocal", "endlocal", "if", "set", "call", "where", "python",
    "exit", "rem",
})


def _read_bytes():
    with open(_LAUNCHER, "rb") as fh:
        return fh.read()


def _read_text():
    return _read_bytes().decode("ascii")  # decode('ascii') also enforces ASCII-only


def _logical_segments(text):
    """Yield (lineno, segment) command segments: comments/blank stripped, each
    line split on the '&' / '|' command separators, parentheses removed."""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low == "@echo off" or low.startswith("rem") or low == ")" or low == "(":
            continue
        # Remove redirection operators first so their '&' (e.g. 2>&1, >&2) is not
        # mistaken for a command separator; also drop '>nul' style sinks.
        line = re.sub(r"\d?>&\d", " ", line)
        line = re.sub(r"\d?>\s*nul", " ", line, flags=re.IGNORECASE)
        # Now split on genuine command separators.
        for sep in ("&&", "||", "&", "|"):
            line = line.replace(sep, "\n")
        for seg in line.split("\n"):
            seg = seg.strip()
            if seg:
                yield lineno, seg


def _leading_token(segment):
    seg = segment.lstrip("@(").strip()
    # An 'if ... (' opener: evaluate the command AFTER the condition too.
    token = seg.split(None, 1)[0] if seg else ""
    return token.lower().rstrip("(")


def _code_text():
    """Launcher text with REM comment lines removed (executable lines only).

    Deny-list capability checks run against this so a comment that names a
    forbidden primitive (to document that it is absent) does not self-trip."""
    return "\n".join(
        raw for raw in _read_text().splitlines()
        if not raw.strip().lower().startswith("rem")
    )


class TestEncoding(unittest.TestCase):
    def test_ascii_no_bom(self):
        data = _read_bytes()
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM present")
        self.assertFalse(data.startswith(b"\xff\xfe"), "UTF-16LE BOM present")
        self.assertFalse(data.startswith(b"\xfe\xff"), "UTF-16BE BOM present")
        for i, b in enumerate(data):
            self.assertLess(b, 0x80, "non-ASCII byte 0x%02x at offset %d" % (b, i))

    def test_decodes_as_pure_ascii(self):
        _read_text()  # raises UnicodeDecodeError if any non-ASCII byte exists


class TestStructure(unittest.TestCase):
    def test_echo_off_first_line(self):
        self.assertEqual(_read_text().splitlines()[0].strip().lower(), "@echo off")

    def test_setlocal_and_endlocal_present(self):
        text = _read_text()
        self.assertRegex(text, r"(?im)^\s*setlocal\b")
        self.assertRegex(text, r"(?im)^\s*endlocal\b")

    def test_accepts_no_arguments(self):
        text = _read_text()
        # Rejects any supplied argument and fails closed with code 10.
        self.assertIn('if not "%~1"==""', text)
        self.assertRegex(text, r"(?is)%~1.*?exit /b 10")
        # Never consumes further positional args.
        self.assertNotIn("%~2", text)
        self.assertNotIn("%*", text)


class TestFixedPathsAndServer(unittest.TestCase):
    def test_fixed_secrets_loader_path(self):
        text = _read_text()
        # The literal path is bound once to a fixed var; the call uses %VAR%.
        self.assertEqual(text.count(SECRETS_LOADER), 1)
        self.assertIn('set "SECRETS_LOADER=%s"' % SECRETS_LOADER, text)
        self.assertIn('call "%SECRETS_LOADER%"', text)

    def test_fixed_probe_path(self):
        text = _read_text()
        self.assertEqual(text.count(PROBE_PATH), 1)
        self.assertIn('set "PROBE=%s"' % PROBE_PATH, text)
        self.assertIn('python "%PROBE%"', text)

    def test_hardcoded_demo_server_only(self):
        text = _read_text()
        self.assertEqual(text.count("--expect-server"), 1)
        self.assertIn("--expect-server %s" % APPROVED_SERVER, text)
        # No LIVE / configurable server; no arg or env override of the server.
        self.assertNotIn("MT5_ALLOW_LIVE", text)
        self.assertEqual(len(re.findall(r"--expect-server\s+\S+", text)), 1)
        self.assertRegex(text, r"--expect-server\s+TradersWay-Demo\b")

    def test_no_arbitrary_path_or_variable_server(self):
        text = _read_text()
        # the probe is invoked by its fixed %PROBE% var bound to the fixed path.
        self.assertIn('python "%PROBE%" --execute --expect-server ' + APPROVED_SERVER, text)


class TestOpaqueTokenHandling(unittest.TestCase):
    def test_opaque_call_only(self):
        text = _read_text()
        self.assertIn('call "%SECRETS_LOADER%"', text)
        # The secrets file is never READ or printed (no reader command on it).
        for reader in ("type ", "more ", "findstr", "for /f", "for /F", "set /p"):
            self.assertNotIn(reader, text)

    def test_token_presence_checked_value_blind(self):
        text = _read_text()
        self.assertIn("if not defined %s" % TOKEN_NAME, text)

    def test_token_never_assigned_a_value(self):
        text = _read_text()
        # No 'set GUVFX_AGENT_TOKEN=...' (value handling); only presence checks.
        self.assertFalse(re.search(r'set\s+"?%s\s*=' % TOKEN_NAME, text),
                         "launcher must never assign the token a value")

    def test_token_never_echoed(self):
        for _, seg in _logical_segments(_read_text()):
            if seg.lower().startswith("echo") and TOKEN_NAME in seg:
                self.fail("token name appears on an echo line: %r" % seg)

    def test_no_token_on_argv_or_stdin(self):
        text = _read_text()
        self.assertNotIn("--token", text)
        self.assertNotIn("set /p", text)  # no interactive entry


class TestNoForbiddenCapability(unittest.TestCase):
    def test_only_allowlisted_leading_tokens(self):
        for lineno, seg in _logical_segments(_read_text()):
            tok = _leading_token(seg)
            if tok == "":
                continue
            self.assertIn(
                tok, ALLOWED_LEADING_TOKENS,
                "disallowed command token %r (line %d: %r)" % (tok, lineno, seg),
            )

    def test_no_persistence(self):
        text = _code_text().lower()
        for bad in ("setx", "reg add", "reg delete", "[environment]::setenvironment",
                    ">>"):
            self.assertNotIn(bad, text, "persistence/append primitive present: %r" % bad)

    def test_no_task_service_or_process_control(self):
        text = _code_text().lower()
        for bad in ("schtasks", "taskkill", "shutdown", "start-process",
                    "stop-process", "restart-service", "net start", "net stop",
                    "sc config", "sc start", "sc stop", "sc create", 'start "',
                    "powershell", "wmic"):
            self.assertNotIn(bad, text, "process/service control present: %r" % bad)

    def test_no_trading_or_order_primitives(self):
        text = _code_text().lower()
        for bad in ("order_send", "order_check", "place_order", "withdraw",
                    "/mt5/", "copy_rates", "import mt5", "buy", "sell"):
            self.assertNotIn(bad, text, "trading primitive present: %r" % bad)

    def test_no_network_download_tools(self):
        text = _code_text().lower()
        for bad in ("curl", "invoke-webrequest", "bitsadmin", "certutil",
                    "Invoke-RestMethod".lower()):
            self.assertNotIn(bad, text, "network/download tool present: %r" % bad)


class TestFailClosedAndExitCodes(unittest.TestCase):
    def test_all_fail_closed_branches_present(self):
        text = _read_text()
        for code in (10, 11, 12, 13, 14):
            self.assertIn("exit /b %d" % code, text, "missing fail-closed code %d" % code)

    def test_fail_closed_conditions(self):
        text = _read_text()
        self.assertIn('if not exist "%SECRETS_LOADER%"', text)  # loader absent -> 11
        self.assertIn('if not exist "%PROBE%"', text)           # probe absent  -> 12
        self.assertIn("where python", text)                     # python check  -> 13
        self.assertIn("if errorlevel 1", text)
        self.assertIn("if not defined %s" % TOKEN_NAME, text)   # token absent  -> 14

    def test_exit_code_propagation(self):
        text = _read_text()
        self.assertIn('set "RC=%ERRORLEVEL%"', text)
        self.assertIn("exit /b %RC%", text)
        # RC is captured immediately after the probe invocation.
        probe_idx = text.index('python "%PROBE%"')
        rc_idx = text.index('set "RC=%ERRORLEVEL%"')
        self.assertGreater(rc_idx, probe_idx)

    def test_on_host_gate_set_in_process(self):
        text = _read_text()
        self.assertIn('set "%s=1"' % ON_HOST, text)
        self.assertIn("--execute", text)


class TestNoSensitiveLiteral(unittest.TestCase):
    def test_no_real_token_literal(self):
        text = _read_text()
        # No long hex/base64-ish run that could be a real credential.
        self.assertFalse(re.search(r"\b[0-9a-fA-F]{40,}\b", text),
                         "launcher contains a long hex run resembling a secret")

    def test_test_file_carries_no_credential_literal(self):
        # This suite references only NAMES (GUVFX_AGENT_TOKEN) and synthetic
        # labels; it must carry no real token value (e.g. a long hex run).
        with open(__file__, "r", encoding="utf-8") as fh:
            own = fh.read()
        self.assertFalse(re.search(r"\b[0-9a-fA-F]{40,}\b", own),
                         "test file contains a long hex run resembling a secret")


if __name__ == "__main__":
    unittest.main()
