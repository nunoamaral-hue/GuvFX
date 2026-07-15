"""WS-H — stakeholder-card branding + privacy regression suite.

Locks the source/strategy branding split (TI header vs strategy subtitle), the WIN-only contract,
and the privacy invariants (no raw slug, no account number, no internal ids leak). All assertions
run against the ordered ``text``-op list from ``build_result_card_model`` (deterministic across
Pillow versions — a pixel/PNG golden would be brittle).
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from intelligence.results_card import _safe_account_label, build_result_card_model


def _leg(tp, entry, exit_, target, profit, pips, status="CLOSED"):
    return SimpleNamespace(tp_label=tp, direction="SELL", volume="0.40", entry=entry, exit=exit_,
                           target=target, profit=profit, status=status, pips=pips)


def _canonical(**kw):
    """Canonical WIN fixture. Includes decoy internal fields (correlation_id / execution_mode /
    signal_id) the renderer must NEVER surface, so the no-leak sweep is meaningful."""
    base = dict(
        provider="ti_signals", strategy="ti_signals", strategy_display_name="Wayond WIM Strategy",
        symbol="XAUUSD", direction="SELL", account_label="IS6FX", outcome="WIN",
        net_pnl="26.20", pips="131", gross_pnl="13.74",
        reference_entry="4025.95000", actual_fill="4024.82000", stop_loss="4028.41000",
        exit="4017.95000", take_profit="", take_profits=("4022.28000", "4020.43000", "4017.95000"),
        execution_timestamp="2026-07-15T11:14:08",
        progress={"closed": 3, "total": 3, "final": True},
        # decoys — present on the object, must not reach the card:
        correlation_id="CORR-SECRET-9f3a2b", execution_mode="AUTO_DEMO", signal_id="SIG-1302561-x",
        parser_profile="ti_v3",
        legs=(
            _leg("TP1", "4024.33000", "4022.28000", "4022.28000", "4.10", "20.5"),
            _leg("TP2", "4024.61000", "4020.43000", "4020.43000", "8.36", "41.8"),
            _leg("TP3", "4024.82000", "4017.95000", "4017.95000", "13.74", "68.7"),
        ),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _texts(r):
    _w, _h, ops = build_result_card_model(r)
    return [str(o.get("t", "")) for o in ops if o.get("op") == "text"]


class CardBrandingTests(SimpleTestCase):
    def test_source_and_strategy_split_co_present(self):
        # Header carries the SOURCE label; subtitle carries the STRATEGY name — both on one card.
        joined = " ".join(_texts(_canonical()))
        self.assertIn("TI Signals Trade Result", joined)
        self.assertIn("Wayond WIM Strategy", joined)

    def test_structural_order(self):
        # A light structural golden: the card's key sections must appear in the right order.
        texts = _texts(_canonical())

        def idx(substr):
            return next(i for i, t in enumerate(texts) if substr in t)

        order = [idx("TI Signals Trade Result"), idx("Wayond WIM Strategy"), idx("XAUUSD"),
                 idx("TOTAL PROFIT"), idx("TAKE-PROFIT RESULTS"), idx("TP1"), idx("TRADE ANALYSIS")]
        self.assertEqual(order, sorted(order), f"card sections out of order: {order}")


class CardPrivacyTests(SimpleTestCase):
    def test_no_raw_strategy_slug_when_display_name_blank(self):
        # H Fix 1: a blank strategy_display_name must NOT fall back to the raw slug — the subtitle
        # becomes the neutral "Automated strategy" instead of leaking "ti_signals".
        joined = " ".join(_texts(_canonical(strategy_display_name=""))).lower()
        self.assertNotIn("ti_signals", joined)
        self.assertIn("automated strategy", joined)

    def test_account_number_never_printed(self):
        # H Fix 2: an internal-name fallback that embeds the account number must be redacted.
        texts = _texts(_canonical(account_label="IS6 Demo (1302561)"))
        joined = " ".join(texts)
        self.assertNotIn("1302561", joined)
        self.assertIn("IS6 Demo", joined)

    def test_no_internal_id_leak_sweep(self):
        # No decoy internal id / mode / secret value ever appears on the card.
        joined = " ".join(_texts(_canonical())).lower()
        for banned in ("corr-secret-9f3a2b", "auto_demo", "sig-1302561-x", "ti_v3",
                       "correlation", "execution_mode", "shadow", "leg "):
            self.assertNotIn(banned, joined)

    def test_safe_account_label_helper(self):
        self.assertEqual(_safe_account_label("IS6 Demo (1302561)"), "IS6 Demo")
        self.assertEqual(_safe_account_label("1302561"), "Managed account")
        self.assertEqual(_safe_account_label(""), "Managed account")
        self.assertEqual(_safe_account_label(None), "Managed account")
        self.assertEqual(_safe_account_label("IS6FX"), "IS6FX")            # no number → unchanged


class CardWinOnlyTests(SimpleTestCase):
    def test_win_builds(self):
        self.assertTrue(_texts(_canonical(outcome="WIN")))

    def test_empty_outcome_treated_as_win(self):
        self.assertTrue(_texts(_canonical(outcome="")))

    def test_loss_refused_fail_closed(self):
        # H Fix 3: the WIN-only renderer must REFUSE a loss/breakeven — no green loss card.
        for oc in ("LOSS", "BREAKEVEN"):
            with self.assertRaises(ValueError):
                build_result_card_model(_canonical(outcome=oc))


class CardPrecisionTests(SimpleTestCase):
    def test_instrument_precision_matrix(self):
        cases = {
            "XAUUSD": ("4024.82000", "4024.82"),   # metals → 2dp
            "USDJPY": ("156.12300", "156.123"),     # JPY → 3dp
            "EURUSD": ("1.09876", "1.09876"),        # FX → 5dp (unchanged)
            "BTCUSD": ("64000.50000", "64000.50"),   # crypto → 2dp
        }
        for symbol, (raw, natural) in cases.items():
            r = _canonical(symbol=symbol, actual_fill=raw, reference_entry=raw, stop_loss=raw,
                           exit=raw, take_profits=(raw,), legs=(_leg("TP1", raw, raw, raw, "5.00", "10"),))
            joined = " ".join(_texts(r))
            self.assertIn(natural, joined, f"{symbol}: expected {natural}")
            if raw != natural:
                self.assertNotIn(raw, joined, f"{symbol}: raw {raw} must not appear")


class CardSingleLegTests(SimpleTestCase):
    def test_single_leg_fallback_renders(self):
        # legs=() → the synthesized TP1 row must still render.
        texts = _texts(_canonical(legs=()))
        self.assertIn("TP1", texts)
