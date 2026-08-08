"""ADR-0034 Workspace Delivery — the single authoritative delivery-state writer.

Covers: attempt recording (AUTHORIZED / FAILED) with the stable reason; connect/disconnect transitions and
their ``remoteapp_ready`` + ``last_delivery_success`` semantics; telemetry emitted ONLY from the writer, as
a secret-free ``workspace.remoteapp_*`` operational event; host-node assignment (idempotent + fail-closed);
session-reuse selection (excludes ended/failed); and legacy-safety — the delivery writer never disturbs the
canonical M3c state or the legacy attach state, and never trips the immutable-binding guard.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from execution.models import TerminalNode
from mt5.models import InteractionSession, MT5Session, TerminalBinding
from operational_events.models import OperationalEvent
from trading.models import TradingAccount

from hosted_workspace import delivery_persistence as DP
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
DS = HostedMt5Workspace.DeliveryState


def _ops_on():
    return mock.patch.dict(os.environ, {"OPERATIONS_EVENTS_ENABLED": "1"}, clear=False)


class _Auth:
    def __init__(self, authorized, reason):
        self.authorized = authorized
        self.reason = reason


class _Base(TestCase):
    def setUp(self):
        self.user = U.objects.create_user(username="wr", email="wr@example.com", password="x")
        self.node = TerminalNode.objects.create(hostname="wr-node", status=TerminalNode.Status.ACTIVE)
        self.account = TradingAccount.objects.create(
            user=self.user, name="wr-acct", broker_name="Broker-Demo",
            account_number="800222", is_demo=True)
        self.ws = HostedMt5Workspace.objects.create(trading_account=self.account)


class AttemptRecordingTests(_Base):
    def test_authorized_attempt(self):
        res = DP.record_delivery_attempt(self.ws, _Auth(True, "DA_OK"), correlation_id="corr-1")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.delivery_state, DS.AUTHORIZED)
        self.assertEqual(self.ws.delivery_reason, "DA_OK")
        self.assertIsNotNone(self.ws.last_delivery_attempt)
        self.assertEqual(res.delivery_state, DS.AUTHORIZED)

    def test_failed_attempt(self):
        DP.record_delivery_attempt(self.ws, _Auth(False, "DA_NODE_UNASSIGNED"))
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.delivery_state, DS.FAILED)
        self.assertEqual(self.ws.delivery_reason, "DA_NODE_UNASSIGNED")

    def test_attempt_does_not_touch_canonical_or_legacy_state(self):
        before_canon = self.ws.canonical_state
        before_legacy = self.ws.state
        DP.record_delivery_attempt(self.ws, _Auth(True, "DA_OK"))
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.canonical_state, before_canon)  # M3c untouched
        self.assertEqual(self.ws.state, before_legacy)           # legacy attach untouched


class RemoteAppTransitionTests(_Base):
    def test_connected_sets_ready_and_success_and_emits(self):
        with _ops_on():
            res = DP.record_remoteapp_connected(self.ws, event_seq=1, correlation_id="corr-c")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.delivery_state, DS.CONNECTED)
        self.assertTrue(self.ws.remoteapp_ready)
        self.assertIsNotNone(self.ws.last_delivery_success)
        self.assertTrue(res.applied)
        self.assertTrue(res.telemetry_emitted)
        ev = OperationalEvent.objects.filter(event_type="workspace.remoteapp_connected").first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.account_id, self.account.id)

    def test_disconnected_clears_ready_retains_session_and_emits(self):
        with _ops_on():
            DP.record_remoteapp_connected(self.ws, event_seq=1)
            prior_success = HostedMt5Workspace.objects.get(pk=self.ws.pk).last_delivery_success
            DP.record_remoteapp_disconnected(self.ws, event_seq=2, correlation_id="corr-d")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.delivery_state, DS.DISCONNECTED)
        self.assertFalse(self.ws.remoteapp_ready)
        # Disconnect is NOT a teardown — last_delivery_success (the persistent session marker) is retained.
        self.assertEqual(self.ws.last_delivery_success, prior_success)
        self.assertTrue(OperationalEvent.objects.filter(
            event_type="workspace.remoteapp_disconnected").exists())

    def test_reordered_event_rejected_last_actual_wins(self):
        """A late CONNECTED (lower seq) arriving AFTER a real DISCONNECTED must NOT resurrect the session."""
        with _ops_on():
            DP.record_remoteapp_connected(self.ws, event_seq=1)
            DP.record_remoteapp_disconnected(self.ws, event_seq=2)
            res = DP.record_remoteapp_connected(self.ws, event_seq=1)  # stale/reordered
        self.ws.refresh_from_db()
        self.assertFalse(res.applied)
        self.assertFalse(res.telemetry_emitted)
        self.assertEqual(self.ws.delivery_state, DS.DISCONNECTED)  # held, not resurrected
        self.assertFalse(self.ws.remoteapp_ready)

    def test_replayed_same_seq_is_idempotent_single_event(self):
        with _ops_on():
            r1 = DP.record_remoteapp_connected(self.ws, event_seq=5)
            r2 = DP.record_remoteapp_connected(self.ws, event_seq=5)  # replay of the SAME event
        self.assertTrue(r1.applied)
        self.assertFalse(r2.applied)  # seq <= stored -> rejected, no double-write
        # Exactly ONE connected event emitted despite the replay (seq-keyed dedup + staleness gate).
        self.assertEqual(OperationalEvent.objects.filter(
            event_type="workspace.remoteapp_connected").count(), 1)

    def test_invalid_seq_rejected(self):
        for bad in (0, -1, None, True, "3"):
            res = DP.record_remoteapp_connected(self.ws, event_seq=bad)
            self.assertFalse(res.applied, f"seq={bad!r} should be rejected")
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.delivery_state, DS.NONE)  # nothing applied

    def test_telemetry_is_secret_free(self):
        with _ops_on():
            DP.record_remoteapp_connected(self.ws, event_seq=1)
        ev = OperationalEvent.objects.filter(event_type="workspace.remoteapp_connected").first()
        blob = f"{ev.summary} {ev.metadata} {ev.reason_code} {ev.status}"
        for banned in ("password", "runtime_root", "windows", "accounts.dat"):
            self.assertNotIn(banned, blob.lower())


class HostNodeAssignmentTests(_Base):
    def test_assign_is_idempotent(self):
        self.assertTrue(DP.assign_workspace_node(self.ws, self.node))
        self.ws.refresh_from_db()
        self.assertEqual(self.ws.workspace_node_id, self.node.pk)
        # Second identical assign is a no-op (returns False, no redundant write).
        self.assertFalse(DP.assign_workspace_node(self.ws, self.node))

    def test_assign_none_fails_closed(self):
        self.assertFalse(DP.assign_workspace_node(self.ws, None))
        self.ws.refresh_from_db()
        self.assertIsNone(self.ws.workspace_node_id)  # never cleared as a side effect


class SessionReuseTests(_Base):
    def _mt5_session(self, state, expires_at=None):
        binding = TerminalBinding.objects.create(
            terminal_node=self.node, terminal_identifier=f"tid-{state}-{expires_at}",
            mt5_account_login="800222", environment_type="demo")
        isess = InteractionSession.objects.create(user=self.user, terminal_binding=binding)
        return MT5Session.objects.create(
            interaction_session=isess, terminal_binding=binding,
            hosted_workspace=self.ws, state=state, expires_at=expires_at)

    def test_none_when_no_sessions(self):
        self.assertIsNone(DP.reusable_delivery_session(self.ws))

    def test_live_session_is_reused(self):
        live = self._mt5_session("connected")
        self.assertEqual(DP.reusable_delivery_session(self.ws).pk, live.pk)

    def test_ended_and_failed_sessions_excluded(self):
        self._mt5_session("ended")
        self._mt5_session("failed")
        self.assertIsNone(DP.reusable_delivery_session(self.ws))

    def test_expired_session_not_reused(self):
        from django.utils import timezone
        from datetime import timedelta
        # A still-"connected" session whose lease already lapsed must NOT be handed back for reuse.
        self._mt5_session("connected", expires_at=timezone.now() - timedelta(minutes=5))
        self.assertIsNone(DP.reusable_delivery_session(self.ws))

    def test_unexpired_and_null_expiry_sessions_still_reusable(self):
        from django.utils import timezone
        from datetime import timedelta
        future = self._mt5_session("connected", expires_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(DP.reusable_delivery_session(self.ws).pk, future.pk)
        future.delete()
        null_exp = self._mt5_session("connected", expires_at=None)  # NULL expiry = not lease-bound
        self.assertEqual(DP.reusable_delivery_session(self.ws).pk, null_exp.pk)
