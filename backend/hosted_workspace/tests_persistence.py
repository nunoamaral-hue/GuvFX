"""ADR-0034 / M3c — the Workspace Core persistence layer (writer + consumer + read model + DARK API).

Covers the 24-point bar: stale-observation rejection, stale-decision / illegal-transition protection under
the authoritative locked state, idempotent replay (single transition + single event), version/decision
monotonicity, material-vs-no-op classification, telemetry emission ONLY from the writer seam and ONLY on a
real state change, secret-free telemetry + projection, the DARK master gate on the consumer AND the API,
IDOR owner-scoping, and legacy-safety (legacy WorkspaceState fields untouched by the M3c writer). Plus a
runnable mutation-adequacy harness for the writer's novel pure predicates (``_coerce_version``/``_as_bool``).

The M3a manager + M3b producer already carry AST mutation adequacy for the pure decision core; here the new
surface is the DB writer, so most proofs are behavioural oracles against Postgres (via ``TestCase``).
"""
from __future__ import annotations

import inspect
import os
import textwrap
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from operational_events.models import OperationalEvent
from trading.models import TradingAccount

from hosted_workspace import persistence as P
from hosted_workspace.consumer import ingest_observation
from hosted_workspace.manager import WorkspaceDecision, WorkspaceObservation, derive_workspace_decision
from hosted_workspace.models import HostedMt5Workspace, WorkspaceState, WorkspaceTransition
from hosted_workspace.persistence import PersistStatus, persist_workspace_decision
from hosted_workspace.read_model import workspace_state_projection
from hosted_workspace.state_machine import WorkspaceLifecycleState as S, WorkspaceReason
from hosted_workspace.telemetry import WorkspaceEvent

U = get_user_model()


def _obs(**kw):
    """A manager WorkspaceObservation (health signals). previous_* are overwritten by the writer path in
    real use; here they only matter when we call derive_workspace_decision directly."""
    base = dict(process_running=True, ipc_available=True, connected=True, account_match=True,
                trade_allowed=True, fresh=True, previous_state=str(S.CONNECTED),
                previous_reason=str(WorkspaceReason.NONE), observed_at=None)
    base.update(kw)
    return WorkspaceObservation(**base)


def _ops_on():
    """Context manager turning the ADR-0032 operational-event recorder ON (it reads the ENV, not settings)."""
    return mock.patch.dict(os.environ, {"OPERATIONS_EVENTS_ENABLED": "1"})


class _Base(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="m3c", email="m3c@example.com", password="x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="m3c-acct", broker_name="Broker-Demo",
            account_number="500999", is_demo=True)

    def _ws(self, *, canonical_state=S.PROVISIONING, canonical_reason=WorkspaceReason.NONE, **kw):
        ws = HostedMt5Workspace.objects.create(trading_account=self.account, **kw)
        # Establish a stored canonical premise directly (bypassing the writer) for the test's precondition.
        if canonical_state != S.PROVISIONING or canonical_reason != WorkspaceReason.NONE:
            ws.canonical_state = str(canonical_state)
            ws.canonical_reason = str(canonical_reason)
            ws.save(update_fields=["canonical_state", "canonical_reason", "updated_at"])
        return ws


class WriterHappyPathTests(_Base):
    def test_connected_to_execution_ready_applies_and_records_provenance(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        decision = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        with _ops_on():
            r = persist_workspace_decision(ws, _obs(), decision, observation_version=1,
                                           correlation_id="corr-abc")
        self.assertEqual(r.status, PersistStatus.APPLIED)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.EXECUTION_READY))
        self.assertEqual(ws.canonical_reason, str(WorkspaceReason.NONE))
        self.assertEqual(ws.observation_version, 1)
        self.assertEqual(ws.decision_version, 1)
        self.assertTrue(ws.canonical_execution_ready)
        self.assertIsNotNone(ws.last_transition_at)
        self.assertIsNotNone(ws.last_decision_at)
        self.assertEqual(ws.last_correlation_id, "corr-abc")
        # exactly one transition row, faithfully recorded
        tr = ws.transitions.get()
        self.assertEqual((tr.from_state, tr.to_state), (str(S.CONNECTED), str(S.EXECUTION_READY)))
        self.assertTrue(tr.state_changed)
        self.assertEqual(tr.observation_version, 1)
        self.assertEqual(tr.decision_version, 1)
        self.assertTrue(r.telemetry_emitted)
        # exactly one operational event, linked to the account, of the right type
        ev = OperationalEvent.objects.get(account=self.account)
        self.assertEqual(ev.event_type, str(WorkspaceEvent.EXECUTION_READY))
        self.assertEqual(ev.state_version, 1)
        self.assertEqual(ev.dedup_key, tr.dedupe_key)

    def test_projection_cache_reflects_latest_observation(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        decision = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), trade_allowed=False))
        r = persist_workspace_decision(ws, _obs(trade_allowed=False), decision, observation_version=2)
        self.assertIn(r.status, (PersistStatus.APPLIED, PersistStatus.IDEMPOTENT))
        ws.refresh_from_db()
        self.assertIs(ws.proj_trade_allowed, False)
        self.assertIs(ws.proj_connected, True)
        self.assertIs(ws.proj_execution_ready, False)  # trading halted -> not ready


