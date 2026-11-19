"""Synthetic-only static tests for the isolated winter-timezone launchers
(GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121):

  deploy/tim-winter-tz/start_tim_isolated_bridge.bat
  deploy/tim-winter-tz/stop_tim_isolated_bridge.bat
  deploy/tim-winter-tz/run_winter_tz_probe_isolated.bat

Standard-library unittest only (governance CI installs no dependencies). STATIC checks on the
launchers' bytes/text — no batch execution, no host contact, no token. Proves ASCII/no-BOM,
no-argument, fixed-path, opaque-token, value-blind, fail-closed, exit-code-propagating launchers
with no forbidden command (leading-token ALLOW-LIST), and the hard-coded loopback probe target.
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIR = os.path.join(_HERE, "..", "deploy", "tim-winter-tz")

START = os.path.join(_DIR, "start_tim_isolated_bridge.bat")
STOP = os.path.join(_DIR, "stop_tim_isolated_bridge.bat")
PROBE = os.path.join(_DIR, "run_winter_tz_probe_isolated.bat")
ALL = (START, STOP, PROBE)

SECRETS_LOADER = r"C:\GuvFX\secrets\bridge.tokens.bat"
CTL = r"C:\GuvFX\TIM\winter-tz-probe\tim_isolated_bridge_ctl.py"
PROBE_PY = r"C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py"
TOKEN = "GUVFX_AGENT_TOKEN"
LOOPBACK_BASE = "http://127.0.0.1:8977"

ALLOWED_LEADING_TOKENS = frozenset({
    "echo", "setlocal", "endlocal", "if", "set", "call", "where", "python", "exit", "rem",
})


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _text(path):
    return _bytes(path).decode("ascii")  # also enforces ASCII-only


def _code_lines(path):
    return [ln for ln in _text(path).splitlines() if not ln.strip().lower().startswith("rem")]


def _segments(path):
    for lineno, raw in enumerate(_text(path).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low == "@echo off" or low.startswith("rem") or line in (")", "("):
            continue
        line = re.sub(r"\d?>&\d", " ", line)
        line = re.sub(r"\d?>\s*nul", " ", line, flags=re.IGNORECASE)
        for sep in ("&&", "||", "&", "|"):
            line = line.replace(sep, "\n")
        for seg in line.split("\n"):
            seg = seg.strip()
            if seg:
                yield lineno, seg


def _leading(seg):
    tok = (seg.lstrip("@(").strip().split(None, 1) or [""])[0]
    return tok.lower().rstrip("(")


class TestCommonToAllLaunchers(unittest.TestCase):
    def test_ascii_no_bom(self):
        for p in ALL:
            data = _bytes(p)
            self.assertFalse(data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")),
                             "%s has a BOM" % os.path.basename(p))
            self.assertTrue(all(b < 0x80 for b in data), "%s has non-ASCII bytes" % p)

    def test_echo_off_and_setlocal_endlocal(self):
        for p in ALL:
            t = _text(p)
            self.assertEqual(t.splitlines()[0].strip().lower(), "@echo off", p)
            self.assertRegex(t, r"(?im)^\s*setlocal\b")
            self.assertRegex(t, r"(?im)^\s*endlocal\b")

    def test_no_arguments(self):
        for p in ALL:
            t = _text(p)
            self.assertIn('if not "%~1"==""', t, p)
            self.assertRegex(t, r"(?is)%~1.*?exit /b 10")
            self.assertNotIn("%~2", t)
            self.assertNotIn("%*", t)

    def test_exit_code_propagation(self):
        for p in ALL:
            t = _text(p)
            self.assertIn('set "RC=%ERRORLEVEL%"', t, p)
            self.assertIn("exit /b %RC%", t, p)

    def test_only_allowlisted_leading_tokens(self):
        for p in ALL:
            for lineno, seg in _segments(p):
                tok = _leading(seg)
                if tok == "":
                    continue
                self.assertIn(tok, ALLOWED_LEADING_TOKENS,
                              "%s line %d: disallowed token %r (%r)" % (os.path.basename(p), lineno, tok, seg))

    def test_no_persistence_or_broad_kill_or_trading(self):
        for p in ALL:
            code = "\n".join(_code_lines(p)).lower()
            for bad in ("setx", "reg add", "reg delete", ">>", "schtasks", "taskkill",
                        "/im ", "shutdown", "start-process", "stop-process", "restart-service",
                        "net start", "net stop", 'start "', "powershell", "wmic",
                        "order_send", "withdraw", "place_order", "/mt5/order"):
                self.assertNotIn(bad, code, "%s must not contain %r" % (os.path.basename(p), bad))

    def test_no_token_echoed_or_assigned(self):
        for p in ALL:
            t = _text(p)
            self.assertFalse(re.search(r'set\s+"?%s\s*=' % TOKEN, t),
                             "%s assigns the token a value" % p)
            for _ln, seg in _segments(p):
                if seg.lower().startswith("echo") and TOKEN in seg:
                    self.fail("%s echoes the token" % p)

    def test_no_credential_literal(self):
        for p in ALL:
            self.assertFalse(re.search(r"\b[0-9a-fA-F]{40,}\b", _text(p)),
                             "%s has a long hex run resembling a secret" % p)


class TestStartLauncher(unittest.TestCase):
    def test_opaque_token_then_controller_start(self):
        t = _text(START)
        self.assertIn('call "%SECRETS_LOADER%"', t)
        self.assertIn("if not defined %s" % TOKEN, t)
        self.assertEqual(t.count(CTL), 1)
        self.assertIn('set "CTL=%s"' % CTL, t)
        self.assertIn('python "%CTL%" start', t)
        self.assertNotIn("--base-url", t)
        self.assertNotIn('python "%CTL%" stop', t)  # start never invokes the stop subcommand

    def test_fixed_secrets_loader(self):
        self.assertIn('set "SECRETS_LOADER=%s"' % SECRETS_LOADER, _text(START))


class TestStopLauncher(unittest.TestCase):
    def test_no_token_and_controller_stop(self):
        t = _text(STOP)
        self.assertNotIn("SECRETS_LOADER", t)   # stop needs no token
        self.assertNotIn(TOKEN, t)
        self.assertNotIn("call ", t)
        self.assertEqual(t.count(CTL), 1)
        self.assertIn('python "%CTL%" stop', t)


class TestProbeLauncher(unittest.TestCase):
    def test_opaque_token_and_loopback_target(self):
        t = _text(PROBE)
        self.assertIn('call "%SECRETS_LOADER%"', t)
        self.assertIn("if not defined %s" % TOKEN, t)
        self.assertIn('set "TIM_WINTER_TZ_PROBE_ON_HOST=1"', t)
        self.assertIn("--execute", t)
        self.assertEqual(t.count("--expect-server TradersWay-Demo"), 1)
        # base URL hard-coded to the loopback fixed port; never 0.0.0.0, never arbitrary.
        self.assertEqual(t.count("--base-url"), 1)
        self.assertIn("--base-url " + LOOPBACK_BASE, t)
        self.assertEqual(len(re.findall(r"--base-url\s+\S+", t)), 1)
        self.assertNotIn("0.0.0.0", t)
        self.assertNotIn("MT5_ALLOW_LIVE", t)

    def test_fixed_probe_path(self):
        t = _text(PROBE)
        self.assertEqual(t.count(PROBE_PY), 1)
        self.assertIn('set "PROBE=%s"' % PROBE_PY, t)


if __name__ == "__main__":
    unittest.main()
