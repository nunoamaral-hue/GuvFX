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
        # Only catalogue identity — id/name/is_marketplace + the family slug (a stable family id, NOT
        # proprietary logic). The full filters object and all definition fields must NOT appear.
        self.assertEqual(set(row.keys()), {"id", "name", "is_marketplace", "family"})
        for leaked in ("entry_rules", "tp_rules", "sl_rules", "entry_logic", "exit_logic",
                       "indicator_blocks", "filters", "magic_number", "signal_source", "parser_profile"):
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


# ─────────────────────────── member flow: Add Strategy to a STOPPED account ───────────────────────────
class InactiveAccountAddStrategyTests(_Base):
    """Target member lifecycle: a member configures strategies BEFORE Start Trading. Adding a strategy must
    succeed on an INACTIVE (stopped) account and remain zero-dispatch (the account-level + stage gates are
    the authoritative execution kills, not an activation guard)."""
    def _inactive(self, user, num):
        return TradingAccount.objects.create(
            user=user, name=num, account_number=num, is_demo=True, is_active=False, broker_name="DemoBroker")

    def test_add_strategy_to_inactive_account_succeeds(self):
        acc = self._inactive(self.userA, "INACT1")
        r = self._client(self.userA).post(ASSIGN, {"account": acc.id, "strategy": self.privA.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)   # no longer "Cannot activate ... inactive account"
        acc.refresh_from_db()
        self.assertFalse(acc.is_active)                   # account stays STOPPED (Add Strategy never starts it)

    def test_added_assignment_is_non_routable_zero_dispatch(self):
        acc = self._inactive(self.userA, "INACT2")
        aid = self._client(self.userA).post(ASSIGN, {"account": acc.id, "strategy": self.privA.id}, format="json").data["id"]
        asn = StrategyAssignment.objects.get(id=aid)
        # fresh member assignment is stage TEST + execution_mode MANUAL + on an inactive account → the fan-out
        # (requires stage LIVE + account.is_active) and the signal engine (account_not_active) both exclude it.
        self.assertEqual(asn.stage, StrategyAssignment.STAGE_TEST)
        self.assertEqual(asn.execution_mode, StrategyAssignment.ExecutionMode.MANUAL)

    def test_multiple_strategies_on_inactive_account(self):
        acc = self._inactive(self.userA, "INACT3")
        s2 = Strategy.objects.create(owner=self.userA, name="Alice Private 2")
        c = self._client(self.userA)
        self.assertEqual(c.post(ASSIGN, {"account": acc.id, "strategy": self.privA.id}, format="json").status_code, 201)
        self.assertEqual(c.post(ASSIGN, {"account": acc.id, "strategy": s2.id}, format="json").status_code, 201)
        self.assertEqual(StrategyAssignment.objects.filter(account=acc).count(), 2)

    def test_manual_evaluate_refuses_inactive_account_zero_job(self):
        # Closes the dispatch-on-inactive hole the adversarial review found: the per-template engines (e.g.
        # TC1) create PLACE_ORDER jobs directly and don't each re-check is_active, and the manual evaluate path
        # has no stage/active filter. run_signal_evaluation now guards it at the single chokepoint.
        from strategies.signal_engine import run_signal_evaluation
        from execution.models import ExecutionJob
        acc = self._inactive(self.userA, "INACT4")
        strat = Strategy.objects.create(owner=self.userA, name="TC1", filters={"template_slug": "tc1-engine-v1"})
        StrategyAssignment.objects.create(
            strategy=strat, account=acc, is_active=True, stage=StrategyAssignment.STAGE_LIVE)
        before = ExecutionJob.objects.filter(account=acc).count()
        res = run_signal_evaluation(None, strat, acc, "XAUUSD", self.userA)
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "account_not_active")
        self.assertEqual(ExecutionJob.objects.filter(account=acc).count(), before)  # zero dispatch