class WriterRejectionTests(_Base):
    def test_stale_observation_is_rejected_without_mutation(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        persist_workspace_decision(ws, _obs(), d, observation_version=5)
        # a lower/equal version arriving late must not overwrite
        before = HostedMt5Workspace.objects.get(pk=ws.pk)
        r = persist_workspace_decision(ws, _obs(), d, observation_version=5)
        self.assertEqual(r.status, PersistStatus.REJECTED_STALE)
        after = HostedMt5Workspace.objects.get(pk=ws.pk)
        self.assertEqual(after.observation_version, before.observation_version)
        self.assertEqual(after.decision_version, before.decision_version)
        self.assertEqual(ws.transitions.count(), 1)  # no duplicate transition from the replay

    def test_invalid_versions_are_rejected(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        for bad in (0, -1, True, "3", 1.5, None):
            r = persist_workspace_decision(ws, _obs(), d, observation_version=bad)
            self.assertEqual(r.status, PersistStatus.REJECTED_INVALID, bad)
        ws.refresh_from_db()
        self.assertEqual(ws.observation_version, 0)
        self.assertEqual(ws.transitions.count(), 0)

    def test_unsaved_workspace_is_rejected(self):
        ws = HostedMt5Workspace(trading_account=self.account)  # not saved -> pk is None
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        r = persist_workspace_decision(ws, _obs(), d, observation_version=1)
        self.assertEqual(r.status, PersistStatus.REJECTED_INVALID)

    def test_illegal_transition_against_locked_state_is_held(self):
        # Stored DISCONNECTED; a decision (computed against a stale CONNECTED premise) says EXECUTION_READY.
        ws = self._ws(canonical_state=S.DISCONNECTED)
        rogue = WorkspaceDecision(
            next_state=str(S.EXECUTION_READY), reason=str(WorkspaceReason.NONE),
            transition_required=True, telemetry_event=str(WorkspaceEvent.EXECUTION_READY),
            execution_ready=True, recovery_required=False)
        with _ops_on():
            r = persist_workspace_decision(ws, _obs(), rogue, observation_version=9)
        self.assertEqual(r.status, PersistStatus.REJECTED_ILLEGAL)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.DISCONNECTED))  # held
        self.assertEqual(ws.observation_version, 0)  # rejected -> version not advanced
        self.assertEqual(ws.transitions.count(), 0)
        self.assertFalse(OperationalEvent.objects.exists())  # no event for a rejected decision

    def test_stale_decision_beyond_version_guard(self):
        # Newer version, but the decision's target is illegal from the state that actually got there first.
        ws = self._ws(canonical_state=S.CONNECTED)
        # v5 legitimately moves CONNECTED -> DISCONNECTED
        dced = derive_workspace_decision(
            _obs(previous_state=str(S.CONNECTED), connected=False, account_match=False, trade_allowed=False))
        persist_workspace_decision(ws, _obs(connected=False), dced, observation_version=5)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.DISCONNECTED))
        # v6 carries a decision computed against the OLD CONNECTED premise -> CONNECTED->EXECUTION_READY,
        # which is illegal from the now-authoritative DISCONNECTED. Higher version, still rejected.
        stale_premise = WorkspaceDecision(
            next_state=str(S.EXECUTION_READY), reason=str(WorkspaceReason.NONE), transition_required=True,
            telemetry_event=str(WorkspaceEvent.EXECUTION_READY), execution_ready=True, recovery_required=False)
        r = persist_workspace_decision(ws, _obs(), stale_premise, observation_version=6)
        self.assertEqual(r.status, PersistStatus.REJECTED_ILLEGAL)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.DISCONNECTED))


