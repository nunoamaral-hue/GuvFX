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
        for t in ("Cancel signal", "cancel this setup", "Ignore this setup",
                  "disregard this trade", "void the order"):
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
