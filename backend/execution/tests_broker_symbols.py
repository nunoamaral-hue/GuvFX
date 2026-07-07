"""GFX-PKT-BROKER-SYMBOL-REGISTRY — the broker/account-aware symbol registry.

Proves the hardcoded allowlist is replaced by a broker-aware, fail-closed registry: existing
symbols still accepted (default baseline when no broker cache); a symbol is accepted iff the
account's broker offers it; broker suffixes map when unambiguous and fail closed when ambiguous;
the registry fails closed when unavailable; the provider symbol is preserved and the resolved
BROKER symbol is what the order is placed under; and nothing here places an order.
"""
import ast
import importlib
import os
import pathlib
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from execution import broker_symbols as reg
from execution.broker_symbols import can_account_trade_symbol
from execution.models import (
    BrokerInstrument,
    ExecutionControl,
    ExecutionJob,
    ProposedOrderLeg,
    SignalExecutionPlan,
    SignalSourceConfig,
)
from execution.signal_promotion import promote_plan_to_demo_jobs, promote_plan_to_shadow_jobs
from signal_intake.models import PendingSignalApproval
from trading.models import TradingAccount

User = get_user_model()


class RegistryBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="op", email="op@x.invalid", password="x")
        self.acct = TradingAccount.objects.create(
            user=self.user, name="Demo", account_number="D1", is_demo=True,
        )

    def _instrument(self, broker_symbol, base=None, enabled=True, **meta):
        return BrokerInstrument.objects.create(
            account=self.acct, broker_symbol=broker_symbol,
            base_symbol=base if base is not None else reg.base_symbol(broker_symbol),
            enabled=enabled, metadata=meta or {},
        )


class RegistryResolutionTests(RegistryBase):
    def test_1_eurusd_accepted_default_baseline(self):
        r = can_account_trade_symbol(self.acct, "EURUSD")
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_symbol, "EURUSD")
        self.assertEqual(r.reason, reg.REASON_DEFAULT)

    def test_2_xauusd_accepted_default_baseline(self):
        r = can_account_trade_symbol(self.acct, "XAUUSD")
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_symbol, "XAUUSD")

    def test_3_btcusd_accepted_when_broker_supports(self):
        self._instrument("BTCUSD", digits=2)
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_symbol, "BTCUSD")
        self.assertEqual(r.reason, reg.REASON_EXACT)
        self.assertEqual(r.metadata.get("digits"), 2)

    def test_4_btcusd_rejected_when_broker_lacks_it(self):
        # A synced cache is authoritative — no default-baseline fallback once instruments exist.
        self._instrument("EURUSD")
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, reg.SYMBOL_NOT_AVAILABLE_ON_BROKER)
        # ...and EURUSD is still accepted from the cache (exact).
        self.assertTrue(can_account_trade_symbol(self.acct, "EURUSD").accepted)

    def test_5_suffix_mapping_unambiguous(self):
        self._instrument("BTCUSD.")   # base -> BTCUSD
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_symbol, "BTCUSD.")
        self.assertEqual(r.reason, reg.REASON_MAPPED)

    def test_6_ambiguous_mapping_fails_closed(self):
        self._instrument("BTCUSD.")   # base BTCUSD
        self._instrument("BTCUSD+")   # base BTCUSD
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, reg.SYMBOL_MAPPING_AMBIGUOUS)

    @mock.patch.dict(os.environ, {"BROKER_SYMBOL_REGISTRY_STRICT": "true"})
    def test_7_registry_unavailable_fails_closed(self):
        # Strict mode + no synced cache -> no default fallback -> unavailable (even EURUSD).
        r = can_account_trade_symbol(self.acct, "EURUSD")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, reg.SYMBOL_REGISTRY_UNAVAILABLE)

    def test_8_provider_symbol_preserved_and_exact_wins(self):
        # exact match takes priority over a base-suffix candidate (not ambiguous).
        self._instrument("BTCUSD")
        self._instrument("BTCUSD.")
        r = can_account_trade_symbol(self.acct, "btcusd")   # normalisation
        self.assertTrue(r.accepted)
        self.assertEqual(r.provider_symbol, "BTCUSD")       # preserved (normalised)
        self.assertEqual(r.broker_symbol, "BTCUSD")         # exact wins over BTCUSD.

    def test_disabled_instrument_ignored(self):
        self._instrument("BTCUSD", enabled=False)
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertFalse(r.accepted)   # disabled -> treated as no cache -> baseline -> not in it
        self.assertEqual(r.reason, reg.SYMBOL_NOT_AVAILABLE_ON_BROKER)

    def test_blank_symbol_invalid(self):
        self.assertEqual(can_account_trade_symbol(self.acct, "").reason, reg.INVALID_SYMBOL)