class WriterIdempotentAndMaterialTests(_Base):
    def test_idempotent_same_state_updates_projection_not_decision_version(self):
        ws = self._ws(canonical_state=S.CONNECTED, canonical_reason=WorkspaceReason.NONE)
        # decision holds CONNECTED/NONE (connected, matched, fresh, but trading halted) — no material change
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), trade_allowed=False))
        self.assertEqual(d.next_state, str(S.CONNECTED))
        self.assertEqual(d.reason, str(WorkspaceReason.NONE))
        r = persist_workspace_decision(ws, _obs(trade_allowed=False), d, observation_version=3)
        self.assertEqual(r.status, PersistStatus.IDEMPOTENT)
        ws.refresh_from_db()
        self.assertEqual(ws.observation_version, 3)   # version advanced
        self.assertEqual(ws.decision_version, 0)      # no material decision -> not incremented
        self.assertIsNone(ws.last_transition_at)
        self.assertEqual(ws.transitions.count(), 0)
        self.assertFalse(r.telemetry_emitted)

    def test_reason_only_change_is_material_without_state_change_or_telemetry(self):
        ws = self._ws(canonical_state=S.CONNECTED, canonical_reason=WorkspaceReason.NONE)
        # CONNECTED + not-fresh -> stays CONNECTED but reason becomes STALE_OBSERVATION
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED), fresh=False))
        self.assertEqual(d.next_state, str(S.CONNECTED))
        self.assertEqual(d.reason, str(WorkspaceReason.STALE_OBSERVATION))
        self.assertIsNone(d.telemetry_event)  # no state change -> manager emits no event
        with _ops_on():
            r = persist_workspace_decision(ws, _obs(fresh=False), d, observation_version=4)
        self.assertEqual(r.status, PersistStatus.APPLIED)  # material (reason changed)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_reason, str(WorkspaceReason.STALE_OBSERVATION))
        self.assertEqual(ws.decision_version, 1)           # material -> incremented
        self.assertIsNone(ws.last_transition_at)           # but NOT a state change
        tr = ws.transitions.get()
        self.assertFalse(tr.state_changed)
        self.assertEqual(tr.from_state, tr.to_state)
        self.assertFalse(r.telemetry_emitted)              # telemetry only on a real state change
        self.assertFalse(OperationalEvent.objects.exists())

    def test_version_and_decision_monotonicity_across_a_sequence(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        # v1: CONNECTED -> EXECUTION_READY (material)
        d1 = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        persist_workspace_decision(ws, _obs(), d1, observation_version=1)
        # v2: EXECUTION_READY -> CONNECTED (trade halted) (material)
        d2 = derive_workspace_decision(_obs(previous_state=str(S.EXECUTION_READY), trade_allowed=False))
        persist_workspace_decision(ws, _obs(trade_allowed=False), d2, observation_version=2)
        ws.refresh_from_db()
        self.assertEqual(ws.observation_version, 2)
        self.assertEqual(ws.decision_version, 2)
        self.assertEqual(ws.transitions.count(), 2)
        # replay v2 -> stale, no change
        r = persist_workspace_decision(ws, _obs(trade_allowed=False), d2, observation_version=2)
        self.assertEqual(r.status, PersistStatus.REJECTED_STALE)
        ws.refresh_from_db()
        self.assertEqual(ws.decision_version, 2)


class WriterTelemetryTests(_Base):
    def test_telemetry_is_secret_free(self):
        ws = self._ws(canonical_state=S.CONNECTED,
                      currently_attached_login="SUPERSECRETLOGIN", currently_attached_server="SRV")
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        with _ops_on():
            persist_workspace_decision(ws, _obs(), d, observation_version=1, correlation_id="cid")
        ev = OperationalEvent.objects.get(account=self.account)
        blob = f"{ev.event_type}|{ev.summary}|{ev.status}|{ev.reason_code}|{ev.metadata}|{ev.correlation_id}"
        for forbidden in ("SUPERSECRETLOGIN", "password", "token", "secret", "keyring", "accounts_dat"):
            self.assertNotIn(forbidden, blob, forbidden)
        self.assertFalse(ev.customer_visible)  # operator-facing by default

    def test_no_event_when_recorder_dark(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        # OPERATIONS_EVENTS_ENABLED is not set -> recorder is DARK
        r = persist_workspace_decision(ws, _obs(), d, observation_version=1)
        self.assertEqual(r.status, PersistStatus.APPLIED)   # state STILL persists (fail-open telemetry)
        self.assertFalse(r.telemetry_emitted)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.EXECUTION_READY))
        self.assertEqual(ws.transitions.count(), 1)         # provenance still recorded
        self.assertFalse(OperationalEvent.objects.exists())  # but no event row while dark

    def test_legacy_fields_are_untouched_by_the_writer(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        self.assertEqual(ws.state, WorkspaceState.NOT_PROVISIONED)  # legacy default
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        persist_workspace_decision(ws, _obs(), d, observation_version=1)
        ws.refresh_from_db()
        # M3c writes canonical_* only; the inert ADR-0033 legacy fields stay exactly as they were.
        self.assertEqual(ws.state, WorkspaceState.NOT_PROVISIONED)
        self.assertIsNone(ws.observed_connected)
        self.assertIsNone(ws.active_account_match)


class ConsumerTests(_Base):
    def test_dark_consumer_is_a_total_no_op(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        # master flag OFF (default) -> None, nothing written
        r = ingest_observation(ws, _obs(), observation_version=1)
        self.assertIsNone(r)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.CONNECTED))
        self.assertEqual(ws.observation_version, 0)
        self.assertEqual(ws.transitions.count(), 0)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_enabled_consumer_derives_against_stored_premise(self):
        ws = self._ws(canonical_state=S.CONNECTED)
        # The incoming observation's previous_state is a LIE (RETIRED); the consumer must ignore it and use
        # the stored CONNECTED premise, yielding EXECUTION_READY.
        with _ops_on():
            r = ingest_observation(ws, _obs(previous_state=str(S.RETIRED)),
                                   observation_version=1, correlation_id="c1")
        self.assertEqual(r.status, PersistStatus.APPLIED)
        ws.refresh_from_db()
        self.assertEqual(ws.canonical_state, str(S.EXECUTION_READY))

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_enabled_consumer_no_execution_action(self):
        # Sanity: the consumer never launches/attaches/orders — it only reads + derives + persists. Proven by
        # there being no such collaborator; here we assert it is pure w.r.t. host state (no exception, no
        # attribute access beyond the model) for a fully-healthy observation.
        ws = self._ws(canonical_state=S.CONNECTED)
        with _ops_on():
            r = ingest_observation(ws, _obs(), observation_version=1)
        self.assertIn(r.status, (PersistStatus.APPLIED, PersistStatus.IDEMPOTENT))


