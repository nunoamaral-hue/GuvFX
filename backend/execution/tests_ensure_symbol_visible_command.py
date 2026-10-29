"""ADR-0063 — the governed ``ensure_symbol_visible`` management command.

Proves it resolves ONLY the account's OWN bridge, PROVES identity (login + server + environment) before AND
after the op, INDEPENDENTLY re-reads visibility after commit (never trusts the mutating call's self-report),
is dry-run by default (no mutation sent), refuses blank/wildcard/bulk symbols, an unavailable symbol, an
unresolved transport, an identity mismatch, a timeout and an unproven postcondition — and NEVER creates an
ExecutionJob / order / trade. The bridge is mocked (no network, no MT5).
"""
import ast
import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from execution.management.commands import ensure_symbol_visible as cmd
from execution.models import ExecutionJob
from execution.snapshot_transport import IdentityCheck, SnapshotTransport
from trading.models import TradingAccount

U = get_user_model()
_n = 0

_ENV = {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://GLOBAL:8788", "GUVFX_WINDOWS_AGENT_TOKEN": "TOK"}
_OK = IdentityCheck(True, "ok", "51763832", "51763832")
_BASE = "http://TENANT-8807:8807"
_ACCT_JSON = b'{"ok": true, "account_login": "51763832", "account_server": "IS6Technologies-Live", "trade_mode": 2}'
_ENSURE_OK = {"ok": True, "changed": True, "after_visible": True}


def _acct(user, number="51763832"):
    global _n
    _n += 1
    return TradingAccount.objects.create(user=user, name="IS6", broker_name="IS6",
                                         account_number=number, is_demo=False, is_active=False)


def _resp(body: bytes):
    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return body
    return _R()


def _bridge(base=_BASE, *, acct=_ACCT_JSON, present=("XAUUSD.std",), visible_before=False,
            reveals=True, ensure=None, record=None):
    """Stateful tenant-bridge fake: /mt5/symbols reports ``present`` symbols, visible iff revealed. A successful
    ensure-symbol-visible POST flips them visible (unless ``reveals=False`` — a bridge that lies about success)."""
    ens = dict(_ENSURE_OK if ensure is None else ensure)
    state = {"revealed": visible_before}

    def _symbols_body():
        return json.dumps({"ok": True, "symbols": [
            {"name": n, "visible": state["revealed"], "trade_mode": 4} for n in present]}).encode()

    def _fake(req, timeout=None):
        url = req.full_url
        assert url.startswith(base), f"read hit {url}, expected base {base}"
        if record is not None:
            record.append((req.get_method(), url))
        if "/mt5/snapshots/account" in url:
            return _resp(acct)
        if "/mt5/ensure-symbol-visible" in url:
            if ens.get("ok") and reveals:
                state["revealed"] = True
            return _resp(json.dumps(ens).encode())
        if "/mt5/symbols" in url:
            return _resp(_symbols_body())
        return _resp(b"{}")
    return _fake


class _Patches:
    """Install the standard transport mocks (override per test). ``verify`` may be a value or a MagicMock."""
    def __init__(self, *, resolve=None, verify=_OK, urlopen=None, record=None):
        self.resolve = resolve or SnapshotTransport(True, "ok", _BASE, per_tenant=True)
        self.verify = verify
        self.urlopen = urlopen if urlopen is not None else _bridge(record=record)

    def __enter__(self):
        verify_mock = self.verify if isinstance(self.verify, mock.MagicMock) else None
        self._stack = [
            mock.patch.dict(os.environ, _ENV, clear=False),
            mock.patch.object(cmd, "resolve_account_snapshot_base", return_value=self.resolve),
            mock.patch.object(cmd, "account_windows_username", return_value="guvfx_u_50"),
            (mock.patch.object(cmd, "verify_snapshot_identity", verify_mock) if verify_mock is not None
             else mock.patch.object(cmd, "verify_snapshot_identity", return_value=self.verify)),
            mock.patch.object(cmd.urllib.request, "urlopen", self.urlopen),
        ]
        for p in self._stack:
            p.start()
        return self

    def __exit__(self, *a):
        for p in reversed(self._stack):
            p.stop()
        return False


class EnsureSymbolVisibleCommandTests(TestCase):
    def setUp(self):
        self.u = U.objects.create_user(username="r", email="r@x.invalid", password="x")
        self.a = _acct(self.u)

    # ---------- dry-run (default) ----------
    def test_dry_run_default_sends_no_mutation(self):
        rec = []
        with _Patches(urlopen=_bridge(record=rec)):
            call_command("ensure_symbol_visible", "--account", str(self.a.id), "--symbol", "XAUUSD.std")
        self.assertTrue(any(m == "GET" and "/mt5/symbols" in u for m, u in rec))
        self.assertFalse(any("/mt5/ensure-symbol-visible" in u for _m, u in rec))  # NO mutation sent
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # ---------- commit ----------
    def test_commit_reveals_symbol(self):
        rec = []
        with _Patches(urlopen=_bridge(record=rec)):
            call_command("ensure_symbol_visible", "--account", str(self.a.id),
                         "--symbol", "XAUUSD.std", "--commit")
        posts = [(m, u) for m, u in rec if "/mt5/ensure-symbol-visible" in u]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][0], "POST")
        # the independent post-commit re-read happened (a second /mt5/symbols GET)
        self.assertGreaterEqual(sum(1 for m, u in rec if m == "GET" and "/mt5/symbols" in u), 2)
        self.assertEqual(ExecutionJob.objects.count(), 0)   # negative control: no order/job created

    def test_commit_idempotent_when_already_visible(self):
        with _Patches(urlopen=_bridge(visible_before=True,
                                      ensure={"ok": True, "changed": False, "after_visible": True})):
            call_command("ensure_symbol_visible", "--account", str(self.a.id),
                         "--symbol", "XAUUSD.std", "--commit")
        self.assertEqual(ExecutionJob.objects.count(), 0)

    # finding 1 — the command's identity firewall usage is asserted, not just mocked away.
    def test_commit_verifies_identity_twice_with_server_and_environment(self):
        verify = mock.MagicMock(return_value=_OK)
        with _Patches(verify=verify):
            call_command("ensure_symbol_visible", "--account", str(self.a.id),
                         "--symbol", "XAUUSD.std", "--commit")
        self.assertEqual(verify.call_count, 2)   # pre + post drift re-verify both ran
        for call in verify.call_args_list:
            self.assertTrue(call.kwargs.get("require_server"), "must pin the server")
            self.assertTrue(call.kwargs.get("require_environment"), "must pin DEMO/LIVE environment")

    # finding 3 — a bridge that falsely reports after_visible is still caught by the independent re-read.
    def test_independent_reread_catches_bridge_reporting_false_success(self):
        with _Patches(urlopen=_bridge(reveals=False, ensure={"ok": True, "changed": True, "after_visible": True})):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")

    # ---------- fail-closed ----------
    def test_identity_mismatch_refused_no_mutation(self):
        rec = []
        with _Patches(verify=IdentityCheck(False, "login_mismatch", "a", "b"), urlopen=_bridge(record=rec)):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")
        self.assertFalse(any("/mt5/ensure-symbol-visible" in u for _m, u in rec))

    def test_unresolved_transport_refused(self):
        with _Patches(resolve=SnapshotTransport(False, "endpoint_not_ready")):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")

    def test_unavailable_symbol_refused_no_mutation(self):
        rec = []
        with _Patches(urlopen=_bridge(present=("EURUSD.std",), record=rec)):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")
        self.assertFalse(any("/mt5/ensure-symbol-visible" in u for _m, u in rec))

    def test_postcondition_not_proven_refused(self):
        with _Patches(urlopen=_bridge(reveals=False, ensure={"ok": True, "changed": True, "after_visible": False})):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")

    def test_bridge_refusal_propagates(self):
        with _Patches(urlopen=_bridge(ensure={"ok": False, "error": "SYMBOL_NOT_SELECTABLE_ON_MT5"})):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id),
                             "--symbol", "XAUUSD.std", "--commit")

    def test_timeout_fails_closed(self):
        def _boom(req, timeout=None):
            raise cmd.urllib.error.URLError("timed out")
        with _Patches(urlopen=_boom):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id), "--symbol", "XAUUSD.std")

    def test_blank_and_wildcard_and_bulk_refused_before_any_network(self):
        resolve = mock.MagicMock()
        with mock.patch.dict(os.environ, _ENV, clear=False), \
             mock.patch.object(cmd, "resolve_account_snapshot_base", resolve):
            for bad in ("", "   ", "*", "XAU*", "XAUUSD.std,EURUSD", "XAU USD"):
                with self.assertRaises(CommandError):
                    call_command("ensure_symbol_visible", "--account", str(self.a.id), "--symbol", bad)
        resolve.assert_not_called()   # rejected before resolving any bridge

    def test_missing_token_refused(self):
        with mock.patch.dict(os.environ, {"GUVFX_WINDOWS_AGENT_BASE_URL": "http://GLOBAL:8788",
                                          "GUVFX_WINDOWS_AGENT_TOKEN": "", "GUVFX_AGENT_TOKEN": ""}, clear=False):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", str(self.a.id), "--symbol", "XAUUSD.std")

    def test_unknown_account_refused(self):
        with mock.patch.dict(os.environ, _ENV, clear=False):
            with self.assertRaises(CommandError):
                call_command("ensure_symbol_visible", "--account", "99999", "--symbol", "XAUUSD.std")

    # ---------- negative control: module imports no execution/order path ----------
    def test_command_module_has_no_order_or_job_imports(self):
        src = open(cmd.__file__, "r", encoding="utf-8").read()
        tree = ast.parse(src)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        joined = " ".join(imported)
        for banned in ("ExecutionJob", "order", "process_job", "producer", "consumer", "risk_controls"):
            self.assertNotIn(banned, joined, f"command must not import {banned}")
