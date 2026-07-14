"""Tests for the TI Signals parser (intelligence.ti_signals_source) and its registration
in the signal_intake parser registry.

Covers: the real TI entry format (BUY/SELL) → tradeable SIGNAL with the mid used as entry;
range-less entry fallback; quarantine (UNKNOWN) on a header without a timeframe or without a
stop-loss; update messages (TP hit / move SL / SL hit); never-raises on garbage; and that the
TI and Wayond parsers are mutually exclusive on each other's format.
"""
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from intelligence.telegram_source import Kind, parse_message as wayond_parse
from intelligence.ti_signals_source import parse_ti_signals
from signal_intake.acquisition import acquire_message
from signal_intake.models import (
    AcquiredMessage, MessageAmendment, ParserProfile, PendingSignalApproval, SignalProvider,
)
from signal_intake.parsers import get_parser, registered_profiles

TI_BUY = (
    "🔔 XAUUSD BUY (M15)\n"
    "Entry: 4019.25-4020.82 (mid 4020.03)\n"
    "TP1: 4023.67\nTP2: 4025.28\nTP3: 4027.43\n"
    "SL: 4017.61\n"
    "有效期限: 2026-07-14 05:17 UTC"
)
TI_SELL = (
    "🔔 XAUUSD SELL (M15)\n"
    "Entry: 4018.78-4020.20 (mid 4019.49)\n"
    "TP1: 4016.19\nTP2: 4014.72\nTP3: 4012.77\n"
    "SL: 4021.69\n"
    "有效期限: 2026-07-14 08:18 UTC"
)


