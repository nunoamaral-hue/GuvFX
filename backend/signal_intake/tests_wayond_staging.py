"""
GFX-PKT-WAYOND-CORPUS-SEED-READY tests — the real-message intake workflow.

Covers the paste parser (split + @directives), staging (classify + propose + review
flags), promotion (only CONFIRMED, dedup, never touches the real corpus in tests),
and the certification-confidence metric. No Telegram, no DB, no order.
"""

import json
import os
import tempfile

from django.test import SimpleTestCase

from signal_intake import certification as cert
from signal_intake import staging

SIGNAL_SHAPE = ("EURUSD | BUY 1.0850\n❌ Stop Loss 1.0820 (30 pips)\n✅ TP1 1.0880")
UPDATE_SHAPE = "TP1 hit! +30 pips 🎯"
WARNING_SHAPE = "Heads up: high-impact news at 13:30, trade carefully."
CHATTER_SHAPE = "gm everyone, good luck today"


class PasteParsingTests(SimpleTestCase):
    def test_splits_on_delimiter_and_skips_empty(self):
        paste = f"{SIGNAL_SHAPE}\n---\n{WARNING_SHAPE}\n---\n   \n"
        entries = staging.parse_paste(paste)
        self.assertEqual(len(entries), 2)  # blank trailing block dropped
        self.assertIn("BUY 1.0850", entries[0]["text"])

    def test_directives_parsed(self):
        paste = "@type: ENTRY_SIGNAL\n@edit\n@id: my-buy\n" + SIGNAL_SHAPE
        [e] = staging.parse_paste(paste)
        self.assertEqual(e["declared_type"], "ENTRY_SIGNAL")
        self.assertEqual(e["id"], "my-buy")
        self.assertTrue(e["meta"]["is_edit"])
        self.assertTrue(e["text"].startswith("EURUSD"))  # directives stripped from body

    def test_at_sign_inside_body_is_not_a_directive(self):
        [e] = staging.parse_paste("real signal below\n@type: ENTRY_SIGNAL")
        # @type is not a LEADING line, so it stays body and nothing is declared.
        self.assertIsNone(e["declared_type"])

    def test_directive_after_blank_line_is_parsed(self):
        # Blank lines between the delimiter and the directive must not defeat parsing.
        [e] = staging.parse_paste(f"\n\n@type: WARNING\n@edit\n{WARNING_SHAPE}")
        self.assertEqual(e["declared_type"], "WARNING")
        self.assertTrue(e["meta"]["is_edit"])
        self.assertNotIn("@type", e["text"])          # directive not left in body

    def test_leading_at_mention_is_preserved_in_body(self):
        # A real @mention must NOT be eaten as a directive (critical data-loss guard).
        [e] = staging.parse_paste("@trader_bob says BUY EURUSD 1.0850\nStop Loss 1.0820")
        self.assertIsNone(e["declared_type"])
        self.assertIn("@trader_bob says BUY EURUSD 1.0850", e["text"])

    def test_two_directives_on_one_line_stay_in_body_not_silently_lost(self):
        [e] = staging.parse_paste("@edit @type: WARNING\n" + WARNING_SHAPE)
        # Not a clean single directive per line -> treated as body, visible for the
        # user to fix (no silent loss); nothing declared, no flag set.
        self.assertIsNone(e["declared_type"])
        self.assertEqual(e["meta"], {})
        self.assertIn("@edit @type: WARNING", e["text"])


class StagingTests(SimpleTestCase):
    def test_undeclared_proposes_observed_and_needs_review(self):
        [e] = staging.stage_entries(SIGNAL_SHAPE)
        self.assertEqual(e["observed"], "ENTRY_SIGNAL")
        self.assertEqual(e["expected_type"], "ENTRY_SIGNAL")  # proposal = observed
        self.assertFalse(e["confirmed"])
        self.assertTrue(e["needs_review"])                    # a proposed trade

    def test_declared_type_is_confirmed_and_verdicted(self):
        [e] = staging.stage_entries("@type: WARNING\n" + WARNING_SHAPE)
        self.assertTrue(e["confirmed"])
        self.assertEqual(e["expected_type"], "WARNING")
        self.assertEqual(e["observed"], "UNKNOWN")
        self.assertEqual(e["verdict"], "PASS")               # WARNING safely quarantined

    def test_signal_shaped_but_not_tradeable_is_flagged_for_review(self):
        # Looks like a signal (has BUY / Stop Loss) but no full order+SL -> UNKNOWN.
        [e] = staging.stage_entries("@type: CHATTER\nThinking about a EURUSD BUY maybe?")
        self.assertEqual(e["observed"], "UNKNOWN")
        self.assertTrue(e["needs_review"])   # signal-hint but not tradeable -> check

    def test_bad_declared_type_falls_back_to_unconfirmed(self):
        [e] = staging.stage_entries("@type: NONSENSE\n" + WARNING_SHAPE)
        self.assertFalse(e["confirmed"])
        self.assertTrue(e["needs_review"])


