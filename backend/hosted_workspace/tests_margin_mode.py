"""B1 — margin-mode int->label + freshness, the capability read-model, and the observation plumbing
(RawWorkspaceSnapshot -> WorkspaceObservation carry-through). No DB (pure/derivation tests)."""
import datetime

from django.test import SimpleTestCase
from django.utils import timezone

from hosted_workspace import margin_mode as mm
from hosted_workspace.onboarding_read_model import capability_projection
from hosted_workspace.producer import RawWorkspaceSnapshot, build_workspace_observation


class _WS:
    """Duck-typed HostedMt5Workspace stub for the pure read-model derivations."""
    def __init__(self, raw, last):
        self.proj_margin_mode = raw
        self.last_decision_at = last


class MarginModeMappingTests(SimpleTestCase):
    def test_known_ints_map(self):
        self.assertEqual(mm.label_for(mm.RETAIL_HEDGING), mm.HEDGING)
        self.assertEqual(mm.label_for(mm.RETAIL_NETTING), mm.NETTING)
        self.assertEqual(mm.label_for(mm.EXCHANGE), mm.EXCHANGE_LABEL)

    def test_unknown_and_malformed_map_to_unknown(self):
        for bad in (None, 3, 99, -1, "2", 2.0, True, False, object()):
            self.assertEqual(mm.label_for(bad), mm.UNKNOWN, bad)

    def test_supports_independent_same_symbol_only_hedging(self):
        self.assertTrue(mm.supports_independent_same_symbol(mm.HEDGING))
        for label in (mm.NETTING, mm.EXCHANGE_LABEL, mm.UNKNOWN):
            self.assertFalse(mm.supports_independent_same_symbol(label))

    def test_label_if_fresh(self):
        now = timezone.now()
        fresh = now - datetime.timedelta(hours=1)
        stale = now - datetime.timedelta(hours=48)
        future = now + datetime.timedelta(hours=2)
        self.assertEqual(mm.label_if_fresh(mm.RETAIL_HEDGING, fresh, now), mm.HEDGING)
        self.assertEqual(mm.label_if_fresh(mm.RETAIL_HEDGING, stale, now), mm.UNKNOWN)   # stale -> UNKNOWN
        self.assertEqual(mm.label_if_fresh(mm.RETAIL_HEDGING, future, now), mm.UNKNOWN)  # future skew -> UNKNOWN
        self.assertEqual(mm.label_if_fresh(mm.RETAIL_HEDGING, None, now), mm.UNKNOWN)    # never observed
        self.assertEqual(mm.label_if_fresh(0, fresh, now), mm.NETTING)


class CapabilityProjectionTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.fresh = self.now - datetime.timedelta(hours=1)
        self.stale = self.now - datetime.timedelta(hours=48)

    def test_fresh_hedging_full(self):
        cap = capability_projection(_WS(mm.RETAIL_HEDGING, self.fresh), now=self.now)
        self.assertEqual(cap["margin_mode"], mm.HEDGING)
        self.assertEqual(cap["multi_strategy"], "FULL")
        self.assertTrue(cap["same_symbol_concurrent"])
        self.assertTrue(cap["capability_fresh"])

    def test_fresh_netting_limited(self):
        cap = capability_projection(_WS(mm.RETAIL_NETTING, self.fresh), now=self.now)
        self.assertEqual(cap["margin_mode"], mm.NETTING)
        self.assertEqual(cap["multi_strategy"], "LIMITED")
        self.assertFalse(cap["same_symbol_concurrent"])

    def test_fresh_exchange_limited(self):
        cap = capability_projection(_WS(mm.EXCHANGE, self.fresh), now=self.now)
        self.assertEqual(cap["margin_mode"], mm.EXCHANGE_LABEL)
        self.assertEqual(cap["multi_strategy"], "LIMITED")

    def test_stale_is_unknown_pending(self):
        cap = capability_projection(_WS(mm.RETAIL_HEDGING, self.stale), now=self.now)
        self.assertEqual(cap["margin_mode"], mm.UNKNOWN)
        self.assertEqual(cap["multi_strategy"], "PENDING")
        self.assertFalse(cap["same_symbol_concurrent"])
        self.assertFalse(cap["capability_fresh"])

    def test_no_workspace_is_unknown_pending(self):
        cap = capability_projection(None, now=self.now)
        self.assertEqual(cap["margin_mode"], mm.UNKNOWN)
        self.assertEqual(cap["multi_strategy"], "PENDING")
        self.assertFalse(cap["same_symbol_concurrent"])
        self.assertIsNone(cap["capability_observed_at"])


class ObservationPlumbingTests(SimpleTestCase):
    def _snap(self, margin):
        now = timezone.now().timestamp()
        return RawWorkspaceSnapshot(
            expected_login="123", expected_server="Demo",
            process_running=True, attach_attempted=True, attach_succeeded=True, ipc_available=True,
            terminal_connected=True, trade_allowed=True,
            observed_login="123", observed_server="Demo", observed_trade_mode=0,
            observed_margin_mode=margin, observed_at=now, freshness_limit_seconds=60.0)

    def test_margin_mode_carries_to_observation(self):
        obs = build_workspace_observation(self._snap(mm.RETAIL_HEDGING), now=timezone.now().timestamp(),
                                          previous_state="PROVISIONING")
        self.assertEqual(obs.margin_mode, mm.RETAIL_HEDGING)

    def test_bool_margin_rejected(self):
        obs = build_workspace_observation(self._snap(True), now=timezone.now().timestamp(),
                                          previous_state="PROVISIONING")
        self.assertIsNone(obs.margin_mode)  # bool is not a genuine trade/margin int

    def test_none_margin_stays_none(self):
        obs = build_workspace_observation(self._snap(None), now=timezone.now().timestamp(),
                                          previous_state="PROVISIONING")
        self.assertIsNone(obs.margin_mode)