class TiSignalsParserTests(SimpleTestCase):
    def test_buy_entry_parses_to_tradeable_signal_with_mid_entry(self):
        p = parse_ti_signals(TI_BUY, message_id="10")
        self.assertEqual(p.kind, Kind.SIGNAL)
        self.assertTrue(p.is_tradeable_shape())
        self.assertEqual(p.market, "XAUUSD")
        self.assertEqual(p.direction, "BUY")
        self.assertEqual(p.entry, "4020.03")          # the mid, not the range bounds
        self.assertEqual(p.stop_loss, "4017.61")
        self.assertEqual(p.take_profits, ("4023.67", "4025.28", "4027.43"))
        self.assertEqual(p.expiry, "2026-07-14T05:17:00+00:00")   # 有効期限 → ISO UTC
        self.assertEqual(p.message_id, "10")

    def test_sell_entry_parses(self):
        p = parse_ti_signals(TI_SELL, message_id="11")
        self.assertEqual(p.kind, Kind.SIGNAL)
        self.assertEqual(p.direction, "SELL")
        self.assertEqual(p.entry, "4019.49")
        self.assertEqual(p.stop_loss, "4021.69")
        self.assertEqual(p.take_profits[0], "4016.19")

    def test_entry_without_range_uses_single_value(self):
        text = "🔔 EURUSD BUY (M15)\nEntry: 1.0850\nTP1: 1.0900\nSL: 1.0800"
        p = parse_ti_signals(text)
        self.assertEqual(p.kind, Kind.SIGNAL)
        self.assertEqual(p.entry, "1.0850")

    def test_header_without_timeframe_is_quarantined(self):
        # No "(TF)" → not a TI header → UNKNOWN (never guessed into a signal).
        p = parse_ti_signals("XAUUSD BUY\nSL: 4000\nTP1: 4010")
        self.assertEqual(p.kind, Kind.UNKNOWN)
        self.assertFalse(p.is_tradeable_shape())

    def test_header_without_stop_loss_is_quarantined(self):
        p = parse_ti_signals("🔔 XAUUSD BUY (M15)\nEntry: 4019.25-4020.82 (mid 4020.03)\nTP1: 4023.67")
        self.assertEqual(p.kind, Kind.UNKNOWN)

    def test_tp_hit_update(self):
        p = parse_ti_signals("TP1 hit! +25 pips locked 🎯", message_id="u1")
        self.assertEqual(p.kind, Kind.UPDATE)
        self.assertEqual(p.update_type, "TP_HIT")
        self.assertEqual(p.pips, "+25")   # sign preserved, matching the Wayond parser

    def test_move_sl_update(self):
        p = parse_ti_signals("Move SL to 4020")
        self.assertEqual(p.kind, Kind.UPDATE)
        self.assertEqual(p.update_type, "MOVE_SL")
        self.assertEqual(p.new_stop_loss, "4020")

    def test_never_raises_on_garbage(self):
        for junk in ["", "   ", "hello world", "🔔🔔🔔", None]:
            p = parse_ti_signals(junk)  # type: ignore[arg-type]
            self.assertIn(p.kind, (Kind.SIGNAL, Kind.UPDATE, Kind.UNKNOWN))

    def test_prose_mentioning_symbol_direction_is_not_a_signal(self):
        # Header must begin a line AND carry a real timeframe token; commentary must NOT be
        # promoted to a tradeable signal (adversarial-review finding).
        prose1 = ("Market recap for today.\nWe closed EURUSD BUY (daily) target.\n"
                  "SL: 1.0800 was our stop.\nEntry: 1.0850 filled.")
        prose2 = "Analysis: GOLDX SELL (setup) looks primed today.\nSL: 4030\nEntry: 4025"
        self.assertEqual(parse_ti_signals(prose1).kind, Kind.UNKNOWN)
        self.assertEqual(parse_ti_signals(prose2).kind, Kind.UNKNOWN)

    def test_update_recapping_header_is_not_a_fresh_signal(self):
        # A follow-up that recaps the header/SL block must classify as UPDATE, not a re-entry.
        sl_hit = ("SL hit on XAUUSD SELL (M15)\n"
                  "Entry: 4019.25-4020.82 (mid 4020.03)\nSL: 4021.69")
        self.assertEqual(parse_ti_signals(sl_hit).update_type, "SL_HIT")
        tp_recap = ("🔔 XAUUSD BUY (M15) UPDATE\n"
                    "Entry: 4019.25-4020.82 (mid 4020.03)\nSL: 4017.61\nTP1 hit! +30 pips 🎯")
        self.assertEqual(parse_ti_signals(tp_recap).update_type, "TP_HIT")

    def test_bare_range_without_mid_is_quarantined(self):
        # A range with no explicit mid is ambiguous — quarantine, never grab a bound.
        p = parse_ti_signals("🔔 XAUUSD SELL (M15)\nEntry: 4019.25-4020.82\nSL: 4021.69\nTP1: 4016.19")
        self.assertEqual(p.kind, Kind.UNKNOWN)

    def test_mid_delimiter_variants_extract_the_mid(self):
        for frag in ["(mid 4020.03)", "(mid: 4020.03)", "(mid = 4020.03)", "(mid=4020.03)"]:
            text = f"🔔 XAUUSD BUY (M15)\nEntry: 4019.25-4020.82 {frag}\nSL: 4017.61\nTP1: 4023.67"
            p = parse_ti_signals(text)
            self.assertEqual(p.kind, Kind.SIGNAL, frag)
            self.assertEqual(p.entry, "4020.03", frag)

    def test_ti_and_wayond_parsers_are_mutually_exclusive(self):
        # A Wayond-format message is NOT a TI signal…
        wayond_text = "EURUSD | BUY 1.0850\n❌ Stop Loss 1.0800\n✅ TP1 1.0900"
        self.assertEqual(parse_ti_signals(wayond_text).kind, Kind.UNKNOWN)
        # …and a TI-format message is NOT a Wayond signal.
        self.assertEqual(wayond_parse(TI_BUY).kind, Kind.UNKNOWN)


    def test_expiry_delimiter_and_english_variants(self):
        base = "🔔 XAUUSD BUY (M15)\nEntry: 4019.25-4020.82 (mid 4020.03)\nSL: 4017.61\nTP1: 4023.67\n"
        for line in ["有効期限: 2026-07-14 05:17 UTC", "Valid until 2026-07-14 05:17 UTC",
                     "Expiry: 2026-07-14 05:17"]:
            self.assertEqual(parse_ti_signals(base + line).expiry, "2026-07-14T05:17:00+00:00", line)


