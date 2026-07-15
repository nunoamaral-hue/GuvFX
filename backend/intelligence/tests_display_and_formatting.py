"""GFX-PKT-STAKEHOLDER-BRANDING — public display labels + instrument-aware card formatting.

Presentation-only: internal slugs / account identity are never changed. The stakeholder card shows
human labels (TI Signals, IS6FX), instrument-natural prices, and NO internal ids / 'leg' wording.
"""
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from intelligence.display_labels import source_display_label
from intelligence.results_card import _price, build_result_card_model
from trading.models import TradingAccount

User = get_user_model()


class SourceLabelTests(SimpleTestCase):
    def test_known_slugs_map_to_human_labels(self):
        self.assertEqual(source_display_label("ti_signals"), "TI Signals")
        self.assertEqual(source_display_label("wayond"), "Wayond")

    def test_unknown_and_empty_degrade_safely(self):
        self.assertEqual(source_display_label("foo_bar"), "Foo Bar")
        self.assertEqual(source_display_label(""), "GuvFX")
        self.assertEqual(source_display_label(None), "GuvFX")

    def test_label_is_not_the_raw_slug(self):
        self.assertNotEqual(source_display_label("ti_signals"), "ti_signals")


class PriceFormatTests(SimpleTestCase):
    def test_instrument_aware_precision(self):
        self.assertEqual(_price("4024.33000", "XAUUSD"), "4024.33")   # metal 2dp
        self.assertEqual(_price("1.090000", "EURUSD"), "1.09000")     # FX 5dp
        self.assertEqual(_price("150.12345", "USDJPY"), "150.123")    # JPY 3dp
        self.assertEqual(_price("65000.5", "BTCUSD"), "65000.50")     # crypto 2dp

    def test_empty_and_non_numeric(self):
        self.assertEqual(_price("", "XAUUSD"), "")
        self.assertEqual(_price(None, "XAUUSD"), "")
        self.assertEqual(_price("n/a", "XAUUSD"), "n/a")


class PublicLabelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")

    def _acct(self, **kw):
        base = dict(user=self.user, name="IS6 Demo (1302561)", account_number="1302561", is_demo=True)
        base.update(kw)
        return TradingAccount.objects.create(**base)

    def test_public_name_used(self):
        self.assertEqual(self._acct(public_display_name="IS6FX").public_label(), "IS6FX")

    def test_number_hidden_by_default(self):
        self.assertNotIn("1302561", self._acct(public_display_name="IS6FX").public_label())

    def test_number_shown_only_when_opted_in(self):
        a = self._acct(public_display_name="IS6FX", public_show_account_number=True)
        self.assertEqual(a.public_label(), "IS6FX (1302561)")

    def test_fallback_to_internal_name_when_blank(self):
        self.assertEqual(self._acct().public_label(), "IS6 Demo (1302561)")


def _leg(tp, entry, exit_, target, profit, pips, status="CLOSED"):
    return SimpleNamespace(tp_label=tp, direction="SELL", volume="0.40", entry=entry, exit=exit_,
                           target=target, profit=profit, status=status, pips=pips)


def _fake_canonical(**kw):
    base = dict(
        provider="ti_signals", strategy="ti_signals", strategy_display_name="Wayond WIM Strategy",
        symbol="XAUUSD", direction="SELL", account_label="IS6FX", outcome="WIN",
        net_pnl="26.20", pips="131", gross_pnl="13.74",
        reference_entry="4025.95000", actual_fill="4024.82000", stop_loss="4028.41000",
        exit="4017.95000", take_profit="", take_profits=("4022.28000", "4020.43000", "4017.95000"),
        execution_timestamp="2026-07-15T11:14:08",
        progress={"closed": 3, "total": 3, "final": True},
        legs=(
            _leg("TP1", "4024.33000", "4022.28000", "4022.28000", "4.10", "20.5"),
            _leg("TP2", "4024.61000", "4020.43000", "4020.43000", "8.36", "41.8"),
            _leg("TP3", "4024.82000", "4017.95000", "4017.95000", "13.74", "68.7"),
        ),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class CardModelTests(SimpleTestCase):
    def _texts(self, r):
        _w, _h, ops = build_result_card_model(r)
        return [str(o.get("t", "")) for o in ops if o.get("op") == "text"]

    def test_header_is_human_label_not_slug(self):
        joined = " ".join(self._texts(_fake_canonical()))
        self.assertIn("TI Signals Trade Result", joined)
        self.assertNotIn("TI_SIGNALS", joined)

    def test_account_public_label_shown(self):
        self.assertIn("IS6FX", self._texts(_fake_canonical()))

    def test_prices_instrument_formatted_no_raw_5dp(self):
        joined = " ".join(self._texts(_fake_canonical()))
        self.assertIn("4024.82", joined)          # actual fill / TP3 entry at 2dp
        self.assertNotIn("4024.82000", joined)    # never the raw 5dp on a card

    def test_no_leg_wording_or_internal_ids(self):
        joined = " ".join(self._texts(_fake_canonical())).lower()
        for banned in ("leg", "correlation", "shadow", "execution_mode", "ti_signals"):
            self.assertNotIn(banned, joined)

    def test_progressive_tp_rows_present(self):
        texts = self._texts(_fake_canonical())
        for tp in ("TP1", "TP2", "TP3"):
            self.assertIn(tp, texts)