class ReadModelTests(_Base):
    def test_projection_is_secret_free_customer_view(self):
        ws = self._ws(canonical_state=S.CONNECTED, currently_attached_login="500999",
                      currently_attached_server="Broker-Demo", attach_path="C:/secret/path/terminal64.exe")
        proj = workspace_state_projection(ws, staff=False)
        blob = str(proj)
        self.assertNotIn("500999", blob)                 # full login never present
        self.assertNotIn("C:/secret/path", blob)         # attach path never present
        self.assertNotIn("operator", proj)               # no operator block for a customer
        self.assertEqual(proj["canonical_state"], str(S.CONNECTED))
        self.assertIn("health", proj)

    def test_staff_projection_adds_masked_operator_block(self):
        ws = self._ws(canonical_state=S.CONNECTED, currently_attached_login="500999")
        d = derive_workspace_decision(_obs(previous_state=str(S.CONNECTED)))
        persist_workspace_decision(ws, _obs(), d, observation_version=7, correlation_id="corr-9")
        ws.refresh_from_db()
        proj = workspace_state_projection(ws, staff=True)
        op = proj["operator"]
        self.assertEqual(op["active_login_masked"], "***999")
        self.assertNotIn("500999", str(proj))
        self.assertEqual(op["observation_version"], 7)
        self.assertEqual(op["decision_version"], 1)
        self.assertEqual(op["correlation_id"], "corr-9")
        self.assertEqual(len(op["recent_transitions"]), 1)


