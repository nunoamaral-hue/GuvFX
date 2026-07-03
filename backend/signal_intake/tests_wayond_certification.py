"""
GFX-PKT-WAYOND-PARSER-CERTIFICATION tests — the permanent Wayond regression suite.

Three layers:
  1. Corpus regression — every REAL message in wayond_corpus.json certifies safely
     and stays classified as certified (this file grows as Nuno supplies messages).
  2. Framework self-tests — classify()/verdict logic detects an entry signal, an
     update, and fail-closes on edit/media/empty/stale (uses illustrative text, NOT
     the real corpus).
  3. Drift guard — the pure classify() agrees with the REAL dispatcher
     (acquire_message) on the safety group, so the report can never silently diverge
     from the pipeline.
"""

import datetime as dt
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from signal_intake import certification as cert
from signal_intake.acquisition import acquire_message
from signal_intake.models import AcquiredMessage, ParserProfile, SignalProvider

# Illustrative (NOT corpus) — a well-formed Wayond-shaped signal / update, used only
# to prove the framework can positively detect the tradeable and update shapes.
SIGNAL_SHAPE = ("NZDJPY | Potential upward movement\nNZDJPY | BUY 91.300\n"
                "❌ Stop Loss 90.900 (40 pips)\n✅ TP1 91.450\n✅ TP2 91.700")
UPDATE_SHAPE = "TP1 hit! That gives us +250 pips. 🏅"
WARNING_SHAPE = "Remember we have NFP today at 14:30 CET so be carefull trading."


def _group(label):
    """Collapse a taxonomy label OR dispatcher outcome to a safety group."""
    if label in ("ENTRY_SIGNAL", AcquiredMessage.Outcome.INTAKEN):
        return "TRADED"
    if label in ("UPDATE", AcquiredMessage.Outcome.UPDATE):
        return "UPDATE"
    return "NEUTRAL"


class CorpusRegressionTests(SimpleTestCase):
    """The real corpus must always certify — no UNSAFE, no FAIL."""

    def test_corpus_loads_and_is_valid(self):
        entries = cert.load_corpus()
        self.assertGreaterEqual(len(entries), 1)

    def test_corpus_certifies_clean(self):
        report = cert.build_report()
        self.assertEqual(report["summary"]["unsafe"], [],
                         "a real Wayond message is UNSAFE — parser must be fixed")
        self.assertEqual(report["summary"]["verdicts"]["FAIL"], 0)
        self.assertTrue(report["summary"]["certified"])

    def test_no_non_entry_message_is_ever_tradeable(self):
        # The core safety property across the whole corpus.
        for r in cert.build_report()["results"]:
            if r["expected"] != "ENTRY_SIGNAL":
                self.assertNotEqual(r["observed"], "ENTRY_SIGNAL",
                                    f"{r['id']} ({r['expected']}) wrongly read as tradeable")


