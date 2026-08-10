"""ADR-0034 Workspace Delivery — the RemoteApp payload builder (``build_remoteapp_rdp_payload``).

Pure tests (no DB): the RemoteApp variant carries the FreeRDP ``remote-app*`` parameters AND inherits every
isolation restriction of the dedicated builder; the ``||`` alias contract; the Windows password rides ONLY
inside the payload (later AES-encrypted), never leaked into a non-parameter field; and — critically — the
extracted shared base leaves the certified ``build_dedicated_rdp_payload`` output byte-for-byte unchanged.
Plus a runnable mutation-adequacy harness for ``normalize_remote_app``.
"""
from __future__ import annotations

import textwrap

from django.test import SimpleTestCase

from mt5.guac_json import (
    build_dedicated_rdp_payload,
    build_remoteapp_rdp_payload,
    normalize_remote_app,
)


def _remoteapp_params(**over):
    base = dict(
        username="ws-u", windows_username="guvfx_u_9", windows_password="P@ss-SECRET",
        host="node-1", remote_app="terminal64",
        remote_app_dir=r"C:\GuvFX\accounts\9\terminal", remote_app_args="/portable")
    base.update(over)
    return build_remoteapp_rdp_payload(**base)["connections"]["mt5-workspace"]["parameters"]


class RemoteAppPayloadTests(SimpleTestCase):
    def test_publishes_single_app_with_double_pipe_alias(self):
        self.assertEqual(_remoteapp_params()["remote-app"], "||terminal64")

    def test_carries_dir_and_args(self):
        p = _remoteapp_params()
        self.assertEqual(p["remote-app-dir"], r"C:\GuvFX\accounts\9\terminal")
        self.assertEqual(p["remote-app-args"], "/portable")

    def test_inherits_every_dedicated_restriction_except_paste(self):
        p = _remoteapp_params()
        # No drive redirection, no MT5->browser copy, no audio — inherited from the shared base.
        self.assertEqual(p["enable-drive"], "false")
        self.assertEqual(p["disable-copy"], "true")     # MT5 -> browser copy stays OFF (minimise exposure)
        self.assertEqual(p["enable-audio"], "false")    # inherited legacy key (guacd ignores it — see below)
        # Printing is additionally disabled for the RemoteApp variant.
        self.assertEqual(p["enable-printing"], "false")

    def test_input_completion_paste_enabled_and_layout_pinned(self):
        # ADR-0034 Customer-Zero input completion: browser->MT5 PASTE is enabled (broker passwords may contain
        # symbols) and the keyboard layout is pinned so symbol keys map correctly. Clipboard enabling must NOT
        # broaden the boundary — drive/file/printer stay disabled and copy-out stays off.
        p = _remoteapp_params()
        self.assertEqual(p["disable-paste"], "false")        # client(browser) -> server(MT5) paste ENABLED
        self.assertEqual(p["disable-copy"], "true")          # server(MT5) -> client copy still OFF
        self.assertEqual(p["server-layout"], "en-gb-qwerty") # explicit scancode translation
        # Enabling paste must not enable drive redirection, file transfer, printing, or audio.
        self.assertEqual(p["enable-drive"], "false")
        self.assertEqual(p["enable-printing"], "false")
        self.assertEqual(p["disable-audio"], "true")
        self.assertNotIn("enable-sftp", p)                   # no file transfer channel
        self.assertNotIn("enable-printing", {k: v for k, v in p.items() if v == "true"})

    def test_remoteapp_audio_lockdown_uses_effective_guacd_keys(self):
        # `enable-audio` is NOT a real guacd RDP key (it silently no-ops). The RemoteApp payload must carry
        # the keys guacd actually honours so the "no audio" posture is real: disable-audio=true (output OFF)
        # and enable-audio-input=false (mic OFF). Added on the RemoteApp payload only; host cert confirms the
        # disable takes effect on the deployed guacd (RULE 11).
        p = _remoteapp_params()
        self.assertEqual(p["disable-audio"], "true")
        self.assertEqual(p["enable-audio-input"], "false")

    def test_password_only_in_password_field_not_elsewhere(self):
        p = _remoteapp_params(windows_password="TOPSECRET")
        self.assertEqual(p["password"], "TOPSECRET")
        # The password must never bleed into any other parameter (e.g. remote-app-args).
        leaked = {k: v for k, v in p.items() if k != "password" and "TOPSECRET" in str(v)}
        self.assertEqual(leaked, {})

    def test_optional_dir_and_args_omitted_when_empty(self):
        p = _remoteapp_params(remote_app_dir="", remote_app_args="")
        self.assertNotIn("remote-app-dir", p)
        self.assertNotIn("remote-app-args", p)
        self.assertEqual(p["remote-app"], "||terminal64")  # the app itself is still mandatory

    def test_empty_alias_fails_closed(self):
        with self.assertRaises(ValueError):
            build_remoteapp_rdp_payload(
                username="u", windows_username="w", windows_password="p",
                host="h", remote_app="")

    def test_normalize_is_idempotent_and_prefixes(self):
        self.assertEqual(normalize_remote_app("terminal64"), "||terminal64")
        self.assertEqual(normalize_remote_app("||terminal64"), "||terminal64")
        self.assertEqual(normalize_remote_app("  terminal64  "), "||terminal64")

    def test_stable_conn_id_default_is_workspace(self):
        payload = build_remoteapp_rdp_payload(
            username="u", windows_username="w", windows_password="p", host="h",
            remote_app="terminal64")
        self.assertIn("mt5-workspace", payload["connections"])
        self.assertEqual(payload["connections"]["mt5-workspace"]["protocol"], "rdp")

    def test_dedicated_payload_unchanged_by_shared_base(self):
        """The base extraction must NOT alter the certified dedicated (full-desktop) payload."""
        params = build_dedicated_rdp_payload(
            username="ded", windows_username="guvfx_u_9", windows_password="pw",
            host="node-1")["connections"]["mt5-terminal"]["parameters"]
        self.assertEqual(params, {
            "hostname": "node-1", "port": "3389", "username": "guvfx_u_9", "password": "pw",
            "security": "any", "ignore-cert": "true", "color-depth": "24",
            "resize-method": "display-update", "enable-drive": "false", "enable-audio": "false",
            "disable-copy": "true", "disable-paste": "true",
        })
        # The dedicated payload must NOT carry RemoteApp params (that is the delivery variant only).
        self.assertNotIn("remote-app", params)