# ─────────────────────────── legacy-transition family dedup (canonical vs legacy) ───────────────────────────
class FamilyDedupTests(_Base):
    def setUp(self):
        super().setUp()
        # A canonical PUBLISHED strategy and a LEGACY per-user copy that share the SAME family (template_slug).
        self.canonical = Strategy.objects.create(
            owner=self.provider, name="Wayond WIM Strategy", is_marketplace=True,
            filters={"template_slug": "wayond-wim", "signal_source": "ti_signals"})
        self.legacy = Strategy.objects.create(
            owner=self.userA, name="Wayond WIM Strategy", filters={"template_slug": "wayond-wim"})

    def test_cannot_assign_same_family_twice_on_one_account(self):
        c = self._client(self.userA)
        self.assertEqual(c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json").status_code, 201)
        # canonical shares the family → assigning it to the SAME account is a graceful 400 (already assigned)
        dup = c.post(ASSIGN, {"account": self.a1.id, "strategy": self.canonical.id}, format="json")
        self.assertEqual(dup.status_code, 400, dup.content)

    def test_same_family_allowed_on_a_different_account(self):
        c = self._client(self.userA)
        c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json")
        # a different account with no WIM family → canonical is assignable
        r = c.post(ASSIGN, {"account": self.a2.id, "strategy": self.canonical.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_family_less_strategy_not_restricted(self):
        c = self._client(self.userA)
        c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json")
        # M has no template_slug → no family restriction → coexists with the WIM family on the same account
        r = c.post(ASSIGN, {"account": self.a1.id, "strategy": self.M.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_assignable_exposes_family(self):
        rows = {s["id"]: s for s in self._client(self.userA).get(ASSIGNABLE).data}
        self.assertEqual(rows[self.canonical.id]["family"], "wayond-wim")
        self.assertIsNone(rows[self.M.id]["family"])   # no template_slug

    def test_assignable_dedups_family_to_canonical(self):
        # userA owns the LEGACY wayond-wim copy AND the published canonical exists (same family). The picker
        # must expose exactly ONE wayond-wim option = the canonical (published wins over owned legacy).
        ids = [s["id"] for s in self._client(self.userA).get(ASSIGNABLE).data]
        wim = [s for s in self._client(self.userA).get(ASSIGNABLE).data if s["family"] == "wayond-wim"]
        self.assertEqual(len(wim), 1, "exactly one wayond-wim option")
        self.assertEqual(wim[0]["id"], self.canonical.id)      # canonical wins
        self.assertNotIn(self.legacy.id, ids)                  # legacy copy hidden
        # non-family strategies are NOT collapsed
        self.assertIn(self.M.id, ids)                          # published, no family
        self.assertIn(self.privA.id, ids)                      # owned private, no family

    def test_assignable_keeps_distinct_families_with_same_name(self):
        # Two DIFFERENT families that happen to share a display name must NOT be collapsed (dedup is by
        # template_slug, never by name).
        f1 = Strategy.objects.create(owner=self.userA, name="Same Name", filters={"template_slug": "fam-a"})
        f2 = Strategy.objects.create(owner=self.userA, name="Same Name", filters={"template_slug": "fam-b"})
        ids = [s["id"] for s in self._client(self.userA).get(ASSIGNABLE).data]
        self.assertIn(f1.id, ids)
        self.assertIn(f2.id, ids)

    def test_assignment_serializer_exposes_family(self):
        c = self._client(self.userA)
        aid = c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json").data["id"]
        row = next(r for r in c.get(f"{ASSIGN}?account={self.a1.id}").data if r["id"] == aid)
        self.assertEqual(row["strategy_family"], "wayond-wim")

    def test_patch_repoint_to_same_family_blocked(self):
        # The update path must enforce the same guard — a PATCH repointing onto a same-family strategy
        # would otherwise create a second active family assignment (double-run) bypassing the create guard.
        c = self._client(self.userA)
        c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json")  # legacy WIM active
        f = Strategy.objects.create(owner=self.userA, name="Plain")  # family-less
        n = c.post(ASSIGN, {"account": self.a1.id, "strategy": f.id}, format="json").data["id"]
        r = c.patch(f"{ASSIGN}{n}/", {"strategy": self.canonical.id}, format="json")  # repoint → same family
        self.assertEqual(r.status_code, 400, r.content)

    def test_patch_reactivate_into_same_family_blocked(self):
        c = self._client(self.userA)
        c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json")  # legacy active
        inactive = StrategyAssignment.objects.create(account=self.a1, strategy=self.canonical, is_active=False)
        r = c.patch(f"{ASSIGN}{inactive.id}/", {"is_active": True}, format="json")  # reactivate same family
        self.assertEqual(r.status_code, 400, r.content)

    def test_patch_deactivate_never_blocked_by_family(self):
        c = self._client(self.userA)
        aid = c.post(ASSIGN, {"account": self.a1.id, "strategy": self.legacy.id}, format="json").data["id"]
        r = c.patch(f"{ASSIGN}{aid}/", {"is_active": False}, format="json")  # stopping is always allowed
        self.assertEqual(r.status_code, 200, r.content)
