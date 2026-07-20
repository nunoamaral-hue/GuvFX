"""GFX-BETA-PHASE0 Increment 1 — per-(account+assignment) lot-size override: validation, versioning,
immutable audit history, account-owner scoping, source-global fallback, truthful (not-live) payload."""
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIRequestFactory, force_authenticate

from trading.models import TradingAccount
from strategies.models import (Strategy, StrategyAssignment, AssignmentLegSizing,
                               AssignmentLegSizingHistory, effective_lot_per_leg)
from strategies.views_sizing import AssignmentLegSizingView, AssignmentLegSizingHistoryView
from execution.models import SignalSourceConfig

U = get_user_model()


class LegSizingTests(TestCase):
    def setUp(self):
        self.owner = U.objects.create_user(username="o", email="o@x.invalid", password="x")
        self.other = U.objects.create_user(username="p", email="p@x.invalid", password="x")
        self.staff = U.objects.create_user(username="s", email="s@x.invalid", password="x", is_staff=True)
        self.acct = TradingAccount.objects.create(
            user=self.owner, name="A", account_number="A1", is_demo=True)
        self.strat = Strategy.objects.create(owner=self.owner, name="WIM")
        self.asn = StrategyAssignment.objects.create(
            strategy=self.strat, account=self.acct, signal_source="wayond")
        SignalSourceConfig.objects.create(
            source="wayond", auto_demo_execution_enabled=True, max_lot_per_leg=Decimal("0.02"))
        self.factory = APIRequestFactory()

    def _get(self, user, pk=None):
        req = self.factory.get("/x")
        force_authenticate(req, user=user)
        return AssignmentLegSizingView.as_view()(req, pk=pk or self.asn.id)

    def _put(self, user, value, pk=None):
        req = self.factory.put("/x", {"lot_per_leg": value}, format="json")
        force_authenticate(req, user=user)
        return AssignmentLegSizingView.as_view()(req, pk=pk or self.asn.id)

    # --- fallback + default ---
    def test_no_override_falls_back_to_source_global(self):
        r = self._get(self.owner)
        self.assertEqual(r.data["lot_per_leg"], "0.02")  # source-global cap
        self.assertFalse(r.data["is_override"])
        self.assertEqual(r.data["default_lot_per_leg"], "0.01")
        self.assertFalse(r.data["applies_to_live_execution"])  # truthful: NOT live

    def test_effective_resolver(self):
        self.assertEqual(effective_lot_per_leg(self.asn), Decimal("0.02"))  # fallback
        AssignmentLegSizing.objects.create(assignment=self.asn, lot_per_leg=Decimal("0.01"))
        self.asn.refresh_from_db()
        self.assertEqual(effective_lot_per_leg(self.asn), Decimal("0.01"))  # override wins

    # --- set + versioning + history ---
    def test_put_creates_override_v1_and_history(self):
        r = self._put(self.owner, "0.03")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["is_override"])
        self.assertEqual(r.data["lot_per_leg"], "0.03")
        self.assertEqual(r.data["version"], 1)
        self.assertEqual(AssignmentLegSizingHistory.objects.filter(assignment=self.asn).count(), 1)

    def test_put_bumps_version_and_appends_history(self):
        self._put(self.owner, "0.03")
        r = self._put(self.owner, "0.05")
        self.assertEqual(r.data["version"], 2)
        h = list(AssignmentLegSizingHistory.objects.filter(assignment=self.asn).order_by("version"))
        self.assertEqual([(x.version, str(x.lot_per_leg)) for x in h], [(1, "0.03"), (2, "0.05")])

    def test_put_same_value_is_noop(self):
        self._put(self.owner, "0.03")
        r = self._put(self.owner, "0.03")
        self.assertEqual(r.data["version"], 1)  # unchanged
        self.assertEqual(AssignmentLegSizingHistory.objects.filter(assignment=self.asn).count(), 1)

    # --- validation (broker min/max/step) ---
    def test_below_min_rejected(self):
        self.assertEqual(self._put(self.owner, "0.00").status_code, 400)

    def test_above_max_rejected(self):
        self.assertEqual(self._put(self.owner, "200").status_code, 400)

    def test_non_step_rejected(self):
        self.assertEqual(self._put(self.owner, "0.015").status_code, 400)

    # --- ownership scoping (account owner, NOT strategy owner) ---
    def test_other_user_cannot_read(self):
        self.assertEqual(self._get(self.other).status_code, 404)

    def test_other_user_cannot_write(self):
        self.assertEqual(self._put(self.other, "0.03").status_code, 404)
        self.assertFalse(AssignmentLegSizing.objects.filter(assignment=self.asn).exists())

    def test_staff_can_read(self):
        self.assertEqual(self._get(self.staff).status_code, 200)

    # --- history endpoint ---
    def test_history_endpoint_newest_first(self):
        self._put(self.owner, "0.03")
        self._put(self.owner, "0.05")
        req = self.factory.get("/x")
        force_authenticate(req, user=self.owner)
        r = AssignmentLegSizingHistoryView.as_view()(req, pk=self.asn.id)
        self.assertEqual([h["version"] for h in r.data["history"]], [2, 1])

    def test_global_source_config_untouched(self):
        # Editing the override must NEVER mutate the shared operator sizing row.
        self._put(self.owner, "0.03")
        self.assertEqual(SignalSourceConfig.objects.get(source="wayond").max_lot_per_leg, Decimal("0.02"))

    # --- review fixes: JSON-number body, garbage input, direct-ORM guard ---
    def test_json_number_body_accepted(self):
        # A native JSON number (not a string) must be accepted, not drift-rejected.
        r = self._put(self.owner, 0.03)  # float, not "0.03"
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["lot_per_leg"], "0.03")

    def test_garbage_body_is_400_not_500(self):
        for bad in ("abc", "NaN", "Infinity", ""):
            self.assertEqual(self._put(self.owner, bad).status_code, 400, f"{bad!r} should 400")

    def test_direct_orm_out_of_range_rejected(self):
        # 500 is within the DB field width (NUMERIC(6,2)) but above LOT_MAX (100) — save() must reject.
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            AssignmentLegSizing(assignment=self.asn, lot_per_leg=Decimal("500.00")).save()

    def test_history_masks_staff_identity_from_tenant(self):
        # Staff edits the tenant's sizing; the tenant's history must not reveal the staff email.
        self._put(self.staff, "0.03")
        req = self.factory.get("/x")
        force_authenticate(req, user=self.owner)
        r = AssignmentLegSizingHistoryView.as_view()(req, pk=self.asn.id)
        self.assertEqual(r.data["history"][0]["changed_by"], "operator")  # masked, not s@x.invalid