class NormalizeRemoteAppMutationAdequacy(SimpleTestCase):
    """Runnable mutation-adequacy harness for ``normalize_remote_app`` — the ``||`` contract + empty guard
    are single-app-lockdown-critical, so prove the tests KILL representative source mutants."""

    SOURCE = textwrap.dedent('''
        def normalize_remote_app(remote_app):
            app = (remote_app or "").strip()
            if not app:
                raise ValueError("empty")
            return app if app.startswith("||") else "||" + app
    ''')

    MUTANTS = [
        ('startswith("||")', 'startswith("|")'),      # wrong prefix check → double-|| or wrong prefix
        ('if not app:', 'if app:'),                    # invert empty guard
        ('"||" + app', 'app'),                          # drop the prefix entirely
        ('.strip()', ''),                               # drop whitespace normalisation
    ]

    def _oracle(self, ns):
        """Return True iff a mutant is KILLED. Any of: wrong output on a valid input, a raise on a valid
        input, or NO raise on empty input — all count as killed."""
        fn = ns["normalize_remote_app"]
        # Valid inputs must normalise correctly and never raise. A raise here = killed (empty-guard invert).
        for probe, expected in (("terminal64", "||terminal64"),
                                ("||terminal64", "||terminal64"),
                                ("  terminal64  ", "||terminal64")):
            try:
                if fn(probe) != expected:
                    return True
            except Exception:
                return True
        # Output invariant — the result ALWAYS starts with "||" (kills the ``startswith("|")`` mutant, whose
        # only non-equivalent input is a single-pipe alias like "|x").
        try:
            if not fn("|x").startswith("||"):
                return True
        except Exception:
            return True
        # Empty input MUST fail closed. No raise = killed.
        try:
            fn("")
            return True
        except ValueError:
            pass
        return False

    def test_baseline_survives(self):
        ns = {}
        exec(compile(self.SOURCE, "<baseline>", "exec"), ns)
        self.assertFalse(self._oracle(ns), "baseline must pass the oracle")

    def test_all_mutants_killed(self):
        for old, new in self.MUTANTS:
            src = self.SOURCE.replace(old, new, 1)
            self.assertIn(new, src, f"mutation {old!r}->{new!r} did not apply")
            ns = {}
            try:
                exec(compile(src, "<mutant>", "exec"), ns)
            except SyntaxError:
                continue  # a non-compiling mutant is trivially killed
            self.assertTrue(self._oracle(ns), f"MUTANT SURVIVED: {old!r} -> {new!r}")
