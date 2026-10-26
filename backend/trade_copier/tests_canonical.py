"""Canonical economic-instrument resolution (packet §2): name + metadata, never name alone, dimensions SEPARATE.

Discriminating case: a USD-priced gold spread bet that settles in GBP has the CORRECT canonical identity
METAL:XAU:USD with HIGH confidence — the GBP settlement is a separate dimension (a compatibility concern),
not evidence the identity is wrong. These are PURE (no DB)."""
from decimal import Decimal

from django.test import SimpleTestCase

from trade_copier.canonical import CONF_HIGH, CONF_NONE, CT_SPOT, CT_SPREADBET, resolve_canonical

XAUUSD_META = {"contract_size": 100.0, "currency_base": "XAU", "currency_profit": "USD", "currency_margin": "XAU",
               "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "trade_mode": 4, "path": "Metals\\XAUUSD"}
XAUUSD_STD_META = {"contract_size": 100.0, "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
                   "volume_min": 0.01, "volume_step": 0.01, "volume_max": 30.0, "trade_mode": 4,
                   "path": "Spot Metals std\\XAUUSD.std"}
# The real Pepperstone #49 spread bet: USD gold pricing, GBP P&L, GBP margin, contract 1.
XAUUSD_SB_META = {"contract_size": 1.0, "currency_base": "GBP", "currency_profit": "GBP", "currency_margin": "GBP",
                  "volume_min": 1.0, "volume_step": 0.1, "volume_max": 10000.0, "trade_mode": 4,
                  "path": "Spread Bet Metals\\XAUUSD_SB"}
EURUSD_META = {"contract_size": 100000.0, "currency_base": "EUR", "currency_profit": "USD", "currency_margin": "EUR",
               "volume_min": 0.01, "volume_step": 0.01, "volume_max": 500.0, "trade_mode": 4, "path": "Forex\\EURUSD"}


class CanonicalResolutionTests(SimpleTestCase):
    def test_xauusd_identity_and_dimensions(self):
        r = resolve_canonical("XAUUSD", XAUUSD_META)
        self.assertEqual(r.key.slug, "METAL:XAU:USD")
        self.assertEqual((r.asset_class, r.underlying, r.quote_currency), ("METAL", "XAU", "USD"))
        self.assertEqual(r.settlement_currency, "USD")
        self.assertEqual(r.margin_currency, "XAU")          # margin currency modelled separately
        self.assertEqual(r.contract_type, CT_SPOT)
        self.assertEqual(r.contract_size, Decimal("100"))
        self.assertEqual(r.identity_confidence, CONF_HIGH)

    def test_xauusd_std_same_canonical(self):
        a = resolve_canonical("XAUUSD", XAUUSD_META)
        b = resolve_canonical("XAUUSD.std", XAUUSD_STD_META)
        self.assertEqual(a.key, b.key)
        self.assertEqual(b.identity_confidence, CONF_HIGH)

    def test_spread_bet_identity_is_correct_settlement_is_a_separate_dimension(self):
        """XAUUSD_SB: canonical identity METAL:XAU:USD is CORRECT and HIGH confidence; USD price vs GBP
        settlement is a separate dimension, NOT an identity defect and NOT read from currency_profit."""
        usd = resolve_canonical("XAUUSD", XAUUSD_META)
        sb = resolve_canonical("XAUUSD_SB", XAUUSD_SB_META)
        self.assertEqual(sb.key, usd.key)                   # same underlying/quote identity
        self.assertEqual(sb.identity_confidence, CONF_HIGH)  # identity is NOT downgraded by settlement
        self.assertEqual(sb.quote_currency, "USD")          # price reference (from the NAME)
        self.assertEqual(sb.settlement_currency, "GBP")     # P&L currency (separate)
        self.assertEqual(sb.margin_currency, "GBP")         # margin currency (separate)
        self.assertEqual(sb.contract_type, CT_SPREADBET)
        self.assertEqual(sb.contract_size, Decimal("1"))

    def test_quote_currency_not_taken_from_profit_currency(self):
        # Even though currency_profit is GBP, the quote (price) currency stays USD (from the name).
        sb = resolve_canonical("XAUUSD_SB", XAUUSD_SB_META)
        self.assertNotEqual(sb.quote_currency, sb.settlement_currency)
        self.assertEqual(sb.quote_currency, "USD")

    def test_fx_pair_resolves_fx_eur_usd(self):
        r = resolve_canonical("EURUSD", EURUSD_META)
        self.assertEqual(r.key.slug, "FX:EUR:USD")
        self.assertEqual(r.identity_confidence, CONF_HIGH)

    def test_unknown_symbol_has_no_canonical_key(self):
        r = resolve_canonical("US500", {"currency_profit": "USD", "path": "Indices\\US500"})
        self.assertIsNone(r.key)
        self.assertEqual(r.identity_confidence, CONF_NONE)
        self.assertIn("quote_currency_unresolved_from_name", r.identity_notes)

    def test_metadata_without_currencies_is_not_guessed_for_identity(self):
        r = resolve_canonical("XAUUSD", {"contract_size": 100.0})
        self.assertEqual(r.key.slug, "METAL:XAU:USD")       # name gives quote USD
        self.assertEqual(r.identity_confidence, CONF_HIGH)
        self.assertIsNone(r.settlement_currency)            # not guessed when the broker didn't say
