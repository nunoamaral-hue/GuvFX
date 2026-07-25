"""Static no-trade proof for the GuvFX disposable-demo diagnostic EA (WS-B).

The EA (deploy/beta-agent/diagnostics/GuvfxProbeEA.mq5) is a VALIDATION test artefact, not a strategy. Its whole
safety case is that it can never place an order, reach the network, load a DLL, log a credential, or write outside
its slot, and that it fails closed on a non-demo account. These tests reject the EA if any of those invariants is
broken - so a future edit that (accidentally or otherwise) introduces a trading API fails CI before the EA can be
compiled or run on the host.
"""
import os
import re
from django.test import SimpleTestCase

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_EA = os.path.join(_REPO, "deploy", "beta-agent", "diagnostics", "GuvfxProbeEA.mq5")


def _src():
    return open(_EA, encoding="utf-8").read()


def _code():
    """EA source with // line-comments and block comments stripped, so a banned token in a comment (the header
    lists what the EA must NOT do) never satisfies a positive assertion or trips a negative one."""
    src = _src()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)          # block comments
    src = re.sub(r"//[^\n]*", "", src)                       # line comments
    return src


class DiagnosticEaNoTradeTests(SimpleTestCase):
    # Any of these appearing in EXECUTABLE code means the EA can (or is preparing to) trade / reach out / load
    # native code. Matched against comment-stripped source so the documentation header is not a false positive.
    FORBIDDEN = (
        # direct trade primitives
        "OrderSend", "OrderSendAsync", "OrderModify", "OrderClose", "OrderDelete",
        "PositionOpen", "PositionClose", "PositionModify",
        "CTrade", "CExpert", "OrderCalcMargin", "OrderCalcProfit",
        r"\.Buy\(", r"\.Sell\(", r"\.BuyLimit\(", r"\.SellLimit\(", r"\.BuyStop\(", r"\.SellStop\(",
        # indirect control transfer to compiled code (a probe never loads or drives other MQL programs)
        "ChartApplyTemplate", "iCustom", "IndicatorCreate", "EventChartCustom",
        # network (incl. the TLS socket variants)
        "WebRequest", "SocketCreate", "SocketConnect", "SocketSend", "SocketRead",
        "SocketTlsConnect", "SocketTlsSend", "SocketTlsRead", "SocketTlsHandshake",
        # native code + off-box messaging
        r"#import", "SendFTP", "SendMail", "SendNotification",
    )

    def test_no_trading_or_network_or_dll_api(self):
        code = _code()
        for tok in self.FORBIDDEN:
            self.assertFalse(re.search(tok, code), f"forbidden API present in EA: {tok}")

    def test_no_trade_library_include(self):
        code = _code()
        # A trade helper library is the usual on-ramp to CTrade.Buy/Sell; the diagnostic EA includes none.
        self.assertNotRegex(code, r"#include\s*[<\"][^>\"]*Trade[\\/][^>\"]*mqh")
        self.assertNotRegex(code, r"#include\s*[<\"][^>\"]*Expert[\\/]")

    def test_login_number_is_never_valued_only_presence_tested(self):
        # The account login (ACCOUNT_LOGIN) is a sensitive identifier. It may only be compared for presence
        # (!= 0 / == 0); it must never be formatted into a log or otherwise used as a value.
        code = _code()
        for m in re.finditer(r"AccountInfoInteger\(\s*ACCOUNT_LOGIN\s*\)", code):
            tail = code[m.end():m.end() + 8]
            self.assertRegex(tail, r"^\s*(!=|==)\s*0",
                             "ACCOUNT_LOGIN must only be presence-tested (!= 0 / == 0), never valued")

    def test_no_password_or_credential_api_is_referenced(self):
        # MQL5 has no password read API; assert nothing credential-shaped is printed.
        code = _code().lower()
        for tok in ("password", "investor", " account_login,"):     # a trailing comma => used as a format arg
            self.assertNotIn(tok, code, f"credential-shaped token in EA: {tok}")

    def test_the_only_account_string_read_is_the_shared_server_name(self):
        # Allowlist, not denylist: the server name is a shared demo-broker string and safe; any OTHER
        # AccountInfoString (ACCOUNT_NAME = the holder's name, ACCOUNT_COMPANY, ...) could identify a person.
        code = _code()
        for m in re.finditer(r"AccountInfoString\(\s*(ACCOUNT_\w+)\s*\)", code):
            self.assertEqual(m.group(1), "ACCOUNT_SERVER",
                             f"EA reads a non-allowlisted account string: {m.group(1)}")

    def test_comment_stripper_cannot_be_evaded_by_string_literals(self):
        # The FORBIDDEN scan runs on comment-stripped source, and the EA header intentionally NAMES the banned
        # APIs, so raw-source scanning is not an option. Guarantee the stripper is sound instead: no string
        # literal may contain a comment delimiter (which could hide a banned token on the same line).
        src = _src()
        for lit in re.findall(r'"(?:[^"\\]|\\.)*"', src):
            for delim in ("//", "/*", "*/"):
                self.assertNotIn(delim, lit, f"comment delimiter {delim!r} inside a string literal: {lit!r}")

    def test_writes_are_slot_contained_never_common(self):
        # FILE_COMMON would write to the shared terminal common folder, escaping the slot. The EA must not use it.
        code = _code()
        self.assertNotIn("FILE_COMMON", code)
        self.assertIn("FileOpen(", code)                     # it does write a log, just slot-contained

    def test_fails_closed_on_a_non_demo_account(self):
        code = _code()
        # OnInit must refuse (INIT_FAILED) when a logged-in account is not DEMO.
        self.assertIn("ACCOUNT_TRADE_MODE_DEMO", code)
        self.assertIn("INIT_FAILED", code)
        self.assertRegex(code, r"ACCOUNT_TRADE_MODE\s*\)\s*!=\s*ACCOUNT_TRADE_MODE_DEMO")

    def test_carries_the_validation_identifier(self):
        self.assertIn("GUVFX-PROBE-EA-B3P2", _src())

    def test_it_actually_records_the_required_diagnostic_fields(self):
        # Guard against the EA being gutted to a decorative shell that passes the negative checks but records
        # nothing. Each required signal must be read at least once.
        code = _code()
        required = (
            "TERMINAL_BUILD", "ACCOUNT_TRADE_MODE", "ACCOUNT_SERVER", "TERMINAL_CONNECTED",
            "SYMBOL_BID", "SYMBOL_ASK", "SYMBOL_SELECT", "TERMINAL_TRADE_ALLOWED", "MQL_TRADE_ALLOWED",
            "ChartID", "CHART_WINDOWS_TOTAL", "TERMINAL_DATA_PATH", "OnTimer", "OnTick", "OnInit", "OnDeinit",
        )
        for field in required:
            self.assertIn(field, code, f"diagnostic EA no longer reads required field: {field}")