class PromotionSymbolTests(RegistryBase):
    """Tests 9 + 11 + 12: broker_symbol used for the order payload; provider symbol preserved;
    existing EURUSD shadow/demo behaviour unchanged; BTCUSD no longer blocked by a Python list."""

    def setUp(self):
        super().setUp()
        SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True, total_lot_target=Decimal("0.03"),
        )

    def _mode(self, mode):
        c = ExecutionControl.get_solo()
        c.signal_execution_mode = mode
        c.auto_execution_enabled = True
        c.kill_switch_engaged = False
        c.save()

    def _plan(self, symbol, mid="p1"):
        approval = PendingSignalApproval.objects.create(
            source="wayond", message_id=mid, symbol=symbol, direction="BUY",
            stop_loss="1.0800", take_profit="1.0900", take_profits=["1.0900"],
            status=PendingSignalApproval.Status.APPROVED,
        )
        plan = SignalExecutionPlan.objects.create(
            approval=approval, account=self.acct, source="wayond", message_id=mid,
            symbol=symbol, direction="BUY", stop_loss="1.0800", is_demo=True,
            signal_timestamp=timezone.now(), status=SignalExecutionPlan.Status.PLANNED,
        )
        ProposedOrderLeg.objects.create(
            plan=plan, leg_index=1, take_profit="1.0900", stop_loss="1.0800",
            lot_size=Decimal("0.01"), status=ProposedOrderLeg.Status.PLANNED,
        )
        return plan

    def test_9_broker_symbol_used_for_order_payload_provider_preserved(self):
        self._mode(ExecutionControl.SignalExecutionMode.DEMO)
        self._instrument("BTCUSD.")   # broker offers the suffixed variant
        jobs = promote_plan_to_demo_jobs(self._plan("BTCUSD"), actor=self.user)
        self.assertTrue(jobs)
        j = jobs[0]
        self.assertEqual(j.job_type, ExecutionJob.JobType.PLACE_ORDER)
        self.assertEqual(j.payload["symbol"], "BTCUSD.")           # BROKER symbol placed
        self.assertEqual(j.payload["provider_symbol"], "BTCUSD")   # provider symbol preserved

    def test_11_existing_eurusd_demo_unchanged(self):
        self._mode(ExecutionControl.SignalExecutionMode.DEMO)   # no broker cache -> default baseline
        jobs = promote_plan_to_demo_jobs(self._plan("EURUSD"), actor=self.user)
        self.assertEqual(jobs[0].payload["symbol"], "EURUSD")
        self.assertEqual(jobs[0].payload["provider_symbol"], "EURUSD")

    def test_11b_existing_eurusd_shadow_unchanged(self):
        self._mode(ExecutionControl.SignalExecutionMode.SHADOW)
        jobs = promote_plan_to_shadow_jobs(self._plan("EURUSD", mid="s1"), actor=self.user)
        self.assertTrue(all(j.job_type == ExecutionJob.JobType.PLACE_ORDER_SHADOW for j in jobs))
        self.assertEqual(jobs[0].payload["symbol"], "EURUSD")

    def test_12_btcusd_no_longer_blocked_by_python_allowlist(self):
        # The whole point: BTCUSD trades when the broker supports it (was impossible with the list).
        self._mode(ExecutionControl.SignalExecutionMode.DEMO)
        self._instrument("BTCUSD")
        jobs = promote_plan_to_demo_jobs(self._plan("BTCUSD"), actor=self.user)
        self.assertTrue(jobs)
        self.assertEqual(jobs[0].payload["symbol"], "BTCUSD")


class SyncUpsertTests(RegistryBase):
    """The populate path (upsert from a broker symbol list) — pure DB, no live bridge."""

    def test_upsert_creates_enabled_and_disables_stale(self):
        from execution.management.commands.sync_broker_instruments import upsert_broker_instruments
        c1 = upsert_broker_instruments(self.acct, [
            {"name": "BTCUSD.", "visible": True, "trade_mode": 4, "digits": 2},
            {"name": "XAUUSD", "visible": True, "digits": 2},
            {"name": "EURGBP", "visible": False},        # not visible -> disabled
        ])
        self.assertEqual(c1["created"], 3)
        # BTCUSD. is enabled and maps BTCUSD -> resolvable now.
        r = can_account_trade_symbol(self.acct, "BTCUSD")
        self.assertTrue(r.accepted)
        self.assertEqual(r.broker_symbol, "BTCUSD.")
        self.assertFalse(can_account_trade_symbol(self.acct, "EURGBP").accepted)  # disabled
        # Re-sync WITHOUT BTCUSD. -> it is disabled (kept for audit, not deleted).
        c2 = upsert_broker_instruments(self.acct, [{"name": "XAUUSD", "visible": True}])
        self.assertGreaterEqual(c2["disabled"], 1)
        self.assertTrue(BrokerInstrument.objects.filter(account=self.acct, broker_symbol="BTCUSD.").exists())
        self.assertFalse(can_account_trade_symbol(self.acct, "BTCUSD").accepted)


class BoundaryTests(TestCase):
    def test_10_registry_makes_no_order_or_network_call(self):
        src = pathlib.Path(
            importlib.import_module("execution.broker_symbols").__file__
        ).read_text()
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        for forbidden in ("order_send", "order_check", "MetaTrader5", "mt5", "requests",
                          "httpx", "urllib", "socket", "symbols_get", "order_send"):
            self.assertNotIn(forbidden, names, f"broker_symbols references {forbidden}")