class FrameworkSelfTests(SimpleTestCase):
    def test_detects_entry_signal(self):
        self.assertEqual(cert.classify(SIGNAL_SHAPE), "ENTRY_SIGNAL")

    def test_detects_update(self):
        self.assertEqual(cert.classify(UPDATE_SHAPE), "UPDATE")

    def test_warning_is_unknown_not_tradeable(self):
        self.assertEqual(cert.classify(WARNING_SHAPE), "UNKNOWN")

    def test_real_sell_colon_format_is_entry_signal(self):
        # Real Wayond SELL format uses "STOP LOSS:" / "TP1:" with colons (corpus V1).
        sell = ("XAUUSD | Potential downward movement\nXAUUSD | SELL 4020\n"
                "❌ STOP LOSS: 4028 (80 pips)\n✅TP1: 4015\n✅TP2: 4010\n✅TP3: 4000")
        self.assertEqual(cert.classify(sell), "ENTRY_SIGNAL")
        from intelligence.telegram_source import parse_message
        p = parse_message(sell)
        self.assertEqual((p.market, p.direction, p.entry, p.stop_loss),
                         ("XAUUSD", "SELL", "4020", "4028"))
        self.assertEqual(p.take_profits, ("4015", "4010", "4000"))  # colon TPs parsed

    def test_real_sl_hit_is_an_update(self):
        self.assertEqual(cert.classify("SL hit"), "UPDATE")
        self.assertEqual(cert.classify("SL hit, 4035 for re-entries!"), "UPDATE")

    def test_edit_media_empty_stale_fail_closed_even_over_a_signal(self):
        # A signal-shaped body must NOT be tradeable when edited/media/empty/stale.
        self.assertEqual(cert.classify(SIGNAL_SHAPE, is_edit=True), "QUARANTINED")
        self.assertEqual(cert.classify(SIGNAL_SHAPE, media=True), "QUARANTINED")
        self.assertEqual(cert.classify(SIGNAL_SHAPE, stale=True), "STALE")
        self.assertEqual(cert.classify("   "), "QUARANTINED")

    def test_verdict_flags_missed_signal_and_false_positive_as_unsafe(self):
        self.assertEqual(cert._verdict("ENTRY_SIGNAL", "UNKNOWN"), ("FAIL", "UNSAFE"))
        self.assertEqual(cert._verdict("WARNING", "ENTRY_SIGNAL"), ("FAIL", "UNSAFE"))
        self.assertEqual(cert._verdict("ENTRY_SIGNAL", "ENTRY_SIGNAL"), ("PASS", "SAFE"))
        self.assertEqual(cert._verdict("WARNING", "UNKNOWN"), ("PASS", "SAFE"))

    def test_corpus_rejects_bad_expected_type(self):
        with self.assertRaises(ValueError):
            cert.build_report([{"id": "x", "expected_type": "NONSENSE", "text": "hi"}])


class DispatcherAgreementTests(TestCase):
    """The pure classifier must agree with the REAL dispatcher on the safety group for
    an ARMED, registered provider, across content + freshness: fresh / edited / media /
    empty / stale / indeterminate-date. The provider-arming gate (DROPPED_NOT_ARMED)
    and the unknown-parser-profile gate (QUARANTINED) are orthogonal, fail-closed
    dispatcher gates covered by tests_acquisition — classify() intentionally does not
    model them (it certifies the parser given a live provider)."""

    def setUp(self):
        self.profile = ParserProfile.objects.create(slug="wayond_v1")
        self.provider = SignalProvider.objects.create(
            slug="wayond", name="Wayond", telegram_chat_id="1001", chat_title="Wayond",
            parser_profile=self.profile, status=SignalProvider.Status.ARMED,
            acquisition_window_seconds=600,
        )
        self.now = timezone.now()
        self._n = 0

    def _dispatch_group(self, text, *, age_s=5, **extra):
        self._n += 1
        msg = {"message_id": f"m{self._n}", "chat_id": "1001", "text": text,
               "date": self.now - dt.timedelta(seconds=age_s)}
        msg.update(extra)
        acq = acquire_message(self.provider, msg, now=self.now)
        return _group(acq.outcome)

    def test_pure_classifier_matches_dispatcher_on_safety_group(self):
        cases = [
            # (text, classify-kwargs, dispatch-kwargs)
            (SIGNAL_SHAPE, {}, {}),
            (UPDATE_SHAPE, {}, {}),
            (WARNING_SHAPE, {}, {}),
            (SIGNAL_SHAPE, {"is_edit": True}, {"edit_date": 111}),   # edited over a signal
            (SIGNAL_SHAPE, {"media": True}, {"media": True}),
            ("   ", {}, {}),                                         # empty
            (SIGNAL_SHAPE, {"stale": True}, {"age_s": 5000}),        # stale over a signal
            (SIGNAL_SHAPE, {"stale": True}, {"date": None}),         # indeterminate date -> STALE
        ]
        for text, ckw, dkw in cases:
            pure = _group(cert.classify(text, **ckw))
            live = self._dispatch_group(text, **dkw)
            self.assertEqual(pure, live,
                             f"drift: classify={pure} dispatcher={live} for {text[:30]!r}")


class CertifyCommandTests(SimpleTestCase):
    def test_command_runs_and_reports_certified(self):
        out = StringIO()
        call_command("certify_wayond", stdout=out)
        text = out.getvalue()
        self.assertIn("WAYOND PARSER CERTIFICATION", text)
        self.assertIn("CERTIFIED", text)
