"""ADR-0088 — integration + API tests for generic withdrawal detection (DB-backed). Covers the ingestion hook
gating (DARK no-op; canary accept/review/quarantine/not-withdrawal), specific-parser precedence, idempotency/
replay, PII-safety of the candidate, owner-scoping, and the review API (owner-scoped read + staff-gated audited
action). SYNTHETIC fixtures only — no real PII."""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from broker_intelligence import generic_detector as gdx
from broker_intelligence import ingestion, parsers
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEvent, GenericWithdrawalPattern, Provenance, ReviewCandidateAction,
                                        Withdrawal, WithdrawalReviewCandidate)
from broker_intelligence.views import GenericReviewCandidateActionView, GenericReviewCandidateListView

User = get_user_model()
_CANARY = "canary@guvfx.test,user-a@guvfx.test,user-b@guvfx.test,staff@guvfx.test"


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="acme-fx.com", BROKER_GENERIC_DETECTION_OWNER_ALLOWLIST=_CANARY)
class _Base(TestCase):
    def setUp(self):
        super().setUp()
        self._root = tempfile.mkdtemp(prefix="bi-gen-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        self.user = User.objects.create_user(username="canary", email="canary@guvfx.test", password="x")
        self.pattern = GenericWithdrawalPattern.objects.create(
            name="acme", version=1, broker_display_name="Acme FX", authenticated_sender_domains=["acme-fx.com"],
            subject_indicators=["withdrawal"], body_indicators=[], enabled=True, confidence=Decimal("0.900"),
            provenance=Provenance.SYNTHETIC)

    def tearDown(self):
        parsers.clear_registry()
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)
        super().tearDown()

    def _mm(self, subject, body, *, frm="no-reply@acme-fx.com", auth="pass", html="", raw=None):
        return MailMessage(
            raw_bytes=raw if raw is not None else ("%s|%s|%s" % (frm, subject, body)).encode(),
            to_addresses=(), from_address=frm, subject=subject, body=body, html_body=html,
            received_at=timezone.now(), provider_message_id="pm-%d" % (abs(hash((subject, body))) % 100000),
            auth_verdict=auth)

    def _ingest(self, mm, *, owner=None, provenance=Provenance.REAL):
        return ingestion.ingest_message(mm, store=self.store, provenance=provenance,
                                        owner_user=owner or self.user, connected_mailbox=None)