class TiSignalsRegistryTests(SimpleTestCase):
    def test_profile_registered(self):
        self.assertIn("ti_signals_v1", registered_profiles())

    def test_get_parser_dispatches_ti_format(self):
        parser = get_parser("ti_signals_v1")
        p = parser(TI_BUY, "20")
        self.assertEqual(p.kind, Kind.SIGNAL)
        self.assertEqual(p.market, "XAUUSD")


class TiSignalsExpiryAcquisitionTests(TestCase):
    """The expiry gate is enforced at intake: an expired TI signal is quarantined (no approval,
    no order); a still-valid one is intaken as before."""

    def setUp(self):
        self.parser = ParserProfile.objects.create(
            slug="ti_signals_v1", certification_level=ParserProfile.CertificationLevel.MEDIUM,
        )
        self.provider = SignalProvider.objects.create(
            slug="ti_signals", name="TI Signals", telegram_chat_id="-100777",
            parser_profile=self.parser, status=SignalProvider.Status.ARMED,
        )

    def _msg(self, mid, expiry_dt, now):
        text = (
            "🔔 XAUUSD BUY (M15)\nEntry: 4019.25-4020.82 (mid 4020.03)\n"
            "TP1: 4023.67\nTP2: 4025.28\nTP3: 4027.43\nSL: 4017.61\n"
            f"有効期限: {expiry_dt:%Y-%m-%d %H:%M} UTC"
        )
        return {"message_id": mid, "chat_id": "-100777", "text": text, "date": now}

    def test_expired_signal_is_quarantined_not_intaken(self):
        now = timezone.now()
        acq = acquire_message(self.provider, self._msg("e1", now - timedelta(minutes=1), now), now=now)
        self.assertEqual(acq.outcome, AcquiredMessage.Outcome.STALE)
        self.assertEqual(acq.reason, "signal_expired")
        self.assertEqual(PendingSignalApproval.objects.count(), 0)

    def test_valid_signal_is_intaken(self):
        now = timezone.now()
        acq = acquire_message(self.provider, self._msg("v1", now + timedelta(hours=1), now), now=now)
        self.assertEqual(acq.outcome, AcquiredMessage.Outcome.INTAKEN)
        self.assertEqual(PendingSignalApproval.objects.count(), 1)

    def test_edit_into_expired_signal_creates_no_approval(self):
        # An original non-signal edited into an already-expired TI signal records the amendment
        # but creates NO tradeable approval (same fail-closed rule as the fresh path).
        now = timezone.now()
        acquire_message(self.provider,
                        {"message_id": "a1", "chat_id": "-100777", "text": "morning all", "date": now}, now=now)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        edited = self._msg("a1", now - timedelta(minutes=1), now)
        edited["edit_date"] = now
        acquire_message(self.provider, edited, now=now)
        self.assertEqual(PendingSignalApproval.objects.count(), 0)
        self.assertEqual(MessageAmendment.objects.count(), 1)  # audit record still written

    def test_edit_into_valid_signal_creates_flagged_approval(self):
        now = timezone.now()
        acquire_message(self.provider,
                        {"message_id": "a2", "chat_id": "-100777", "text": "morning all", "date": now}, now=now)
        edited = self._msg("a2", now + timedelta(hours=1), now)
        edited["edit_date"] = now
        acquire_message(self.provider, edited, now=now)
        self.assertEqual(PendingSignalApproval.objects.count(), 1)
