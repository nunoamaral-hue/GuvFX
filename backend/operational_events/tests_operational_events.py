"""WP5.1 — Operational Event Model (ADR-0032) tests.

Covers the packet's required matrix: timeline ordering, summary correctness, severity mapping, customer
vs operator visibility, reason codes, empty history, multiple accounts, pagination, ownership, DTO
immutability, and the API — plus the mandatory DARK-default (flag OFF) no-op path, idempotency
(event-duplication protection), and metadata secret-safety.

House idioms mirrored from reliability/execution tests: django.test.TestCase, direct .objects.create,
env-flag flip via mock.patch.dict, APIRequestFactory + force_authenticate for the view.
"""
from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from users.models import User
from trading.models import TradingAccount

from .constants import normalize_severity
from .dto import OperationalEventDTO, OperationalSummaryDTO
from .events import mark_resolved, record_event
from .models import OperationalEvent
from .query import OperationalQueryService as Q
from .summary import build_operational_summary
from .views import OperationalAccountEventsView

_ON = {"OPERATIONS_EVENTS_ENABLED": "1"}
_OFF = {"OPERATIONS_EVENTS_ENABLED": "0"}


def _user(name, *, staff=False):
    u = User.objects.create_user(username=name, email=f"{name}@example.com", password="pw-12345")
    if staff:
        u.is_staff = True
        u.save(update_fields=["is_staff"])
    return u


def _account(user, name="A1", number="1000001"):
    return TradingAccount.objects.create(
        user=user, name=name, account_number=number, is_demo=True, broker_name="DemoBroker")