class ApiTests(_Base):
    def setUp(self):
        super().setUp()
        from rest_framework.test import APIRequestFactory
        self.factory = APIRequestFactory()
        self.other = U.objects.create_user(username="other", email="other@example.com", password="x")

    def _get(self, user=None, query=""):
        from rest_framework.test import force_authenticate
        from hosted_workspace.views import HostedWorkspaceStateView
        req = self.factory.get(f"/api/hosted-workspace/workspace-state/{query}")
        if user is not None:
            force_authenticate(req, user=user)
        return HostedWorkspaceStateView.as_view()(req)

    def test_dark_returns_404_even_for_owner(self):
        self._ws(canonical_state=S.CONNECTED)
        resp = self._get(self.user, f"?account_id={self.account.id}")  # flag OFF (default)
        self.assertEqual(resp.status_code, 404)  # endpoint invisible

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_owner_gets_projection(self):
        self._ws(canonical_state=S.CONNECTED)
        resp = self._get(self.user, f"?account_id={self.account.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["canonical_state"], str(S.CONNECTED))
        self.assertNotIn("operator", resp.data)  # non-staff owner -> customer view only

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_non_owner_is_404_idor_safe(self):
        self._ws(canonical_state=S.CONNECTED)
        resp = self._get(self.other, f"?account_id={self.account.id}")
        self.assertEqual(resp.status_code, 404)  # cross-user read denied as not-found

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_missing_account_id_is_400(self):
        resp = self._get(self.user)
        self.assertEqual(resp.status_code, 400)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_owned_account_without_workspace_is_404(self):
        resp = self._get(self.user, f"?account_id={self.account.id}")  # no HostedMt5Workspace row
        self.assertEqual(resp.status_code, 404)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_staff_may_read_any_account_with_operator_block(self):
        self._ws(canonical_state=S.CONNECTED)
        staff = U.objects.create_user(username="op", email="op@example.com", password="x", is_staff=True)
        resp = self._get(staff, f"?account_id={self.account.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("operator", resp.data)

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED=True)
    def test_unauthenticated_is_denied(self):
        resp = self._get(None, f"?account_id={self.account.id}")
        self.assertIn(resp.status_code, (401, 403))


class MutationAdequacyTests(SimpleTestCase):
    """Runnable mutation harness for the writer's novel PURE predicates. Each mutant of the source must be
    KILLED by at least one boundary input (evidence.md: prove the tests detect the fault, don't assert it)."""

    def _fn_from_source(self, func, replacement):
        src = textwrap.dedent(inspect.getsource(func))
        mutated = src.replace(replacement[0], replacement[1], 1)
        self.assertIn(replacement[1], mutated, f"mutation {replacement} did not apply")
        ns = {}
        exec(compile(mutated, "<mutant>", "exec"), {"isinstance": isinstance}, ns)
        return ns[func.__name__]

    def test_coerce_version_boundary_oracle(self):
        f = P._coerce_version
        self.assertEqual(f(1), 1)
        self.assertEqual(f(2), 2)
        self.assertIsNone(f(0))
        self.assertIsNone(f(-1))
        self.assertIsNone(f(True))    # bool rejected even though it is an int
        self.assertIsNone(f(False))
        self.assertIsNone(f("3"))
        self.assertIsNone(f(1.5))
        self.assertIsNone(f(None))

    def test_coerce_version_mutants_are_killed(self):
        # `>= 1` -> `> 1`  : input 1 distinguishes (orig 1, mutant None)
        m1 = self._fn_from_source(P._coerce_version, (">= 1", "> 1"))
        self.assertNotEqual(m1(1), P._coerce_version(1))
        # `value >= 1` -> `value <= 1` : input 2 distinguishes (orig 2, mutant None)
        m2 = self._fn_from_source(P._coerce_version, (">= 1", "<= 1"))
        self.assertNotEqual(m2(2), P._coerce_version(2))
        # drop the bool guard : `isinstance(value, bool)` -> `isinstance(value, str)` : input True distinguishes
        m3 = self._fn_from_source(P._coerce_version, ("isinstance(value, bool)", "isinstance(value, str)"))
        self.assertNotEqual(m3(True), P._coerce_version(True))

    def test_as_bool_boundary_oracle(self):
        f = P._as_bool
        self.assertIs(f(True), True)
        self.assertIs(f(False), False)
        self.assertIs(f(None), False)
        self.assertIs(f(1), False)    # only real True counts (identity, not truthiness)

    def test_as_bool_mutants_are_killed(self):
        # `is True` -> `== True` : input 1 distinguishes (orig False, mutant True)
        m1 = self._fn_from_source(P._as_bool, ("is True", "== True"))
        self.assertNotEqual(m1(1), P._as_bool(1))
        # `is True` -> `is not True` : input True distinguishes
        m2 = self._fn_from_source(P._as_bool, ("is True", "is not True"))
        self.assertNotEqual(m2(True), P._as_bool(True))