class PromoteTests(SimpleTestCase):
    def _tmp_corpus(self):
        path = os.path.join(tempfile.mkdtemp(), "corpus.json")
        with open(path, "w") as fh:
            json.dump({"messages": []}, fh)
        return path

    def test_only_confirmed_entries_are_promoted(self):
        corpus = self._tmp_corpus()
        staged = staging.stage_entries(
            f"@type: ENTRY_SIGNAL\n{SIGNAL_SHAPE}\n---\n{WARNING_SHAPE}")  # 2nd undeclared
        result = staging.promote(staged, corpus)
        self.assertEqual(len(result["added"]), 1)
        self.assertTrue(any(s["reason"] == "unconfirmed" for s in result["skipped"]))
        data = json.load(open(corpus))
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["expected_type"], "ENTRY_SIGNAL")

    def test_duplicate_text_is_skipped(self):
        corpus = self._tmp_corpus()
        staged = staging.stage_entries("@type: WARNING\n" + WARNING_SHAPE)
        staging.promote(staged, corpus)
        result = staging.promote(staged, corpus)  # promote again
        self.assertEqual(result["added"], [])
        self.assertTrue(any(s["reason"] == "duplicate_text" for s in result["skipped"]))

    def test_promoted_corpus_certifies(self):
        corpus = self._tmp_corpus()
        paste = (f"@type: ENTRY_SIGNAL\n{SIGNAL_SHAPE}\n---\n"
                 f"@type: UPDATE\n{UPDATE_SHAPE}\n---\n@type: WARNING\n{WARNING_SHAPE}")
        staging.promote(staging.stage_entries(paste), corpus)
        report = cert.build_report(cert.load_corpus(corpus))
        self.assertTrue(report["summary"]["certified"])
        self.assertEqual(report["summary"]["unsafe"], [])

    def test_missing_text_entry_is_skipped_not_crash(self):
        corpus = self._tmp_corpus()
        result = staging.promote(
            [{"id": "x", "confirmed": True, "expected_type": "WARNING"}], corpus)
        self.assertEqual(result["added"], [])
        self.assertTrue(any(s["reason"] == "missing_text" for s in result["skipped"]))

    def test_malformed_corpus_raises_valueerror(self):
        path = os.path.join(tempfile.mkdtemp(), "bad.json")
        with open(path, "w") as fh:
            fh.write("[]")   # not an object with a 'messages' list
        with self.assertRaises(ValueError):
            staging.promote([], path)

    def test_parser_gap_is_added_but_flagged_unsafe(self):
        # A real message CONFIRMED as ENTRY_SIGNAL that the parser misses -> added
        # (it's real) but surfaced as a parser gap so certification will fail loudly.
        corpus = self._tmp_corpus()
        staged = [{"id": "missed-buy", "text": "buy eurusd soon maybe",
                   "confirmed": True, "expected_type": "ENTRY_SIGNAL", "meta": {}}]
        result = staging.promote(staged, corpus)
        self.assertIn("missed-buy", result["added"])
        self.assertTrue(any(u["id"] == "missed-buy" for u in result["unsafe"]))


class ConfidenceTests(SimpleTestCase):
    def _report(self, pairs):
        entries = [{"id": f"e{i}", "expected_type": t, "text": txt, "meta": meta}
                   for i, (t, txt, meta) in enumerate(pairs)]
        return cert.build_report(entries)

    def test_warning_only_corpus_is_low_confidence(self):
        conf = cert.certification_confidence(self._report([("WARNING", WARNING_SHAPE, {})]))
        self.assertEqual(conf["level"], "LOW")
        self.assertIn("ENTRY_SIGNAL", conf["safety_critical_missing"])

    def test_safety_critical_only_is_medium(self):
        conf = cert.certification_confidence(self._report([
            ("ENTRY_SIGNAL", SIGNAL_SHAPE, {}),
            ("UPDATE", UPDATE_SHAPE, {}),
        ]))
        self.assertEqual(conf["level"], "MEDIUM")

    def test_full_coverage_is_high(self):
        conf = cert.certification_confidence(self._report([
            ("ENTRY_SIGNAL", SIGNAL_SHAPE, {}),
            ("UPDATE", UPDATE_SHAPE, {}),
            ("WARNING", WARNING_SHAPE, {}),
            ("CHATTER", CHATTER_SHAPE, {}),
            ("QUARANTINED", SIGNAL_SHAPE, {"media": True}),
            ("UNKNOWN", "totally random text", {}),
        ]))
        self.assertEqual(conf["level"], "HIGH")
        self.assertEqual(conf["missing"], [])

    def test_unsafe_forces_low(self):
        # A real signal the parser MISSES: expected ENTRY_SIGNAL, observed UNKNOWN.
        conf = cert.certification_confidence(self._report([("ENTRY_SIGNAL", "buy soon?", {})]))
        self.assertEqual(conf["level"], "LOW")

    def test_full_coverage_with_a_degraded_is_not_high(self):
        # All target types covered, but one real UPDATE the parser misclassifies
        # (DEGRADED) must hold confidence at MEDIUM, not HIGH.
        conf = cert.certification_confidence(self._report([
            ("ENTRY_SIGNAL", SIGNAL_SHAPE, {}),
            ("UPDATE", UPDATE_SHAPE, {}),               # PASS
            ("UPDATE", "not really an update", {}),     # DEGRADED (missed)
            ("WARNING", WARNING_SHAPE, {}),
            ("CHATTER", CHATTER_SHAPE, {}),
            ("QUARANTINED", SIGNAL_SHAPE, {"media": True}),
            ("UNKNOWN", "random text", {}),
        ]))
        self.assertEqual(conf["level"], "MEDIUM")
        self.assertIn("degraded", conf["rationale"])
