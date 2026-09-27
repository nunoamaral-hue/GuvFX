"""WAYOND marketplace availability vs ownership + assignment-scoped sizing.

Sponsor decisions: a PUBLISHED (is_marketplace) strategy is ASSIGNABLE by any eligible customer to their
own account without owning it; a PRIVATE strategy is assignable only by its owner; TradingAccount ownership
is never relaxed; per-assignment sizing (AssignmentLegSizing.lot_per_leg) is isolated per assignment; a
normal customer can never edit a published strategy definition.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.test import TestCase

from billing.models import UserSubscriptionState
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
ASSIGN = "/api/strategies/assignments/"
ASSIGNABLE = "/api/strategies/strategies/assignable/"


def _user(name):
    u = User.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")
    UserSubscriptionState.objects.create(
        user=u, current_plan=UserSubscriptionState.Plan.PRO,
        plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _acct(user, number):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, is_demo=True, is_active=True, broker_name="DemoBroker")


class _Base(TestCase):
    def setUp(self):
        self.provider = _user("provider")   # publishes the marketplace strategy
        self.userA = _user("alice")
        self.userB = _user("bob")
        # PUBLISHED marketplace strategy M (owned by provider, is_marketplace=True)
        self.M = Strategy.objects.create(owner=self.provider, name="Wayond WIM (Marketplace)", is_marketplace=True)
        # PRIVATE strategy owned by A
        self.privA = Strategy.objects.create(owner=self.userA, name="Alice Private")
        self.a1 = _acct(self.userA, "A1")
        self.a2 = _acct(self.userA, "A2")
        self.b3 = _acct(self.userB, "B3")

    def _client(self, u):
        c = APIClient(); c.force_authenticate(u); return c


# ─────────────────────────── availability (assignable endpoint) ───────────────────────────
class AvailabilityTests(_Base):
    def test_published_strategy_visible_to_all_but_private_only_to_owner(self):
        a_ids = {s["id"] for s in self._client(self.userA).get(ASSIGNABLE).data}
        b_ids = {s["id"] for s in self._client(self.userB).get(ASSIGNABLE).data}
        # A sees M (published) + own private; B sees M but NOT A's private.
        self.assertIn(self.M.id, a_ids); self.assertIn(self.privA.id, a_ids)
        self.assertIn(self.M.id, b_ids); self.assertNotIn(self.privA.id, b_ids)

    def test_assignable_marks_publication(self):
        rows = {s["id"]: s for s in self._client(self.userB).get(ASSIGNABLE).data}
        self.assertTrue(rows[self.M.id]["is_marketplace"])  # exposed read-only

    def test_assignable_projection_does_not_leak_definition(self):
        # A published strategy the caller does NOT own must expose only its catalogue identity, never its
        # proprietary definition (entry/exit rules, indicators, etc.).
        row = next(s for s in self._client(self.userB).get(ASSIGNABLE).data if s["id"] == self.M.id)
        self.assertEqual(set(row.keys()), {"id", "name", "is_marketplace"})
        for leaked in ("entry_rules", "tp_rules", "sl_rules", "entry_logic", "exit_logic",
                       "indicator_blocks", "filters", "magic_number"):
            self.assertNotIn(leaked, row)

    def test_owner_scoped_list_still_private(self):
        # The plain list (used for CRUD) stays owner-scoped — B cannot see M or privA there.
        ids = {s["id"] for s in self._client(self.userB).get("/api/strategies/strategies/").data}
        self.assertNotIn(self.M.id, ids)
        self.assertNotIn(self.privA.id, ids)


# ─────────────────────────── assignability rule ───────────────────────────
class AssignabilityTests(_Base):
    def test_own_account_published_strategy_allowed(self):
        r = self._client(self.userA).post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_own_account_foreign_private_denied(self):
        r = self._client(self.userB).post(ASSIGN, {"account": self.b3.id, "strategy": self.privA.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_foreign_account_published_denied(self):
        # even a published strategy cannot be assigned onto an account you don't own
        r = self._client(self.userB).post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_multi_user_multi_account_independent_assignments(self):
        # One published M → many users/accounts → independent assignments.
        rA1 = self._client(self.userA).post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json")
        rA2 = self._client(self.userA).post(ASSIGN, {"account": self.a2.id, "strategy": self.M.id}, format="json")
        rB3 = self._client(self.userB).post(ASSIGN, {"account": self.b3.id, "strategy": self.M.id}, format="json")
        self.assertEqual((rA1.status_code, rA2.status_code, rB3.status_code), (201, 201, 201))
        ids = {rA1.data["id"], rA2.data["id"], rB3.data["id"]}
        self.assertEqual(len(ids), 3)  # three distinct assignments, all on the SAME strategy M
        for aid in ids:
            self.assertEqual(StrategyAssignment.objects.get(id=aid).strategy_id, self.M.id)

    def test_duplicate_assignment_rejected_gracefully(self):
        c = self._client(self.userA)
        self.assertEqual(c.post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json").status_code, 201)
        dup = c.post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json")
        self.assertEqual(dup.status_code, 400)  # unique (strategy, account) → 400, never 500

    def test_assignment_of_published_is_visible_to_assignee(self):
        # get_queryset scopes by account owner → B can see/manage their M-assignment even though B doesn't own M.
        c = self._client(self.userB)
        aid = c.post(ASSIGN, {"account": self.b3.id, "strategy": self.M.id}, format="json").data["id"]
        got = {row["id"] for row in c.get(f"{ASSIGN}?account={self.b3.id}").data}
        self.assertIn(aid, got)


# ─────────────────────────── Phase 10 — published definition protected ───────────────────────────
class PublishedEditProtectionTests(_Base):
    def test_customer_cannot_edit_published_strategy_definition(self):
        # B assigns M, then tries to edit M's global definition → 404 (get_queryset owner-scoped for writes).
        c = self._client(self.userB)
        c.post(ASSIGN, {"account": self.b3.id, "strategy": self.M.id}, format="json")
        r = c.patch(f"/api/strategies/strategies/{self.M.id}/", {"name": "hacked"}, format="json")
        self.assertEqual(r.status_code, 404, r.content)
        self.M.refresh_from_db(); self.assertEqual(self.M.name, "Wayond WIM (Marketplace)")

    def test_customer_cannot_self_publish(self):
        # is_marketplace is read-only — a member PATCHing their own private strategy cannot publish it.
        c = self._client(self.userA)
        c.patch(f"/api/strategies/strategies/{self.privA.id}/", {"is_marketplace": True}, format="json")
        self.privA.refresh_from_db()
        self.assertFalse(self.privA.is_marketplace)


# ─────────────────────────── per-assignment sizing isolation ───────────────────────────
class SizingIsolationTests(_Base):
    def _leg(self, acc):
        return f"/api/strategies/assignments/{acc}/leg-sizing/"

    def test_changing_one_assignment_sizing_does_not_affect_siblings_or_others(self):
        cA = self._client(self.userA); cB = self._client(self.userB)
        a1 = cA.post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json").data["id"]
        a2 = cA.post(ASSIGN, {"account": self.a2.id, "strategy": self.M.id}, format="json").data["id"]
        b3 = cB.post(ASSIGN, {"account": self.b3.id, "strategy": self.M.id}, format="json").data["id"]
        # all start at the canonical 0.01
        self.assertEqual(cA.get(self._leg(a1)).data["lot_per_leg"], "0.01")
        # change A2 → 0.02
        put = cA.put(self._leg(a2), {"lot_per_leg": "0.02"}, format="json")
        self.assertEqual(put.status_code, 200, put.content)
        self.assertEqual(cA.get(self._leg(a2)).data["lot_per_leg"], "0.02")
        # A1 and B3 unchanged (isolation)
        self.assertEqual(cA.get(self._leg(a1)).data["lot_per_leg"], "0.01")
        self.assertEqual(cB.get(self._leg(b3)).data["lot_per_leg"], "0.01")

    def test_leg_sizing_is_account_owner_scoped(self):
        cA = self._client(self.userA)
        a1 = cA.post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json").data["id"]
        # B cannot read/write A's assignment sizing
        self.assertEqual(self._client(self.userB).get(self._leg(a1)).status_code, 404)
