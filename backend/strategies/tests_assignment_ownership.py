"""WAYOND account-scoped strategy management — StrategyAssignment BOTH-AXIS ownership + canonical init.

Sponsor decision: a non-staff user may only create / repoint / remove a StrategyAssignment when they
own BOTH the TradingAccount AND the Strategy. Historically the REST create guarded only the account
axis and update/delete guarded only the strategy axis (via get_queryset) — asymmetric. These tests are
the adversarial IDOR matrix for the fix (strategies/assignment_service.assert_assignment_ownership wired
into StrategyAssignmentViewSet.perform_create/perform_update/perform_destroy) plus the canonical
initializer (sizing + deterministic magic) that makes an account-page create as complete as a
marketplace one.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.test import TestCase

from billing.models import UserSubscriptionState
from strategies.magic_allocation import ASSIGNMENT_MAGIC_BASE
from strategies.models import AssignmentLegSizing, Strategy, StrategyAssignment
from trading.models import TradingAccount

User = get_user_model()
URL = "/api/strategies/assignments/"


def _user(name, *, staff=False, superuser=False):
    u = User.objects.create_user(
        username=name, email=f"{name}@x.invalid", password="x",
        is_staff=staff or superuser, is_superuser=superuser)
    UserSubscriptionState.objects.create(
        user=u, current_plan=UserSubscriptionState.Plan.PRO,
        plan_status=UserSubscriptionState.PlanStatus.ACTIVE, viewer_mode=False)
    return u


def _acct(user, number):
    return TradingAccount.objects.create(
        user=user, name=number, account_number=number, is_demo=True, is_active=True,
        broker_name="DemoBroker")


def _strat(user, name):
    return Strategy.objects.create(owner=user, name=name)


class _Base(TestCase):
    def setUp(self):
        self.owner = _user("owner")
        self.attacker = _user("attacker")
        self.acctO = _acct(self.owner, "OWN1")
        self.acctA = _acct(self.attacker, "ATK1")
        self.stratO = _strat(self.owner, "Owner Strat")
        self.stratA = _strat(self.attacker, "Attacker Strat")
        self.client = APIClient()
        self.client.force_authenticate(self.owner)


# ─────────────────────────── CREATE — both-axis ownership ───────────────────────────
class CreateOwnershipTests(_Base):
    def test_owned_account_owned_strategy_creates_and_is_complete(self):
        r = self.client.post(URL, {"account": self.acctO.id, "strategy": self.stratO.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        asn = StrategyAssignment.objects.get(id=r.data["id"])
        self.assertEqual((asn.account_id, asn.strategy_id), (self.acctO.id, self.stratO.id))
        # Canonical init: conservative sizing 0.01 + deterministic magic 1e9+id (inert until armed).
        sizing = AssignmentLegSizing.objects.get(assignment=asn)
        self.assertEqual(sizing.lot_per_leg, Decimal("0.01"))
        self.assertEqual(asn.magic_number, ASSIGNMENT_MAGIC_BASE + asn.id)

    def test_owned_account_foreign_strategy_fails_closed(self):
        r = self.client.post(URL, {"account": self.acctO.id, "strategy": self.stratA.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(StrategyAssignment.objects.filter(account=self.acctO, strategy=self.stratA).exists())

    def test_foreign_account_owned_strategy_fails_closed(self):
        r = self.client.post(URL, {"account": self.acctA.id, "strategy": self.stratO.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(StrategyAssignment.objects.filter(account=self.acctA).exists())

    def test_foreign_account_foreign_strategy_fails_closed(self):
        r = self.client.post(URL, {"account": self.acctA.id, "strategy": self.stratA.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(StrategyAssignment.objects.filter(account=self.acctA, strategy=self.stratA).exists())

    def test_superuser_bypass_preserved(self):
        root = _user("root", superuser=True)
        c = APIClient(); c.force_authenticate(root)
        # a superuser may create across users (bypass matches get_queryset's is_superuser read scope)
        r = c.post(URL, {"account": self.acctO.id, "strategy": self.stratO.id}, format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_staff_non_superuser_is_not_a_bypass(self):
        # A staff-but-not-superuser account is scoped like a member on reads, so it must ALSO own both
        # axes on writes — it may NOT create an assignment on another user's account (the write-widening
        # asymmetry the adversarial review flagged).
        staff = _user("agent", staff=True)  # is_staff=True, is_superuser=False
        c = APIClient(); c.force_authenticate(staff)
        r = c.post(URL, {"account": self.acctO.id, "strategy": self.stratO.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_staff_non_superuser_cannot_repoint_to_foreign_account(self):
        # staff-non-superuser OWNS a strategy + an assignment on their OWN account; get_queryset admits it
        # (they own the strategy). A PATCH repointing the account FK to a foreign account must be 403.
        staff = _user("agent2", staff=True)
        staff_acct = _acct(staff, "AGT")
        staff_strat = _strat(staff, "Agent Strat")
        asn = StrategyAssignment.objects.create(account=staff_acct, strategy=staff_strat)
        c = APIClient(); c.force_authenticate(staff)
        r = c.patch(f"{URL}{asn.id}/", {"account": self.acctO.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        asn.refresh_from_db()
        self.assertEqual(asn.account_id, staff_acct.id)  # unchanged


# ─────────────────────────── UPDATE — repoint IDOR closed ───────────────────────────
class UpdateOwnershipTests(_Base):
    def setUp(self):
        super().setUp()
        self.asn = StrategyAssignment.objects.create(account=self.acctO, strategy=self.stratO)

    def test_patch_account_to_foreign_account_fails_closed(self):
        r = self.client.patch(f"{URL}{self.asn.id}/", {"account": self.acctA.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.asn.refresh_from_db()
        self.assertEqual(self.asn.account_id, self.acctO.id)  # unchanged

    def test_patch_strategy_to_foreign_strategy_fails_closed(self):
        r = self.client.patch(f"{URL}{self.asn.id}/", {"strategy": self.stratA.id}, format="json")
        self.assertEqual(r.status_code, 403, r.content)
        self.asn.refresh_from_db()
        self.assertEqual(self.asn.strategy_id, self.stratO.id)  # unchanged

    def test_patch_same_owned_fields_ok(self):
        r = self.client.patch(f"{URL}{self.asn.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.asn.refresh_from_db()
        self.assertFalse(self.asn.is_active)


# ─────────────────────────── cross-user access / forged ids ───────────────────────────
class CrossUserAccessTests(_Base):
    def setUp(self):
        super().setUp()
        # an assignment that belongs to the ATTACKER
        self.victim_asn = StrategyAssignment.objects.create(account=self.acctA, strategy=self.stratA)

    def test_cross_user_retrieve_is_404(self):
        r = self.client.get(f"{URL}{self.victim_asn.id}/")
        self.assertEqual(r.status_code, 404, r.content)  # get_queryset scopes by strategy owner

    def test_cross_user_delete_is_404(self):
        r = self.client.delete(f"{URL}{self.victim_asn.id}/")
        self.assertEqual(r.status_code, 404, r.content)
        self.assertTrue(StrategyAssignment.objects.filter(id=self.victim_asn.id).exists())  # survives

    def test_cross_user_patch_is_404(self):
        r = self.client.patch(f"{URL}{self.victim_asn.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 404, r.content)

    def test_forged_assignment_id_is_404(self):
        r = self.client.get(f"{URL}999999/")
        self.assertEqual(r.status_code, 404)

    def test_owner_can_delete_own_assignment(self):
        mine = StrategyAssignment.objects.create(account=self.acctO, strategy=self.stratO)
        r = self.client.delete(f"{URL}{mine.id}/")
        self.assertEqual(r.status_code, 204, r.content)
        self.assertFalse(StrategyAssignment.objects.filter(id=mine.id).exists())

    def test_account_scoped_list(self):
        # only the owner's own assignments, filterable by account
        StrategyAssignment.objects.create(account=self.acctO, strategy=self.stratO)
        r = self.client.get(f"{URL}?account={self.acctO.id}")
        self.assertEqual(r.status_code, 200)
        ids = {row["account"] for row in r.data}
        self.assertEqual(ids, {self.acctO.id})


# ─────────────────────────── canonical init parity + immutability ───────────────────────────
class CanonicalInitTests(_Base):
    def test_existing_magic_never_altered_on_reassign(self):
        # A pre-existing assignment with an allocated magic keeps it (immutable) — nothing re-created.
        asn = StrategyAssignment.objects.create(account=self.acctO, strategy=self.stratO)
        from strategies.magic_allocation import allocate_magic
        allocate_magic(asn)
        original = StrategyAssignment.objects.get(id=asn.id).magic_number
        # initialize again → idempotent, magic unchanged
        from strategies.assignment_service import initialize_new_assignment
        initialize_new_assignment(asn)
        self.assertEqual(StrategyAssignment.objects.get(id=asn.id).magic_number, original)

    def test_new_member_assignment_gets_conservative_sizing_not_source_global(self):
        r = self.client.post(URL, {"account": self.acctO.id, "strategy": self.stratO.id}, format="json")
        asn = StrategyAssignment.objects.get(id=r.data["id"])
        self.assertEqual(AssignmentLegSizing.objects.get(assignment=asn).lot_per_leg, Decimal("0.01"))
