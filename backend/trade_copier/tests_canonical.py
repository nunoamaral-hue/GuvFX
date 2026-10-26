"""Canonical economic-instrument resolution (packet §2): name+metadata, never name alone, fail-closed.

These are PURE (no DB): resolve_canonical only classifies strings + metadata."""
from decimal import Decimal

from django.test import SimpleTestCase

from trade_copier.canonical import CONF_HIGH, CONF_LOW, CONF_NONE, resolve_canonical

# Representative broker metadata (the fields sync_broker_instruments stores).
XAUUSD_META = {"contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD",
               "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "trade_mode": 4, "path": "Metals\\XAUUSD"}
XAUUSD_STD_META = {"contract_size": 100.0, "currency_base": "USD", "currency_profit": "USD",
                   "volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0, "trade_mode": 4,
                   "path": "Spot Metals std\\XAUUSD.std"}
# The real Pepperstone #49 spread bet: name says XAU/USD, but P&L settles in GBP and contract is 1.
XAUUSD_SB_META = {"contract_size": 1.0, "currency_base": "GBP", "currency_profit": "GBP",
                  "volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0, "trade_mode": 4,
                  "path": "Spread Bet Metals\\XAUUSD_SB"}
EURUSD_META = {"contract_size": 100000.0, "currency_base": "EUR", "currency_profit": "USD",
               "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\EURUSD"}


class CanonicalResolutionTests(SimpleTestCase):
    def test_xauusd_resolves_metal_xau_usd_high_confidence(self):
        r = resolve_canonical("XAUUSD", XAUUSD_META)
        self.assertEqual(r.key.slug, "METAL:XAU:USD")
        self.assertEqual((r.asset_class, r.underlying, r.quote_currency), ("METAL", "XAU", "USD"))
        self.assertEqual(r.settlement_currency, "USD")
        self.assertEqual(r.contract_size, Decimal("100"))
        self.assertEqual(r.confidence, CONF_HIGH)
        self.assertEqual(r.ambiguity_reasons, [])

    def test_xauusd_std_same_canonical_as_xauusd(self):
        a = resolve_canonical("XAUUSD", XAUUSD_META)
        b = resolve_canonical("XAUUSD.std", XAUUSD_STD_META)
        self.assertEqual(a.key, b.key)                 # like-for-like: same canonical key
        self.assertEqual(b.confidence, CONF_HIGH)

    def test_spread_bet_shares_underlying_but_is_low_confidence_not_name_only_equated(self):
        """XAUUSD_SB tracks the same underlying key as XAUUSD, but its GBP settlement + contract-size +
        spread-bet type are DETECTED and lower confidence — so the matrix can fail closed rather than
        equate it to a USD XAUUSD by name (packet §2)."""
        usd = resolve_canonical("XAUUSD", XAUUSD_META)
        sb = resolve_canonical("XAUUSD_SB", XAUUSD_SB_META)
        self.assertEqual(sb.key, usd.key)              # same underlying/quote key (price reference)
        self.assertEqual(sb.confidence, CONF_LOW)      # but NOT confidently equivalent
        self.assertEqual(sb.settlement_currency, "GBP")
        self.assertEqual(sb.contract_type, "SPREADBET")
        self.assertEqual(sb.contract_size, Decimal("1"))
        joined = " ".join(sb.ambiguity_reasons)
        self.assertIn("settlement_currency_differs_from_quote", joined)
        self.assertIn("spread_bet_contract_type", joined)

    def test_fx_pair_resolves_fx_eur_usd(self):
        r = resolve_canonical("EURUSD", EURUSD_META)
        self.assertEqual(r.key.slug, "FX:EUR:USD")
        self.assertEqual(r.confidence, CONF_HIGH)

    def test_unknown_symbol_has_no_canonical_key(self):
        r = resolve_canonical("US500", {"currency_profit": "USD", "path": "Indices\\US500"})
        self.assertIsNone(r.key)                       # cannot confidently split underlying/quote
        self.assertEqual(r.confidence, CONF_NONE)
        self.assertIn("quote_currency_unresolved_from_name", r.ambiguity_reasons)

    def test_metadata_without_currencies_is_not_guessed(self):
        r = resolve_canonical("XAUUSD", {"contract_size": 100.0})   # no currency_profit
        self.assertEqual(r.key.slug, "METAL:XAU:USD")               # name gives quote USD
        self.assertEqual(r.settlement_currency, "USD")              # falls back to quote (no contradiction)
        self.assertEqual(r.confidence, CONF_HIGH)
