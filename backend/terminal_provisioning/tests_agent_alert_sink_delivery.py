"""Monitoring-Runner WS-E/H/K — external alert-DELIVERY sinks (Telegram + email fallback) and the factory.

Every test here defends a safety property the whole packet exists to guarantee: the ops alert channel never
hits the customer channel, never borrows the customer bot token, never leaks a secret, never raises, and
never silently swallows a FAILED send.
"""
from __future__ import annotations

import os
import urllib.error
from unittest import mock

from django.test import SimpleTestCase, override_settings

from terminal_provisioning import agent_alert_sink as sink_mod
from terminal_provisioning.agent_monitoring import Alert


def _alert(name="agent_down", severity="HIGH", detail="x"):
    return Alert(name, severity, "UNAVAILABLE", "agent-unavailable", detail)


# ────────────────────────── TelegramAlertSink ──────────────────────────
class TelegramAlertSinkTests(SimpleTestCase):
    def test_fail_closed_missing_owner_chat_or_token(self):
        with self.assertRaises(ValueError):
            sink_mod.TelegramAlertSink(owner="", chat_id="123", token="t")
        with self.assertRaises(ValueError):
            sink_mod.TelegramAlertSink(owner="nuno", chat_id="", token="t")
        with self.assertRaises(ValueError):
            # neither an injected transport NOR an own token => refuse (never fall through to customer token)
            sink_mod.TelegramAlertSink(owner="nuno", chat_id="123")

    def test_delivers_on_api_ok(self):
        sent = []
        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="999",
                                       transport=lambda t: sent.append(t) or {"ok": True})
        r = s.deliver(_alert(), now=1000.0, correlation_id="c1")
        self.assertTrue(r.delivered)
        self.assertEqual(len(sent), 1)

    def test_api_error_is_surfaced_not_raised(self):
        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="999",
                                       transport=lambda t: {"ok": False, "error_code": 403})
        r = s.deliver(_alert(), now=1000.0)
        self.assertFalse(r.delivered)
        self.assertEqual(r.reason, "api_error_403")

    def test_network_exception_sanitised_no_secret(self):
        def boom(_t):
            raise urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", 500, "boom", {}, None)
        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="999", transport=boom, attempts=1)
        r = s.deliver(_alert(), now=1000.0)
        self.assertFalse(r.delivered)
        self.assertEqual(r.reason, "http_500")
        self.assertNotIn("SECRET", r.reason)

    def test_bounded_retry_then_success(self):
        calls = {"n": 0}
        slept = []

        def flaky(_t):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError("temporary")
            return {"ok": True}

        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="9", transport=flaky, attempts=3,
                                       sleep_fn=slept.append)
        r = s.deliver(_alert(), now=1000.0)
        self.assertTrue(r.delivered)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(slept), 2)               # backoff between the 3 attempts

    def test_failed_send_is_not_debounced(self):
        """A FAILED send must not suppress the next probe's retry (the RR-11 trap)."""
        calls = {"n": 0}

        def always_fail(_t):
            calls["n"] += 1
            raise urllib.error.URLError("down")

        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="9", transport=always_fail, attempts=1,
                                       sleep_fn=lambda _s: None)
        r1 = s.deliver(_alert(), now=1000.0)
        r2 = s.deliver(_alert(), now=1001.0)          # within debounce window
        self.assertFalse(r1.delivered)
        self.assertFalse(r2.suppressed)               # NOT debounced — it retried
        self.assertEqual(calls["n"], 2)

    def test_success_is_debounced(self):
        calls = {"n": 0}
        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="9",
                                       transport=lambda t: calls.__setitem__("n", calls["n"] + 1) or {"ok": True},
                                       debounce_seconds=900)
        r1 = s.deliver(_alert(), now=1000.0)
        r2 = s.deliver(_alert(), now=1001.0)
        self.assertTrue(r1.delivered)
        self.assertTrue(r2.suppressed)
        self.assertEqual(calls["n"], 1)

    def test_message_is_ascii_bounded_and_secret_free(self):
        captured = {}
        s = sink_mod.TelegramAlertSink(owner="nuno-oncall", chat_id="SECRET_CHAT", token="SECRET_TOKEN",
                                       transport=lambda t: captured.__setitem__("t", t) or {"ok": True})
        s.deliver(_alert(detail="busy_rate=0.90"), now=1.0, correlation_id="corr-xyz")
        text = captured["t"]
        self.assertTrue(text.isascii())
        self.assertLessEqual(len(text), s.MAX_TEXT)
        for token in ("agent_down", "HIGH", "agent-unavailable", "nuno-oncall", "corr-xyz"):
            self.assertIn(token, text)
        self.assertNotIn("SECRET_TOKEN", text)
        self.assertNotIn("SECRET_CHAT", text)

    def test_recovery_message_reads_recovered(self):
        captured = {}
        s = sink_mod.TelegramAlertSink(owner="nuno", chat_id="9",
                                       transport=lambda t: captured.__setitem__("t", t) or {"ok": True})
        rec = Alert("agent_recovered", "RECOVERY", "HEALTHY", "agent-recovered", "back up")
        s.deliver(rec, now=1.0)
        self.assertIn("RECOVERED", captured["t"])