class IngestionHook(_Base):
    def test_generic_high_confidence_creates_event_and_candidate(self):
        ev = self._ingest(self._mm("Withdrawal completed", "GBP 42.00 sent to account ending 7766"))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.parser_name, "generic")
        self.assertEqual(ev.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(ev.transaction_category, "UNKNOWN")       # generic never asserts EXTERNAL without certification
        self.assertEqual(ev.owner_id, self.user.id)
        self.assertIsNone(ev.trading_account_id)                   # owner-attributed, account-UNRESOLVED
        c = WithdrawalReviewCandidate.objects.get()
        self.assertEqual(c.disposition, WithdrawalReviewCandidate.Disposition.GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(c.created_event_id, ev.id)
        self.assertEqual(c.masked_account_suffix, "••••" + "7766")
        self.assertEqual(Withdrawal.objects.count(), 0)            # a candidate/event is NOT a Withdrawal

    def test_review_required_when_no_enabled_pattern(self):
        self.pattern.enabled = False
        self.pattern.save(update_fields=["enabled"])
        ev = self._ingest(self._mm("Withdrawal completed", "your withdrawal has been completed"))
        self.assertIsNone(ev)                                      # no event
        c = WithdrawalReviewCandidate.objects.get()
        self.assertEqual(c.disposition, WithdrawalReviewCandidate.Disposition.REVIEW_REQUIRED)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_quarantined_on_contradictory_lifecycle(self):
        ev = self._ingest(self._mm("Withdrawal", "your withdrawal has been completed and has been cancelled"))
        self.assertIsNone(ev)
        self.assertEqual(WithdrawalReviewCandidate.objects.get().disposition,
                         WithdrawalReviewCandidate.Disposition.QUARANTINED)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_not_withdrawal_is_noop(self):
        ev = self._ingest(self._mm("Deposit received", "your deposit completed; webinar promotion"))
        self.assertIsNone(ev)
        self.assertEqual(WithdrawalReviewCandidate.objects.count(), 0)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    @override_settings(BROKER_GENERIC_DETECTION_OWNER_ALLOWLIST="")   # canary OFF for this owner
    def test_disabled_canary_is_byte_identical_noop(self):
        ev = self._ingest(self._mm("Withdrawal completed", "GBP 42.00 sent to account ending 7766"))
        self.assertIsNone(ev)                                      # unparseable-as-today
        self.assertEqual(WithdrawalReviewCandidate.objects.count(), 0)   # detector never invoked, no table touched
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_specific_parser_precedence_bypasses_generic(self):
        class _Fake:
            name, version = "fake", "v1"
            def can_parse(self, *, subject, body, from_address):
                return "withdrawal" in (subject or "").lower()
            def parse(self, *, subject, body, from_address):
                return parsers.ParsedBrokerEvent(event_type="WITHDRAWAL_REQUESTED", broker="Acme",
                                                 confidence=Decimal("0.9"))
        parsers.register_parser(_Fake())
        self.addCleanup(parsers.clear_registry)
        ev = self._ingest(self._mm("Withdrawal completed", "GBP 42.00 sent to account ending 7766"))
        self.assertIsNotNone(ev)
        self.assertEqual(ev.parser_name, "fake")                  # specific adapter won
        self.assertEqual(ev.event_type, "WITHDRAWAL_REQUESTED")
        self.assertEqual(WithdrawalReviewCandidate.objects.count(), 0)   # generic never ran

    def test_idempotent_replay_one_event_one_candidate(self):
        mm = self._mm("Withdrawal completed", "GBP 42.00 sent to account ending 7766", raw=b"IDEMPOTENT-RAW-1")
        e1 = self._ingest(mm)
        e2 = self._ingest(self._mm("Withdrawal completed", "x", raw=b"IDEMPOTENT-RAW-1"))   # same bytes -> same sha
        self.assertEqual(e1.id, e2.id)
        self.assertEqual(BrokerEvent.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(WithdrawalReviewCandidate.objects.filter(owner=self.user).count(), 1)

    def test_candidate_is_pii_free(self):
        # body carries a NAME and a FULL account number — neither may survive into the redacted candidate.
        ev = self._ingest(self._mm("Withdrawal completed",
                                   "Dear John Smith, your withdrawal has been completed. Account 99887766, GBP 42.00."))
        c = WithdrawalReviewCandidate.objects.get()
        blob = " ".join(str(x) for x in (
            c.candidate_broker_name, c.authenticated_sender_domain, c.masked_account_suffix, c.currency,
            c.matched_pattern, c.correlation_result, c.proposed_state, c.disposition, " ".join(c.reason_codes or [])))
        self.assertNotIn("John", blob)
        self.assertNotIn("Smith", blob)
        self.assertNotIn("99887766", blob)            # full account never stored
        self.assertIn("7766", c.masked_account_suffix)  # only the masked last-4
        self.assertEqual(c.candidate_broker_name, "Acme FX")   # from the matched pattern config, never the body

    def test_owner_scoped_no_cross_tenant(self):
        ua = User.objects.create_user(username="ua", email="user-a@guvfx.test", password="x")
        ub = User.objects.create_user(username="ub", email="user-b@guvfx.test", password="x")
        self._ingest(self._mm("Withdrawal completed", "GBP 42.00", raw=b"OWNER-A"), owner=ua)
        self._ingest(self._mm("Withdrawal completed", "USD 20.00", raw=b"OWNER-B"), owner=ub)
        self.assertEqual(WithdrawalReviewCandidate.objects.filter(owner=ua).count(), 1)
        self.assertEqual(WithdrawalReviewCandidate.objects.filter(owner=ub).count(), 1)
        self.assertNotEqual(WithdrawalReviewCandidate.objects.filter(owner=ua).first().id,
                            WithdrawalReviewCandidate.objects.filter(owner=ub).first().id)


class ReviewApi(_Base):
    def _get_list(self, user):
        req = APIRequestFactory().get("/api/broker-intelligence/review-candidates/")
        force_authenticate(req, user=user)
        resp = GenericReviewCandidateListView.as_view()(req)
        resp.render()
        import json
        return resp.status_code, json.loads(resp.content or b"null")

    def _post_action(self, user, cid, data):
        req = APIRequestFactory().post("/x", data, format="json")
        force_authenticate(req, user=user)
        resp = GenericReviewCandidateActionView.as_view()(req, candidate_id=cid)
        resp.render()
        import json
        return resp.status_code, json.loads(resp.content or b"null")

    def test_list_owner_scoped_and_canary_gated(self):
        self._ingest(self._mm("Withdrawal completed", "GBP 42.00 ending 7766"))
        sc, body = self._get_list(self.user)
        self.assertEqual(sc, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["candidates"][0]["disposition"], "GENERIC_HIGH_CONFIDENCE")
        # a non-canary member 404s
        outsider = User.objects.create_user(username="out", email="outsider@nope.test", password="x")
        self.assertEqual(self._get_list(outsider)[0], 404)

    def test_action_requires_staff_and_audits(self):
        self.pattern.enabled = False
        self.pattern.save(update_fields=["enabled"])
        self._ingest(self._mm("Withdrawal completed", "your withdrawal has been completed"))
        cand = WithdrawalReviewCandidate.objects.get()
        # non-staff -> 403
        sc, _ = self._post_action(self.user, cand.id, {"action": "APPROVED", "reason": "ok"})
        self.assertEqual(sc, 403)
        # staff -> audited
        staff = User.objects.create_user(username="st", email="staff@guvfx.test", password="x")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        sc, body = self._post_action(staff, cand.id, {"action": "REJECTED", "reason": "not a withdrawal"})
        self.assertEqual(sc, 200)
        self.assertTrue(body["audited"])
        cand.refresh_from_db()
        self.assertEqual(cand.operator_action, "REJECTED")
        act = ReviewCandidateAction.objects.get(candidate=cand)
        self.assertEqual(act.action, "REJECTED")
        self.assertEqual(act.actor, "user:%d" % staff.id)
        self.assertEqual(act.evidence_sha256, cand.evidence_sha256)   # evidence hash preserved

    def test_action_rejects_invalid_action(self):
        self.pattern.enabled = False
        self.pattern.save(update_fields=["enabled"])
        self._ingest(self._mm("Withdrawal completed", "your withdrawal has been completed"))
        cand = WithdrawalReviewCandidate.objects.get()
        staff = User.objects.create_user(username="st2", email="staff@guvfx.test", password="x")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        sc, _ = self._post_action(staff, cand.id, {"action": "DELETE_EVERYTHING"})
        self.assertEqual(sc, 400)

    @override_settings(BROKER_GENERIC_DETECTION_OWNER_ALLOWLIST="", BROKER_GENERIC_DETECTION_ENABLED="")
    def test_action_404_when_feature_fully_dark(self):
        staff = User.objects.create_user(username="st3", email="staff@guvfx.test", password="x")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self.assertEqual(self._post_action(staff, 1, {"action": "APPROVED"})[0], 404)