def _ev(account, *, category="HEALTH", event_type="X", severity="INFO", customer_visible=True,
        resolved=False, reason_code="", dedup_key="", metadata=None, status="", source="system",
        summary=""):
    return OperationalEvent.objects.create(
        account=account, category=category, event_type=event_type, severity=severity,
        customer_visible=customer_visible, resolved=resolved, reason_code=reason_code,
        dedup_key=dedup_key, metadata=metadata or {}, status=status, source=source, summary=summary)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Recorder
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class RecorderTests(TestCase):
    def setUp(self):
        self.u = _user("rec")
        self.a = _account(self.u)

    def test_dark_default_is_a_noop(self):
        with mock.patch.dict(os.environ, _OFF):
            out = record_event(account=self.a, category="VALIDATION", event_type="BROKER_VALIDATED")
        self.assertIsNone(out)
        self.assertEqual(OperationalEvent.objects.count(), 0)

    def test_records_and_returns_dto_when_enabled(self):
        with mock.patch.dict(os.environ, _ON):
            out = record_event(account=self.a, category="VALIDATION", event_type="BROKER_VALIDATED",
                               severity="INFO", summary="Validated", reason_code="validated")
        self.assertIsInstance(out, OperationalEventDTO)
        self.assertEqual(OperationalEvent.objects.count(), 1)
        row = OperationalEvent.objects.get()
        self.assertEqual(row.category, "VALIDATION")
        self.assertEqual(row.reason_code, "validated")
        self.assertEqual(out.account_id, self.a.id)

    def test_severity_is_normalised(self):
        with mock.patch.dict(os.environ, _ON):
            self.assertEqual(record_event(account=self.a, category="HEALTH", event_type="a",
                                          severity="WARN").severity, "WARNING")
            self.assertEqual(record_event(account=self.a, category="HEALTH", event_type="b",
                                          severity="debug").severity, "INFO")
            self.assertEqual(record_event(account=self.a, category="HEALTH", event_type="c",
                                          severity="bogus").severity, "INFO")
            self.assertEqual(record_event(account=self.a, category="HEALTH", event_type="d",
                                          severity="CRITICAL").severity, "CRITICAL")

    def test_customer_visible_default_by_category_and_override(self):
        with mock.patch.dict(os.environ, _ON):
            val = record_event(account=self.a, category="VALIDATION", event_type="v")
            exe = record_event(account=self.a, category="EXECUTION", event_type="e")
            exe_override = record_event(account=self.a, category="EXECUTION", event_type="e2",
                                        customer_visible=True)
        self.assertTrue(val.customer_visible)          # VALIDATION defaults customer-visible
        self.assertFalse(exe.customer_visible)         # EXECUTION defaults operator-only
        self.assertTrue(exe_override.customer_visible)  # explicit override wins

    def test_metadata_secrets_are_redacted(self):
        with mock.patch.dict(os.environ, _ON):
            out = record_event(account=self.a, category="CREDENTIAL", event_type="rot",
                               metadata={"password": "hunter2", "note": "ok",
                                         "nested": {"api_key": "zzz", "count": 3}})
        self.assertEqual(out.metadata["password"], "[REDACTED]")
        self.assertEqual(out.metadata["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(out.metadata["note"], "ok")
        self.assertEqual(out.metadata["nested"]["count"], 3)

    def test_dedup_key_is_idempotent(self):
        with mock.patch.dict(os.environ, _ON):
            first = record_event(account=self.a, category="HEALTH", event_type="deg",
                                 severity="WARNING", dedup_key="H:1:DEGRADED")
            second = record_event(account=self.a, category="HEALTH", event_type="deg",
                                  severity="WARNING", dedup_key="H:1:DEGRADED")
        self.assertEqual(first.id, second.id)
        self.assertEqual(OperationalEvent.objects.filter(dedup_key="H:1:DEGRADED").count(), 1)

    def test_empty_dedup_key_allows_many(self):
        with mock.patch.dict(os.environ, _ON):
            record_event(account=self.a, category="HEALTH", event_type="x")
            record_event(account=self.a, category="HEALTH", event_type="x")
        self.assertEqual(OperationalEvent.objects.filter(dedup_key="").count(), 2)

    def test_mark_resolved(self):
        e = _ev(self.a, severity="WARNING", resolved=False)
        with mock.patch.dict(os.environ, _ON):
            n = mark_resolved(account=self.a)
        self.assertEqual(n, 1)
        e.refresh_from_db()
        self.assertTrue(e.resolved)
        self.assertIsNotNone(e.resolved_at)

    def test_mark_resolved_dark_is_noop(self):
        _ev(self.a, severity="WARNING", resolved=False)
        with mock.patch.dict(os.environ, _OFF):
            self.assertEqual(mark_resolved(account=self.a), 0)

    def test_mark_resolved_requires_a_scope(self):
        _ev(self.a, severity="WARNING")
        with mock.patch.dict(os.environ, _ON):
            self.assertEqual(mark_resolved(), 0)  # no account and no dedup_key → refuse global resolve


class DedupConstraintTests(TestCase):
    """Event-duplication protection is enforced at the DB layer, not only by the recorder."""

    def setUp(self):
        self.a = _account(_user("dup"))

    def test_partial_unique_rejects_duplicate_nonempty_key(self):
        _ev(self.a, dedup_key="k")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _ev(self.a, dedup_key="k")

    def test_empty_key_is_exempt(self):
        _ev(self.a, dedup_key="")
        _ev(self.a, dedup_key="")
        self.assertEqual(OperationalEvent.objects.filter(dedup_key="").count(), 2)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Query service
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class QueryServiceTests(TestCase):
    def setUp(self):
        self.a = _account(_user("q"))
        # Insertion order oldest→newest; ordering is newest-first (-created_at, -id).
        self.e1 = _ev(self.a, event_type="e1", category="VALIDATION", severity="INFO")
        self.e2 = _ev(self.a, event_type="e2", category="HEALTH", severity="WARNING")
        self.e3 = _ev(self.a, event_type="e3", category="HEALTH", severity="CRITICAL")

    def test_timeline_orders_newest_first(self):
        ids = [e.id for e in Q.timeline(self.a)]
        self.assertEqual(ids, [self.e3.id, self.e2.id, self.e1.id])

    def test_pagination_limit_and_offset(self):
        page1 = Q.timeline(self.a, limit=2, offset=0)
        page2 = Q.timeline(self.a, limit=2, offset=2)
        self.assertEqual([e.id for e in page1], [self.e3.id, self.e2.id])
        self.assertEqual([e.id for e in page2], [self.e1.id])

    def test_limit_is_clamped(self):
        # A silly limit is clamped to MAX_TIMELINE_LIMIT (does not raise / does not return everything x N).
        out = Q.timeline(self.a, limit=10_000)
        self.assertEqual(len(out), 3)
        # Non-numeric / non-positive fall back to the default, not an error.
        self.assertEqual(len(Q.timeline(self.a, limit="abc")), 3)
        self.assertEqual(len(Q.timeline(self.a, limit=0)), 3)

    def test_category_filter(self):
        out = Q.timeline(self.a, category="HEALTH")
        self.assertEqual([e.id for e in out], [self.e3.id, self.e2.id])

    def test_latest_in_category_and_of_type(self):
        self.assertEqual(Q.latest_in_category(self.a, "HEALTH").id, self.e3.id)
        self.assertEqual(Q.latest_of_type(self.a, "e1").id, self.e1.id)
        self.assertIsNone(Q.latest_in_category(self.a, "RUNTIME"))

    def test_open_events_excludes_info_and_resolved(self):
        opens = Q.open_events(self.a)
        self.assertEqual({e.id for e in opens}, {self.e2.id, self.e3.id})  # not e1 (INFO)
        self.e3.resolved = True
        self.e3.save(update_fields=["resolved"])
        opens = Q.open_events(self.a)
        self.assertEqual({e.id for e in opens}, {self.e2.id})

    def test_empty_history(self):
        empty = _account(_user("q2"), name="B", number="2000002")
        self.assertEqual(Q.timeline(empty), [])
        self.assertEqual(Q.open_events(empty), [])
        self.assertIsNone(Q.latest_in_category(empty, "HEALTH"))


class VisibilityTests(TestCase):
    def setUp(self):
        self.a = _account(_user("vis"))
        self.pub = _ev(self.a, event_type="pub", customer_visible=True)
        self.op = _ev(self.a, event_type="op", customer_visible=False)

    def test_customer_visible_excludes_operator_only(self):
        ids = {e.id for e in Q.customer_visible(self.a)}
        self.assertEqual(ids, {self.pub.id})

    def test_operator_visible_includes_all(self):
        ids = {e.id for e in Q.operator_visible(self.a)}
        self.assertEqual(ids, {self.pub.id, self.op.id})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Summary service
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class SummaryTests(TestCase):
    def setUp(self):
        self.u = _user("sum")
        self.a = _account(self.u)

    def test_empty_history_deterministic_defaults(self):
        s = build_operational_summary(self.a)
        self.assertIsInstance(s, OperationalSummaryDTO)
        d = s.as_dict()
        self.assertEqual(d["account_id"], self.a.id)
        self.assertEqual(d["validation_state"]["status"], "NEVER")
        self.assertFalse(d["health_state"]["available"])          # health engine DARK
        self.assertEqual(d["health_state"]["state"], "UNKNOWN")
        self.assertFalse(d["runtime_pause"]["paused"])
        self.assertEqual(d["credential_status"]["state"], "missing")
        self.assertFalse(d["disconnect_state"]["disconnected"])
        self.assertEqual(d["event_counts"]["total"], 0)
        self.assertEqual(d["event_counts"]["open"], 0)
        self.assertIsNone(d["latest_validation"])
        self.assertIsNone(d["latest_error"])
        self.assertIsNone(d["latest_warning"])

    def test_counts_latest_error_warning_validation(self):
        _ev(self.a, category="VALIDATION", event_type="val", severity="INFO", reason_code="validated")
        _ev(self.a, category="HEALTH", event_type="deg", severity="WARNING", reason_code="degraded_auth")
        _ev(self.a, category="HEALTH", event_type="down", severity="CRITICAL",
            reason_code="broker_unreachable")
        d = build_operational_summary(self.a).as_dict()
        self.assertEqual(d["event_counts"]["total"], 3)
        self.assertEqual(d["event_counts"]["open"], 2)  # WARNING + CRITICAL unresolved; INFO excluded
        self.assertEqual(d["event_counts"]["by_severity"]["WARNING"], 1)
        self.assertEqual(d["event_counts"]["by_category"]["HEALTH"], 2)
        self.assertEqual(d["latest_error"]["reason_code"], "broker_unreachable")  # CRITICAL is an error-class
        self.assertEqual(d["latest_warning"]["reason_code"], "degraded_auth")
        self.assertEqual(d["latest_validation"]["reason_code"], "validated")

    def test_state_fields_reflect_account(self):
        self.a.validation_status = "VALIDATED"
        self.a.validated_at = timezone.now()
        self.a.password_enc = "cipher-text"
        self.a.disconnected_at = timezone.now()
        self.a.save()
        d = build_operational_summary(self.a).as_dict()
        self.assertEqual(d["validation_state"]["status"], "VALIDATED")
        self.assertEqual(d["credential_status"]["state"], "present")
        self.assertTrue(d["disconnect_state"]["disconnected"])
        self.assertIsNotNone(d["last_update"])

    def test_hybrid_folds_live_health_and_pause(self):
        with mock.patch("reliability.broker_health.get_contract",
                        return_value={"state": "DEGRADED", "eligible": False, "pause_required": True,
                                      "reason_code": "degraded_auth", "state_version": 4,
                                      "updated_at": "2026-08-04T00:00:00+00:00"}), \
             mock.patch("execution.runtime_pause.pause_state",
                        return_value={"paused": True, "reason_code": "broker_health_degraded"}), \
             mock.patch("execution.runtime_pause.is_broker_paused", return_value=True):
            d = build_operational_summary(self.a).as_dict()
        self.assertTrue(d["health_state"]["available"])
        self.assertEqual(d["health_state"]["state"], "DEGRADED")
        self.assertTrue(d["runtime_pause"]["paused"])
        self.assertTrue(d["runtime_pause"]["live_paused"])

    def test_health_read_failure_is_fail_open(self):
        with mock.patch("reliability.broker_health.get_contract", side_effect=RuntimeError("boom")):
            d = build_operational_summary(self.a).as_dict()  # must not raise
        self.assertFalse(d["health_state"]["available"])

    def test_multiple_accounts_are_isolated(self):
        other = _account(_user("sum2"), name="B", number="2000002")
        _ev(self.a, category="HEALTH", event_type="a-only", severity="WARNING")
        d_other = build_operational_summary(other).as_dict()
        self.assertEqual(d_other["event_counts"]["total"], 0)
        self.assertEqual(Q.timeline(other), [])

    def test_customer_only_summary_hides_operator_events(self):
        # A customer-visible WARNING and an operator-only (customer_visible=False) ERROR.
        _ev(self.a, category="HEALTH", event_type="warn", severity="WARNING",
            reason_code="degraded_auth", customer_visible=True)
        _ev(self.a, category="EXECUTION", event_type="op-err", severity="ERROR",
            reason_code="internal_gate_failure", summary="operator diagnostic", customer_visible=False)

        cust = build_operational_summary(self.a, customer_only=True).as_dict()
        self.assertIsNone(cust["latest_error"])                       # operator ERROR hidden
        self.assertEqual(cust["event_counts"]["total"], 1)            # only the WARNING counts
        self.assertNotIn("EXECUTION", cust["event_counts"]["by_category"])
        self.assertEqual(cust["latest_warning"]["reason_code"], "degraded_auth")

        op = build_operational_summary(self.a, customer_only=False).as_dict()
        self.assertEqual(op["latest_error"]["reason_code"], "internal_gate_failure")  # operator sees it
        self.assertEqual(op["event_counts"]["total"], 2)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# DTO immutability
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class DTOImmutabilityTests(TestCase):
    def setUp(self):
        self.a = _account(_user("dto"))

    def test_event_dto_is_frozen(self):
        dto = OperationalEventDTO.from_model(_ev(self.a, metadata={"k": 1}))
        with self.assertRaises(FrozenInstanceError):
            dto.severity = "CRITICAL"

    def test_as_dict_returns_a_copy(self):
        dto = OperationalEventDTO.from_model(_ev(self.a, metadata={"k": 1}))
        d = dto.as_dict()
        d["metadata"]["injected"] = True
        self.assertNotIn("injected", dto.metadata)  # mutating the returned dict must not touch the DTO

    def test_as_dict_nested_mutation_does_not_leak(self):
        # A shallow copy would share nested dicts by reference; assert the deep copy isolates them.
        dto = OperationalEventDTO.from_model(_ev(self.a, metadata={"nested": {"a": 1}}))
        d = dto.as_dict()
        d["metadata"]["nested"]["injected"] = True
        self.assertNotIn("injected", dto.metadata["nested"])

    def test_from_model_does_not_alias_orm_metadata(self):
        ev = _ev(self.a, metadata={"nested": {"a": 1}})
        dto = OperationalEventDTO.from_model(ev)
        dto.metadata["nested"]["mutated"] = True   # mutate the DTO's own copy
        self.assertNotIn("mutated", ev.metadata["nested"])  # source ORM instance untouched


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# API endpoint
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class ApiTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.owner = _user("owner")
        self.other = _user("other")
        self.staff = _user("staff", staff=True)
        self.a = _account(self.owner)
        self.pub = _ev(self.a, event_type="pub", customer_visible=True)
        # Operator-only ERROR: must be hidden from the non-staff owner in BOTH the timeline and the
        # summary aggregates (latest_error / counts), but visible to staff.
        self.op = _ev(self.a, event_type="op", category="EXECUTION", severity="ERROR",
                      reason_code="internal_gate_failure", customer_visible=False)

    def _get(self, user, query=""):
        req = self.factory.get(f"/api/operations/account-events/{query}")
        force_authenticate(req, user=user)
        return OperationalAccountEventsView.as_view()(req)

    def test_dark_returns_404(self):
        with mock.patch.dict(os.environ, _OFF):
            r = self._get(self.owner, f"?account_id={self.a.id}")
        self.assertEqual(r.status_code, 404)

    def test_missing_account_id_is_400(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.owner)
        self.assertEqual(r.status_code, 400)

    def test_owner_gets_summary_and_customer_timeline(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.owner, f"?account_id={self.a.id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("summary", r.data)
        self.assertIn("timeline", r.data)
        ids = {e["id"] for e in r.data["timeline"]}
        self.assertEqual(ids, {self.pub.id})  # operator-only event hidden from the owner (timeline)
        # ...and hidden from the summary aggregates too (the confirmed-fix regression).
        self.assertIsNone(r.data["summary"]["latest_error"])
        self.assertEqual(r.data["summary"]["event_counts"]["total"], 1)
        self.assertNotIn("EXECUTION", r.data["summary"]["event_counts"]["by_category"])

    def test_other_user_is_404_idor(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.other, f"?account_id={self.a.id}")
        self.assertEqual(r.status_code, 404)

    def test_staff_can_read_any_account_and_sees_operator_events(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.staff, f"?account_id={self.a.id}")
        self.assertEqual(r.status_code, 200)
        ids = {e["id"] for e in r.data["timeline"]}
        self.assertEqual(ids, {self.pub.id, self.op.id})  # staff/operator sees all
        self.assertEqual(r.data["summary"]["latest_error"]["reason_code"], "internal_gate_failure")
        self.assertEqual(r.data["summary"]["event_counts"]["total"], 2)

    def test_nonexistent_account_is_404(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.owner, "?account_id=99999")
        self.assertEqual(r.status_code, 404)

    def test_non_integer_account_id_is_404(self):
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.owner, "?account_id=abc")
        self.assertEqual(r.status_code, 404)

    def test_category_filter_and_pagination_params(self):
        _ev(self.a, event_type="h1", category="HEALTH", severity="WARNING", customer_visible=True)
        with mock.patch.dict(os.environ, _ON):
            r = self._get(self.owner, f"?account_id={self.a.id}&category=HEALTH&limit=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["timeline"]), 1)
        self.assertEqual(r.data["timeline"][0]["category"], "HEALTH")


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Severity mapping (unit)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
class SeverityMappingTests(TestCase):
    def test_normalize_severity_mapping(self):
        self.assertEqual(normalize_severity("WARN"), "WARNING")
        self.assertEqual(normalize_severity("warning"), "WARNING")
        self.assertEqual(normalize_severity("DEBUG"), "INFO")
        self.assertEqual(normalize_severity("info"), "INFO")
        self.assertEqual(normalize_severity("ERROR"), "ERROR")
        self.assertEqual(normalize_severity("critical"), "CRITICAL")
        self.assertEqual(normalize_severity("fatal"), "CRITICAL")
        self.assertEqual(normalize_severity("nonsense"), "INFO")
        self.assertEqual(normalize_severity(None), "INFO")