# ────────────────────────── build_alert_sink (telegram + collisions) ──────────────────────────
class BuildTelegramSinkTests(SimpleTestCase):
    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="ops-1", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="tok",
                       TELEGRAM_CHAT_ID="cust-9")
    def test_builds_telegram_when_fully_configured(self):
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.TelegramAlertSink)

    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="same", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="tok",
                       TELEGRAM_CHAT_ID="same")
    def test_refuses_when_ops_chat_equals_customer_chat(self):
        # the exact contamination we guard against: an ops page must never hit the customer channel
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="ops-1", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="shared",
                       TELEGRAM_BOT_TOKEN="shared", TELEGRAM_CHAT_ID="cust-9")
    def test_refuses_when_ops_token_equals_customer_token(self):
        # security RULE 3: the ops sink must not reuse the customer bot token
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    def test_collision_guard_reads_customer_chat_from_env(self):
        # the customer chat id lives in OS env (not a Django setting); the guard must consult env too, or it
        # is dead in every real deployment.
        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "env-customer"}, clear=False), \
             override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                               VALIDATION_AGENT_TELEGRAM_CHAT_ID="env-customer",
                               VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="tok"):
            self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="ops-1", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="")
    def test_fail_closed_without_own_token(self):
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="ops-1", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="tok")
    def test_fail_closed_without_owner(self):
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    @override_settings(AGENT_ALERT_SINK="telegram", AGENT_ALERT_OWNER="nuno",
                       VALIDATION_AGENT_TELEGRAM_CHAT_ID="", VALIDATION_AGENT_TELEGRAM_BOT_TOKEN="tok")
    def test_fail_closed_without_ops_chat(self):
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)


# ────────────────────────── EmailAlertSink (fallback) ──────────────────────────
class EmailAlertSinkTests(SimpleTestCase):
    def test_requires_owner_and_recipient(self):
        with self.assertRaises(ValueError):
            sink_mod.EmailAlertSink(owner="", recipient="a@b.com")
        with self.assertRaises(ValueError):
            sink_mod.EmailAlertSink(owner="nuno", recipient="not-an-email")

    def test_delivers_via_injected_send(self):
        sent = {}
        s = sink_mod.EmailAlertSink(owner="nuno", recipient="ops@guvfx.com",
                                    send_fn=lambda subj, body, to: sent.update(subj=subj, to=to))
        r = s.deliver(_alert(), now=1.0)
        self.assertTrue(r.delivered)
        self.assertEqual(sent["to"], "ops@guvfx.com")
        self.assertIn("agent_down", sent["subj"])

    def test_send_failure_surfaced_not_raised(self):
        def boom(_s, _b, _t):
            raise RuntimeError("smtp down")
        s = sink_mod.EmailAlertSink(owner="nuno", recipient="ops@guvfx.com", send_fn=boom)
        r = s.deliver(_alert(), now=1.0)
        self.assertFalse(r.delivered)
        self.assertEqual(r.reason, "email_error_RuntimeError")

    @override_settings(VALIDATION_AGENT_ALERT_FALLBACK_EMAIL="ops@guvfx.com", AGENT_ALERT_OWNER="nuno")
    def test_fallback_factory_builds_when_configured(self):
        self.assertIsInstance(sink_mod.build_fallback_email_sink(), sink_mod.EmailAlertSink)

    @override_settings(VALIDATION_AGENT_ALERT_FALLBACK_EMAIL="", AGENT_ALERT_OWNER="nuno")
    def test_fallback_factory_none_when_unset(self):
        self.assertIsNone(sink_mod.build_fallback_email_sink())
