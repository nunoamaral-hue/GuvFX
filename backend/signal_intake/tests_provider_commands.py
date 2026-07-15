"""WS-E — provider follow-up command classifier tests. The classifier is the safety-critical
pure component: a status update ("TP1 hit") must NEVER be read as a destructive command."""
from django.test import SimpleTestCase

from signal_intake.provider_commands import (
    classify_command, MOVE_SL_BE, MOVE_SL_PRICE, CLOSE_ALL, CLOSE_LEG, CANCEL,
    NON_ACTIONABLE, UNKNOWN,
)


class ClassifyCommandTests(SimpleTestCase):
    def _t(self, text, expected_type, args=None):
        c = classify_command(text)
        self.assertEqual(c.command_type, expected_type, f"{text!r} → {c.command_type} (want {expected_type})")
        if args is not None:
            self.assertEqual(c.args, args, f"{text!r} args")

    def test_move_sl_breakeven(self):
        for t in ("Move SL to BE", "move sl to breakeven", "SL to break even",
                  "Move stop to entry", "SL → BE now", "Move S/L to BE"):
            self._t(t, MOVE_SL_BE)

    def test_move_sl_price(self):
        self._t("Move SL to 4010", MOVE_SL_PRICE, {"price": "4010"})
        self._t("SL to 1,234.5", MOVE_SL_PRICE, {"price": "1234.5"})
        self._t("move stop loss to 3998.50", MOVE_SL_PRICE, {"price": "3998.50"})

    def test_close_leg(self):
        self._t("Close TP2", CLOSE_LEG, {"leg_index": 2})
        self._t("close tp 3 now", CLOSE_LEG, {"leg_index": 3})
        self._t("Close take profit 1", CLOSE_LEG, {"leg_index": 1})

    def test_close_all(self):
        for t in ("Close all", "close remaining trades", "Close everything",
                  "close all positions now", "Close the trade"):
            self._t(t, CLOSE_ALL)

    def test_cancel(self):
        for t in ("Cancel signal", "cancel this setup", "disregard this trade", "void the order",
                  "cancel the pending order"):
            self._t(t, CANCEL)

    def test_status_updates_are_non_actionable(self):
        # CRITICAL: a status update must never be a command.
        for t in ("TP1 hit ✅ +20 pips", "Running nicely in profit", "TP2 reached",
                  "SL hit, stopped out", "+41 pips secured"):
            self._t(t, NON_ACTIONABLE)

    def test_unknown(self):
        for t in ("gm traders", "", "   ", "great week everyone"):
            self._t(t, UNKNOWN)

    def test_breakeven_beats_price_and_status(self):
        # "TP1 hit, move SL to BE" carries a real command → MOVE_SL_BE (not NON_ACTIONABLE).
        self._t("TP1 hit ✅ move SL to BE", MOVE_SL_BE)

    def test_close_leg_beats_close_all(self):
        self._t("Close TP2 and hold rest", CLOSE_LEG, {"leg_index": 2})

    def test_status_word_without_verb_is_not_close(self):
        # "TP2 hit" (no imperative "close") must stay a status update, not CLOSE_LEG.
        self._t("TP2 hit", NON_ACTIONABLE)

    # --- adversarial-review regressions (proximity idioms + status reports) ---
    def test_close_to_idiom_is_not_a_close(self):
        # "close to X" is commentary, NOT an imperative close (the worst-case false positive).
        for t in ("EURUSD close to all-time high, keep holding",
                  "getting close to all our targets", "close to breakeven now",
                  "price is close to TP2"):
            c = classify_command(t)
            self.assertNotIn(c.command_type, (CLOSE_ALL, CLOSE_LEG), f"{t!r} → {c.command_type}")

    def test_sl_hit_report_is_not_move_sl(self):
        # A report that the SL was hit at a price must NOT be read as "move SL to price".
        for t in ("SL @ 4010 was hit", "stopped out at 4010", "SL 4010 hit"):
            c = classify_command(t)
            self.assertNotEqual(c.command_type, MOVE_SL_PRICE, f"{t!r} → {c.command_type}")

    def test_ignore_is_never_cancel(self):
        # "ignore" is bidirectional — "ignore it, still valid" tells followers to HOLD. It must
        # NEVER be read as a destructive cancel (the MUST-FIX). Only cancel/void/disregard cancel.
        for t in ("ignore the noise, we're up 40 pips", "SL wick, ignore it — position still valid",
                  "ignore it, trade is running fine", "ignore this setup"):
            self.assertNotEqual(classify_command(t).command_type, CANCEL, f"{t!r}")

    def test_pip_count_not_read_as_sl_price(self):
        # "SL at 20 pips" is a pip report, not "move SL to 20".
        for t in ("SL at 20 pips", "secured 40 pips, SL to 30 pips"):
            self.assertNotEqual(classify_command(t).command_type, MOVE_SL_PRICE, f"{t!r}")

    def test_close_comma_tp_is_not_close_leg(self):
        # "we're close, TP2 next" — adjective "close" + comma → not an imperative CLOSE_LEG.
        for t in ("we're close, TP2 next", "so close, TP2 should hit soon"):
            self.assertNotIn(classify_command(t).command_type, (CLOSE_LEG, CLOSE_ALL), f"{t!r}")
