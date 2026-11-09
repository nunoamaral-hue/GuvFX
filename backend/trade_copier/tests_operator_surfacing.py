"""Synthetic tests for the operator failure-surfacing layer (ADR-0083 Phase-3, packet 026): the extended
failure-alert contract + the read-only operator lifecycle view. DARK: everything is gated default-OFF. Nothing
here places/modifies/closes an order or contacts a broker — the lifecycle rows are constructed directly.
"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from trade_copier import failure_alerts as fa
from trade_copier.lifecycle import LifecycleState
from trade_copier.models import (
    CopierFollower, CopierMaster, CopierPositionLifecycle, CopierReplication, MasterEvent,
    MasterObservationPoll, ReplicationStatus, SizingMode)
from trade_copier import services
from trade_copier.reconcile import MATCHED
from trading.models import BrokerServer, TradingAccount

User = get_user_model()

ALERTS_ON = dict(COPIER_ENABLED="1", COPIER_ALERTS_ENABLED="1")
VIEW_ON = dict(COPIER_ENABLED="1", COPIER_OPERATOR_VIEW_ENABLED="1")


def _acct(user, name, num, *, server="IS6Technologies-Live", display="IS6", demo=False):
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": "live"})
    return TradingAccount.objects.create(
        user=user, name=name, broker_name=display, account_number=num,
        is_demo=demo, is_active=True, broker_server=bs)


class SurfacingFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", "owner@guvfx.invalid", "x")
        self.other = User.objects.create_user("other", "other@guvfx.invalid", "x")
        self.staff = User.objects.create_user("staff", "staff@guvfx.invalid", "x", is_staff=True)
        self.macct = _acct(self.owner, "m", "55425442", server="Tradersway-Live", display="TradersWay")
        self.facct = _acct(self.owner, "f", "51763831", server="IS6Technologies-Live", display="IS6")
        self.master = CopierMaster.objects.create(trading_account=self.macct, observe_enabled=True)
        self.f = services.add_follower(self.master, self.facct, sizing_mode=SizingMode.FIXED_LOT,
                                       fixed_lot=Decimal("0.01"))
        services.set_follower_enabled(self.f, True, actor=self.staff)

    def _poll(self, at=None):
        at = at or timezone.now()
        return MasterObservationPoll.objects.create(master=self.master, observed_at=at, read_at=at,
                                                    position_count=1)

    def _event(self, ticket, *, seq=10, etype=MasterEvent.EventType.OPEN, source="POLL", at=None):
        at = at or timezone.now()
        return MasterEvent.objects.create(
            master=self.master, sequence=seq, from_poll=self._poll(at), event_type=etype,
            position_ticket=ticket, symbol="XAUUSD", side="SELL", volume_before=Decimal("0"),
            volume_after=Decimal("0.01"), volume_delta=Decimal("0.01"), observed_at=at, source=source)

    def _lifecycle(self, *, ticket=500, state=LifecycleState.LINKED.value, follower_ticket=777,
                   reconciled=True, failure_reason="", last_at=None, closed=False, open_at=None,
                   target_volume="0.01"):
        now = timezone.now()
        ev = self._event(ticket, seq=ticket)   # unique per-ticket sequence (avoids master+seq collisions)
        if reconciled:
            CopierReplication.objects.create(
                master_event=ev, follower=self.f, requested_op="OPEN",
                status=ReplicationStatus.EXECUTION_VERIFIED, reconciliation_status=MATCHED,
                follower_ticket=follower_ticket, follower_symbol="XAUUSD.std",
                target_volume=Decimal(target_volume) if target_volume else None,
                constraints_authoritative=True, sltp_translation_ok=True)
        row = CopierPositionLifecycle.objects.create(
            master=self.master, follower=self.f, master_position_ticket=ticket, state=state,
            is_terminal=state in (LifecycleState.CLOSED.value, LifecycleState.ENTRY_FAILED_FINAL.value,
                                  LifecycleState.CONTAINED_FAILURE.value),
            follower_ticket=follower_ticket if reconciled else None, failure_reason=failure_reason,
            open_event=ev, open_observed_at=open_at or now,
            linked_at=now if state != LifecycleState.OBSERVED_OPEN.value else None,
            closed_at=now if closed else None, last_transition_at=last_at or now)
        return row


# ─────────────────────────── Phase 2 — failure-alert contract ───────────────────────────

@override_settings(**ALERTS_ON)
class AlertContractTests(SurfacingFixture):
    def test_all_packet_reason_codes_exist(self):
        for code in ("ENTRY_FAILED_FINAL", "MODIFICATION_FAILED", "PARTIAL_CLOSE_FAILED", "FULL_CLOSE_FAILED",
                     "DIVERGED", "UNKNOWN_OUTCOME", "CONTAINED_FAILURE", "DEADLINE_EXCEEDED",
                     "PRE_STATE_MISMATCH", "AUTHORIZATION_EXPIRED_OR_INVALID"):
            self.assertIn(code, fa.ALL_CATEGORIES, code)

    def test_reason_classification(self):
        self.assertEqual(fa.classify_lifecycle_failure("governing_authorization_revoked_or_absent"),
                         fa.AUTHORIZATION_EXPIRED_OR_INVALID)
        self.assertEqual(fa.classify_lifecycle_failure("superseded_by_later_close"), fa.PRE_STATE_MISMATCH)
        self.assertEqual(fa.classify_lifecycle_failure("broker_outcome_unknown"), fa.UNKNOWN_OUTCOME)
        self.assertEqual(fa.classify_lifecycle_failure("deadline_exceeded"), fa.DEADLINE_EXCEEDED)
        self.assertEqual(fa.classify_lifecycle_failure("close_failed"), fa.CLOSE_FAILED_FINAL)
        self.assertEqual(fa.classify_lifecycle_failure("something_unseen"), fa.CONTAINED_FAILURE)  # safe fallback

    def test_dedup_no_storm(self):
        from reliability.models import AlertEvent
        self.assertTrue(fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:1", reason="x", revision=1))
        # Replays / restarts / duplicates at the same-or-lower revision never create a second open row.
        for _ in range(5):
            self.assertFalse(fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:1", reason="x",
                                                   revision=1))
        self.assertEqual(AlertEvent.objects.filter(dedup_key="copier:UNKNOWN_OUTCOME:lifecycle:1").count(), 1)

    def test_newer_revision_replaces_in_place_with_audit_link(self):
        from reliability.models import AlertEvent
        fa.raise_copier_alert(fa.MODIFY_FAILED_FINAL, scope_key="lifecycle:2", reason="r1", revision=1)
        self.assertTrue(fa.raise_copier_alert(fa.MODIFY_FAILED_FINAL, scope_key="lifecycle:2", reason="r2",
                                              revision=2))
        rows = AlertEvent.objects.filter(dedup_key="copier:MODIFICATION_FAILED:lifecycle:2")
        self.assertEqual(rows.count(), 1)  # no storm — replaced in place
        d = rows.get().detail
        self.assertEqual(d["revision"], 2)
        self.assertEqual(d["reason"], "r2")
        self.assertEqual(d["supersedes_revision"], 1)
        self.assertEqual(d["revision_history"][-1]["revision"], 1)  # auditable link to the prior revision

    def test_stale_revision_does_not_overwrite_newer(self):
        fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:3", reason="new", revision=5)
        self.assertFalse(fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:3", reason="old",
                                               revision=2))
        from reliability.models import AlertEvent
        self.assertEqual(AlertEvent.objects.get(dedup_key="copier:UNKNOWN_OUTCOME:lifecycle:3").detail["reason"],
                         "new")

    def test_alert_is_secret_free(self):
        from reliability.models import AlertEvent
        fa.raise_copier_alert(fa.PRE_STATE_MISMATCH, scope_key="lifecycle:4", ticket=99887766,
                              reason="symbol_changed", correlation_id="corr-xyz", revision=1)
        a = AlertEvent.objects.get(dedup_key="copier:PRE_STATE_MISMATCH:lifecycle:4")
        blob = (a.title + a.body + str(a.detail)).lower()
        for secret in ("password", "token", "secret", "55425442", "51763831"):
            self.assertNotIn(secret, blob)

    def test_never_claims_recovery_or_zero(self):
        from reliability.models import AlertEvent
        fa.raise_copier_alert(fa.FULL_CLOSE_FAILED, scope_key="lifecycle:5", reason="close_failed", revision=1)
        a = AlertEvent.objects.get(dedup_key="copier:FULL_CLOSE_FAILED:lifecycle:5")
        self.assertEqual(a.status, AlertEvent.Status.OPEN)   # never auto-resolved from silence
        for claim in ("recovered", "closed_ok", "zero_exposure", "success"):
            self.assertNotIn(claim, str(a.detail).lower())

    @override_settings(COPIER_ENABLED="1", COPIER_ALERTS_ENABLED="0")
    def test_dark_when_flag_off(self):
        from reliability.models import AlertEvent
        self.assertFalse(fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:6", revision=1))
        self.assertEqual(AlertEvent.objects.count(), 0)

    def test_pass_surfaces_close_failed_as_full_close_failed(self):
        # End-to-end: a CONTAINED_FAILURE lifecycle whose latest trigger is close_failed must raise a
        # FULL_CLOSE_FAILED operator alert (scenario 21), carrying the lifecycle revision.
        from reliability.models import AlertEvent
        from trade_copier.models import CopierLifecycleTransition
        row = self._lifecycle(ticket=701, state=LifecycleState.CONTAINED_FAILURE.value,
                              failure_reason="close_failed")
        row.applied_sequence = 42
        row.save(update_fields=["applied_sequence"])
        CopierLifecycleTransition.objects.create(lifecycle=row, from_state="CLOSE_PENDING",
                                                 to_state="CONTAINED_FAILURE", trigger="close_failed", sequence=42)
        out = fa.run_failure_alert_pass()
        self.assertTrue(out["enabled"])
        self.assertGreaterEqual(out["raised"], 1)
        a = AlertEvent.objects.get(dedup_key=f"copier:FULL_CLOSE_FAILED:lifecycle:{row.id}")
        self.assertEqual(a.detail["category"], fa.FULL_CLOSE_FAILED)
        self.assertEqual(a.detail["revision"], 42)
        self.assertEqual(a.status, AlertEvent.Status.OPEN)

    def test_closed_lifecycle_raises_no_false_failure(self):
        # A1 regression: a successfully CLOSED lifecycle must NEVER manufacture a (CRITICAL) failure alert.
        from reliability.models import AlertEvent
        row = self._lifecycle(ticket=800, state=LifecycleState.CLOSED.value, closed=True)
        out = fa.run_failure_alert_pass()
        self.assertTrue(out["enabled"])
        self.assertFalse(AlertEvent.objects.filter(dedup_key__contains=f"lifecycle:{row.id}").exists())
        self.assertEqual(AlertEvent.objects.count(), 0)

    def test_ticket_redacted_in_alert(self):
        # A6: the alert carries only a redacted ticket tail (never the full position ticket).
        from reliability.models import AlertEvent
        fa.raise_copier_alert(fa.UNKNOWN_OUTCOME, scope_key="lifecycle:9", ticket=99887766, revision=1)
        a = AlertEvent.objects.get(dedup_key="copier:UNKNOWN_OUTCOME:lifecycle:9")
        self.assertEqual(a.detail["ticket_redacted"], "…7766")
        self.assertNotIn("99887766", a.title + a.body + str(a.detail))


# ─────────────────────────── Phase 3 — read-only operator view ───────────────────────────

@override_settings(**VIEW_ON)
class OperatorViewServiceTests(SurfacingFixture):
    def _summary(self, **kw):
        from trade_copier.operator_view import build_operator_summary
        return build_operator_summary(self._lifecycle(**kw))

    def test_redaction_masks_login_and_ticket(self):
        s = self._summary(state=LifecycleState.LINKED.value, follower_ticket=99887766)
        self.assertEqual(s["master"]["login_redacted"], "…5442")
        self.assertEqual(s["follower"]["login_redacted"], "…3831")
        self.assertEqual(s["follower"]["ticket_redacted"], "…7766")
        # Full numbers never appear anywhere in the summary.
        blob = str(s)
        self.assertNotIn("55425442", blob)
        self.assertNotIn("51763831", blob)
        self.assertNotIn("99887766", blob)

    def test_linked_matched_is_verified_and_matches(self):
        s = self._summary(state=LifecycleState.LINKED.value, reconciled=True)
        self.assertEqual(s["data_quality"], "VERIFIED")
        self.assertTrue(s["follower_matches_master"])
        self.assertTrue(s["exposure_may_exist"])
        self.assertEqual(s["requested_volume"], "0.010")
        self.assertTrue(s["position_existence_confirmed"])
        # MATCHED confirms existence, NOT fill volume — confirmed_volume must stay null (never the request).
        self.assertIsNone(s["confirmed_volume"])

    def test_entry_failed_final_with_match_is_unknown_not_verified(self):
        # F1 regression: a lifecycle that reconciled MATCHED but reached ENTRY_FAILED_FINAL (linked AFTER the
        # deadline) has a genuinely OPEN follower position — it must NOT be reported as no-exposure/VERIFIED.
        row = self._lifecycle(state=LifecycleState.ENTRY_FAILED_FINAL.value, reconciled=True, follower_ticket=777)
        row.follower_ticket = None   # the engine does not copy the ticket into the terminal ctx
        row.save(update_fields=["follower_ticket"])
        from trade_copier.operator_view import build_operator_summary
        s = build_operator_summary(row)
        self.assertTrue(s["exposure_may_exist"])             # the MATCHED replication proves a position exists
        self.assertEqual(s["data_quality"], "UNKNOWN")       # terminal contradicts the evidence -> investigate
        self.assertTrue(s["manual_investigation_required"])

    def test_unreconciled_never_zero_or_matched(self):
        # Missing reconcile evidence must NOT become zero/closed/successful.
        s = self._summary(state=LifecycleState.ENTRY_PENDING.value, reconciled=False, follower_ticket=None)
        self.assertFalse(s["follower_matches_master"])
        self.assertIsNone(s["confirmed_volume"])            # never 0
        self.assertTrue(s["exposure_may_exist"])             # conservative while unconfirmed
        self.assertIn(s["data_quality"], ("PARTIAL", "UNKNOWN", "STALE"))

    def test_contained_failure_is_unknown_and_needs_investigation(self):
        s = self._summary(state=LifecycleState.CONTAINED_FAILURE.value, failure_reason="link_divergent")
        self.assertEqual(s["data_quality"], "UNKNOWN")
        self.assertTrue(s["manual_investigation_required"])
        self.assertTrue(s["exposure_may_exist"])

    def test_closed_is_verified_no_exposure(self):
        s = self._summary(state=LifecycleState.CLOSED.value, closed=True)
        self.assertEqual(s["data_quality"], "VERIFIED")
        self.assertFalse(s["exposure_may_exist"])
        self.assertFalse(s["manual_investigation_required"])

    def test_entry_failed_final_no_exposure_truthful(self):
        s = self._summary(state=LifecycleState.ENTRY_FAILED_FINAL.value, reconciled=False, follower_ticket=None)
        self.assertFalse(s["exposure_may_exist"])            # never entered
        self.assertEqual(s["data_quality"], "VERIFIED")      # a confirmed terminal, not "success"

    def test_stale_non_terminal(self):
        old = timezone.now() - timedelta(hours=2)
        s = self._summary(state=LifecycleState.LINKED.value, last_at=old)
        self.assertEqual(s["data_quality"], "STALE")

    def test_authorization_no_secret(self):
        # The authz block (when present) exposes only status/expiry/correlation — never a secret.
        s = self._summary(state=LifecycleState.LINKED.value)
        # No governing authz created in this fixture → authorization is None (truthful), not fabricated.
        self.assertIsNone(s["authorization"])


@override_settings(**VIEW_ON)
class OperatorApiTests(SurfacingFixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self._lifecycle(ticket=500, state=LifecycleState.CONTAINED_FAILURE.value, failure_reason="link_divergent")
        self.url = "/api/copy-trading/operator/lifecycles/"

    def test_non_staff_denied_tenant_isolation(self):
        # The owner of the accounts, but NOT staff — denied outright (no cross-tenant/customer access path).
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        # A different user — also denied.
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_denied(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))

    def test_staff_sees_redacted_rows(self):
        self.client.force_authenticate(self.staff)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["enabled"])
        self.assertGreaterEqual(body["total"], 1)
        self.assertNotIn("55425442", str(body))

    def test_read_only_no_mutation_surface(self):
        self.client.force_authenticate(self.staff)
        for method in (self.client.post, self.client.put, self.client.patch, self.client.delete):
            self.assertEqual(method(self.url, {}, format="json").status_code, 405)

    def test_pagination_bounds(self):
        self.client.force_authenticate(self.staff)
        self.assertEqual(self.client.get(self.url + "?limit=0").status_code, 400)
        self.assertEqual(self.client.get(self.url + "?limit=99999").status_code, 400)
        self.assertEqual(self.client.get(self.url + "?offset=-1").status_code, 400)
        r = self.client.get(self.url + "?limit=1&offset=0")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 1)

    def test_attention_filter(self):
        self.client.force_authenticate(self.staff)
        r = self.client.get(self.url + "?attention=true")
        self.assertEqual(r.status_code, 200)
        for item in r.json()["items"]:
            self.assertEqual(item["state"], LifecycleState.CONTAINED_FAILURE.value)

    @override_settings(COPIER_ENABLED="1", COPIER_OPERATOR_VIEW_ENABLED="0")
    def test_dark_when_flag_off(self):
        self.client.force_authenticate(self.staff)
        body = self.client.get(self.url).json()
        self.assertFalse(body["enabled"])
        self.assertEqual(body["items"], [])


@override_settings(**VIEW_ON)
class OperatorViewQueryBoundTests(SurfacingFixture):
    def test_query_count_is_bounded_not_per_row(self):
        # F3 regression: the page builds with a small CONSTANT number of queries regardless of row count
        # (bulk prefetch), not O(rows). Build 6 lifecycles, then assert the query count is bounded.
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from trade_copier.operator_view import list_operator_lifecycles
        for t in range(600, 606):
            self._lifecycle(ticket=t, state=LifecycleState.LINKED.value, follower_ticket=700 + t)
        with CaptureQueriesContext(connection) as ctx:
            data = list_operator_lifecycles(limit=50)
        self.assertEqual(len(data["items"]), 6)
        # count + rows + 3 bulk prefetches (+ a little slack) — far below 3-per-row (~20) fan-out.
        self.assertLess(len(ctx.captured_queries), 10, [q["sql"][:60] for q in ctx.captured_queries])


class RedactionUnitTests(SimpleTestCase):
    def test_redact(self):
        from trade_copier.operator_view import _redact
        self.assertEqual(_redact("55425442"), "…5442")
        self.assertEqual(_redact(99887766), "…7766")
        self.assertIsNone(_redact(None))
        self.assertIsNone(_redact(""))
        self.assertEqual(_redact("12"), "…")     # short values collapse to a constant mask, never shown whole
        self.assertEqual(_redact("6411"), "…")   # a 4-char value is NOT echoed whole
