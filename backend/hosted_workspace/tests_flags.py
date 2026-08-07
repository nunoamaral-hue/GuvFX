"""Tests for hosted_workspace.flags — DARK-by-default, tolerant token set, settings-override-wins."""
from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from hosted_workspace import flags

_ACCESSORS = [
    ("HOSTED_PERSISTENT_MT5_ENABLED", flags.hosted_persistent_mt5_enabled),
    ("HOSTED_MT5_REMOTEAPP_ENABLED", flags.hosted_mt5_remoteapp_enabled),
    ("HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED", flags.hosted_mt5_active_account_polling_enabled),
]


class FlagDefaultOffTests(SimpleTestCase):
    def test_default_off_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name, fn in _ACCESSORS:
                os.environ.pop(name, None)
                self.assertFalse(fn(), f"{name} must default OFF")

    def test_each_truthy_token_enables(self):
        for name, fn in _ACCESSORS:
            for token in ("1", "true", "TRUE", "yes", "On", " on "):
                with mock.patch.dict(os.environ, {name: token}, clear=False):
                    self.assertTrue(fn(), f"{name}={token!r} should be ON")

    def test_falsey_tokens_stay_off(self):
        for name, fn in _ACCESSORS:
            for token in ("", "0", "false", "no", "off", "garbage"):
                with mock.patch.dict(os.environ, {name: token}, clear=False):
                    self.assertFalse(fn(), f"{name}={token!r} should be OFF")

    def test_settings_override_wins_over_env(self):
        # Settings value (even bool) overrides env per Idiom B.
        with mock.patch.dict(os.environ, {"HOSTED_PERSISTENT_MT5_ENABLED": "1"}, clear=False):
            with override_settings(HOSTED_PERSISTENT_MT5_ENABLED=False):
                self.assertFalse(flags.hosted_persistent_mt5_enabled())
            with override_settings(HOSTED_PERSISTENT_MT5_ENABLED="on"):
                self.assertTrue(flags.hosted_persistent_mt5_enabled())
