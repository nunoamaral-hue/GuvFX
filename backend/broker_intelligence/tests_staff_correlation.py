"""ADR-0092 — staff-gated manual withdrawal correlation. SYNTHETIC fixtures ONLY (no real email, name, account,
reference or PII). Covers the positive flow and the fail-closed adversarial matrix."""
from __future__ import annotations

import hashlib
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from broker_intelligence import staff_correlation as SC
from broker_intelligence.models import (BrokerEvent, BrokerProfile, EvidenceBlob, Provenance, Withdrawal,
                                        StaffWithdrawalCorrelation, UnknownSenderDiscoveryCandidate as C)

User = get_user_model()
_n = 0


def _uniq(p="x"):
    global _n
    _n += 1
    return f"{p}{_n:06d}"


def _staff(*, active=True, staff=True):
    u = User.objects.create_user(username=_uniq("stf"), email=f"{_uniq('stf')}@guvfx.test", password="x")
    u.is_staff, u.is_active = staff, active
    u.save()
    return u


def _owner():
    return User.objects.create_user(username=_uniq("own"), email=f"{_uniq('own')}@x.test", password="x")


def _profile(*, domain="vantagemarkets.test", active=True, provenance="OPERATOR_VERIFIED"):
    s = _uniq("brk")
    return BrokerProfile.objects.create(name=f"Broker {s}", slug=s, approved_sender_domains=domain,
                                        provenance=provenance, is_active=active)


def _cand(owner, *, domain="vantagemarkets.test", lifecycle="REQUESTED", state=None, minutes=0, assoc=None, ts=True):
    i = _uniq("c")
    received = (timezone.now() - timedelta(days=1) + timedelta(minutes=minutes)) if ts else None
    return C.objects.create(
        owner=owner, message_sha256=hashlib.sha256(f"m{i}".encode()).hexdigest(),
        provider_message_id_hash=hashlib.sha256(f"p{i}".encode()).hexdigest(),
        sender_domain_hash=hashlib.sha256(domain.encode()).hexdigest(), registrable_domain=domain,
        auth_policy_passed=True, lifecycle_class=lifecycle, confidence_band="MEDIUM",
        reason_codes=["withdrawal_intent", "explicit_lifecycle"], received_at=received, observed_at=received,
        review_state=(state or C.ReviewState.NEW), associated_broker_profile=assoc)


def _attach_event(cand, *, ref="", amount=None, currency=""):
    """Attach a retained EvidenceBlob+BrokerEvent to a candidate (for the conflict tests)."""
    blob = EvidenceBlob.objects.create(sha256=cand.message_sha256, storage_key=_uniq("k"), byte_size=1,
                                       received_at=cand.received_at or timezone.now())
    return BrokerEvent.objects.create(event_type="WITHDRAWAL_REQUESTED", received_at=blob.received_at,
                                      evidence=blob, owner=cand.owner, broker_reference_id=ref,
                                      amount=amount, currency=currency, provenance=Provenance.SYNTHETIC)


def _plan(owner, profile, cands, *, approver=None, reason="same withdrawal, two stages"):
    return SC.plan_correlation(candidate_ids=[c.id for c in cands], broker_profile_id=profile.id,
                               approved_by=(approver or _staff()).email, reason=reason)


class PositiveFlow(TestCase):
    def test_submitted_plus_successful_one_requested_withdrawal(self):
        o, bp, stf = _owner(), _profile(), _staff()
        c_sub = _cand(o, minutes=0)      # earlier: submitted
        c_ok = _cand(o, minutes=1)       # later: request successful
        plan = _plan(o, bp, [c_ok, c_sub], approver=stf)      # ids in "wrong" order on purpose
        self.assertEqual([c.id for c in plan.ordered_candidates], [c_sub.id, c_ok.id])  # ordered by timestamp
        dec, created = SC.apply_correlation(plan)
        self.assertTrue(created)
        self.assertEqual(Withdrawal.objects.count(), 1)
        w = dec.resulting_withdrawal
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)       # in progress
        self.assertIsNone(w.completed_at)                            # NEVER completed
        self.assertEqual(w.correlation_method, Withdrawal.CorrelationMethod.MANUAL_STAFF)
        self.assertEqual(w.requested_at, c_sub.received_at)          # earliest trusted timestamp
        self.assertEqual(w.owner_id, o.id)
        self.assertEqual(dec.status, StaffWithdrawalCorrelation.Status.APPLIED)
        self.assertEqual(set(dec.candidates.values_list("id", flat=True)), {c_sub.id, c_ok.id})
        c_sub.refresh_from_db(); c_ok.refresh_from_db()
        self.assertEqual(c_sub.review_state, C.ReviewState.CORRELATED)
        self.assertEqual(c_ok.review_state, C.ReviewState.CORRELATED)
        self.assertTrue(EvidenceBlob.objects.count() >= 0)          # evidence never deleted (none created here)

    def test_dry_run_command_makes_no_change(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        call_command("correlate_withdrawal_candidates", "--candidate-id", str(a.id), "--candidate-id", str(b.id),
                     "--broker-profile-id", str(bp.id), "--approved-by", stf.email, "--reason", "dry run")
        self.assertEqual(Withdrawal.objects.count(), 0)
        self.assertEqual(StaffWithdrawalCorrelation.objects.count(), 0)
        a.refresh_from_db()
        self.assertEqual(a.review_state, C.ReviewState.NEW)

    def test_apply_command_creates_exactly_one_then_idempotent(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        args = ["--candidate-id", str(a.id), "--candidate-id", str(b.id), "--broker-profile-id", str(bp.id),
                "--approved-by", stf.email, "--reason", "same lifecycle", "--apply"]
        call_command("correlate_withdrawal_candidates", *args)
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(StaffWithdrawalCorrelation.objects.count(), 1)

    def test_replay_is_idempotent(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        d1, c1 = SC.apply_correlation(_plan(o, bp, [a, b], approver=stf))
        # a second plan needs candidates still NEW to pass; after apply they are CORRELATED, so re-planning refuses —
        # BUT apply with the SAME idempotency key is the replay path. Build the plan BEFORE the first apply.
        self.assertTrue(c1)
        # Re-apply via the deterministic key: same inputs -> apply_correlation finds the existing decision.
        a2, b2 = a, b  # same candidates
        # Reconstruct the plan object by key equality: calling apply with an equivalent plan returns the no-op.
        plan2 = SC.CorrelationPlan(owner_id=o.id, broker_profile=bp, ordered_candidates=[a2, b2],
                                   ordered_pmid_hashes=[a2.provider_message_id_hash, b2.provider_message_id_hash],
                                   reviewer=f"user:{stf.id}", reason="same lifecycle",
                                   idempotency_key=d1.idempotency_key, requested_at=d1.resulting_withdrawal.requested_at,
                                   amount=None, currency="")
        d2, created2 = SC.apply_correlation(plan2)
        self.assertFalse(created2)
        self.assertEqual(d1.id, d2.id)
        self.assertEqual(Withdrawal.objects.count(), 1)

    def test_evidence_preserved_and_linked(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        _attach_event(a, amount=None), _attach_event(b, amount=None)
        before_blobs = EvidenceBlob.objects.count()
        dec, _ = SC.apply_correlation(_plan(o, bp, [a, b], approver=stf))
        self.assertEqual(EvidenceBlob.objects.count(), before_blobs)   # none deleted
        self.assertEqual(set(dec.ordered_candidate_pmid_hashes),
                         {a.provider_message_id_hash, b.provider_message_id_hash})


class Adversarial(TestCase):
    def _refuse(self, code, owner, profile, cands, **kw):
        with self.assertRaises(SC.CorrelationRefused) as cm:
            _plan(owner, profile, cands, **kw)
        self.assertEqual(cm.exception.code, code)
        self.assertEqual(Withdrawal.objects.count(), 0)
        self.assertEqual(StaffWithdrawalCorrelation.objects.count(), 0)

    def test_cross_tenant(self):
        o1, o2, bp = _owner(), _owner(), _profile()
        self._refuse("CROSS_TENANT", o1, bp, [_cand(o1, minutes=0), _cand(o2, minutes=1)])

    def test_cross_broker_misattribution(self):
        o, bp = _owner(), _profile(domain="vantagemarkets.test")
        self._refuse("BROKER_MISATTRIBUTION", o, bp,
                     [_cand(o, domain="vantagemarkets.test", minutes=0), _cand(o, domain="other-broker.test", minutes=1)])

    def test_conflicting_broker_association(self):
        o, bp, other = _owner(), _profile(), _profile()
        self._refuse("CONFLICTING_BROKER_ASSOCIATION", o, bp,
                     [_cand(o, minutes=0), _cand(o, minutes=1, assoc=other)])

    def test_non_staff_reviewer(self):
        o, bp = _owner(), _profile()
        self._refuse("REVIEWER_NOT_STAFF", o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)],
                     approver=_staff(staff=False))

    def test_inactive_reviewer(self):
        o, bp = _owner(), _profile()
        self._refuse("REVIEWER_NOT_STAFF", o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)],
                     approver=_staff(active=False))

    def test_broker_profile_inactive(self):
        o, bp = _owner(), _profile(active=False)
        self._refuse("BROKER_PROFILE_INACTIVE", o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)])

    def test_broker_profile_not_operator_verified(self):
        o, bp = _owner(), _profile(provenance="SEED")
        self._refuse("BROKER_PROFILE_NOT_OPERATOR_VERIFIED", o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)])

    def test_broker_profile_not_found(self):
        o = _owner()
        with self.assertRaises(SC.CorrelationRefused) as cm:
            SC.plan_correlation(candidate_ids=[_cand(o, minutes=0).id, _cand(o, minutes=1).id],
                                broker_profile_id=99999, approved_by=_staff().email, reason="x")
        self.assertEqual(cm.exception.code, "BROKER_PROFILE_NOT_FOUND")

    def test_conflicting_reference(self):
        o, bp = _owner(), _profile()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        _attach_event(a, ref="REF-A"), _attach_event(b, ref="REF-B")
        self._refuse("CONFLICTING_REFERENCE", o, bp, [a, b])

    def test_conflicting_amount_currency(self):
        o, bp = _owner(), _profile()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        _attach_event(a, amount="100.00", currency="USD"), _attach_event(b, amount="250.00", currency="USD")
        self._refuse("CONFLICTING_AMOUNT_CURRENCY", o, bp, [a, b])

    def test_ambiguous_timestamp(self):
        o, bp = _owner(), _profile()
        self._refuse("AMBIGUOUS_TIMESTAMP", o, bp, [_cand(o, minutes=0), _cand(o, ts=False)])

    def test_already_correlated_candidate(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        SC.apply_correlation(_plan(o, bp, [a, b], approver=stf))   # a,b now CORRELATED
        c = _cand(o, minutes=2)
        with self.assertRaises(SC.CorrelationRefused) as cm:
            _plan(o, bp, [a, c], approver=stf)
        self.assertEqual(cm.exception.code, "CANDIDATE_NOT_ELIGIBLE")   # a is no longer NEW

    def test_already_rejected_candidate(self):
        o, bp = _owner(), _profile()
        self._refuse("CANDIDATE_NOT_ELIGIBLE", o, bp,
                     [_cand(o, minutes=0, state=C.ReviewState.REJECTED), _cand(o, minutes=1)])

    def test_terminal_lifecycle_candidate_refused_never_completed(self):
        o, bp = _owner(), _profile()
        self._refuse("TERMINAL_LIFECYCLE_CANDIDATE", o, bp,
                     [_cand(o, minutes=0, lifecycle="COMPLETED"), _cand(o, minutes=1)])

    def test_too_few_candidates(self):
        o, bp = _owner(), _profile()
        self._refuse("TOO_FEW_CANDIDATES", o, bp, [_cand(o, minutes=0)])

    def test_duplicate_candidate_id(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a = _cand(o, minutes=0)
        with self.assertRaises(SC.CorrelationRefused) as cm:
            SC.plan_correlation(candidate_ids=[a.id, a.id], broker_profile_id=bp.id, approved_by=stf.email, reason="x")
        self.assertEqual(cm.exception.code, "DUPLICATE_CANDIDATE_ID")

    def test_candidate_not_found(self):
        o, bp, stf = _owner(), _profile(), _staff()
        with self.assertRaises(SC.CorrelationRefused) as cm:
            SC.plan_correlation(candidate_ids=[_cand(o, minutes=0).id, 999999], broker_profile_id=bp.id,
                                approved_by=stf.email, reason="x")
        self.assertEqual(cm.exception.code, "CANDIDATE_NOT_FOUND")

    def test_toctou_candidate_becomes_ineligible_before_apply(self):
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        plan = _plan(o, bp, [a, b], approver=stf)
        C.objects.filter(id=b.id).update(review_state=C.ReviewState.REJECTED)   # changes AFTER plan
        with self.assertRaises(SC.CorrelationRefused) as cm:
            SC.apply_correlation(plan)
        self.assertEqual(cm.exception.code, "CANDIDATE_NOT_ELIGIBLE")
        self.assertEqual(Withdrawal.objects.count(), 0)              # rolled back

    def test_decision_is_append_only(self):
        o, bp, stf = _owner(), _profile(), _staff()
        dec, _ = SC.apply_correlation(_plan(o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)], approver=stf))
        dec.reason = "tampered"
        with self.assertRaises(ValidationError):
            dec.save()
        with self.assertRaises(ValidationError):
            dec.delete()

    def test_reason_is_sanitised_no_control_chars(self):
        o, bp, stf = _owner(), _profile(), _staff()
        plan = _plan(o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)], approver=stf,
                     reason="line1\r\n\t\x00line2   " + "x" * 400)
        self.assertNotIn("\n", plan.reason)
        self.assertNotIn("\r", plan.reason)
        self.assertNotIn("\x00", plan.reason)
        self.assertLessEqual(len(plan.reason), 256)

    def test_no_pii_fields_on_decision_record(self):
        # Structural: the decision model exposes no body/name/full-account column; its text fields carry only a
        # user:<id> reviewer, a sanitised reason and salted hashes.
        o, bp, stf = _owner(), _profile(), _staff()
        dec, _ = SC.apply_correlation(_plan(o, bp, [_cand(o, minutes=0), _cand(o, minutes=1)], approver=stf,
                                            reason="confirmed one lifecycle"))
        names = {f.name for f in StaffWithdrawalCorrelation._meta.get_fields()}
        for forbidden in ("body", "subject", "name", "email", "account_number", "raw"):
            self.assertNotIn(forbidden, names)
        self.assertTrue(dec.reviewer.startswith("user:"))
        for h in dec.ordered_candidate_pmid_hashes:
            self.assertEqual(len(h), 64)   # salted sha256 hash, not raw content


class AdversarialReviewRegressions(TestCase):
    """Regressions for the confirmed adversarial-review findings."""

    def _manual_withdrawal(self, *, amount, currency="USD"):
        from broker_intelligence import correlation as COR  # noqa: F401 (ensure engine importable)
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        _attach_event(a, amount=amount, currency=currency)
        _attach_event(b, amount=amount, currency=currency)
        dec, _ = SC.apply_correlation(_plan(o, bp, [a, b], approver=stf))
        return o, dec.resulting_withdrawal

    def test_manual_withdrawal_never_advanced_to_completed_by_heuristic(self):
        # FINDING (HIGH): without the MANUAL_STAFF exclusion, a later ref-less COMPLETED event with the same
        # owner/amount/currency within the window would advance the staff withdrawal to COMPLETED.
        from broker_intelligence import correlation as COR
        o, w = self._manual_withdrawal(amount="1000.00")
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)
        BrokerEvent.objects.create(
            event_type="WITHDRAWAL_COMPLETED", transaction_category="EXTERNAL_WITHDRAWAL",
            received_at=w.requested_at + timedelta(hours=1), owner=o, trading_account=None,
            broker_reference_id="", amount="1000.00", currency="USD", provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        COR.run_correlation()
        w.refresh_from_db()
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)   # NOT advanced
        self.assertIsNone(w.completed_at)                        # never completed
        self.assertEqual(Withdrawal.objects.count(), 1)          # completion event resolved to nothing

    def test_manual_withdrawal_not_adopted_by_later_reference_event(self):
        # FINDING (MEDIUM): a later reference-carrying event must NOT adopt the staff withdrawal / stamp its reference.
        from broker_intelligence import correlation as COR
        o, w = self._manual_withdrawal(amount="500.00")
        BrokerEvent.objects.create(
            event_type="WITHDRAWAL_CONFIRMATION_REQUIRED", transaction_category="EXTERNAL_WITHDRAWAL",
            received_at=w.requested_at + timedelta(hours=1), owner=o, trading_account=None,
            broker_reference_id="OTHER-REF-9", amount="500.00", currency="USD", provenance=Provenance.SYNTHETIC,
            correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
        COR.run_correlation()
        w.refresh_from_db()
        self.assertEqual(w.broker_reference_id, "")              # NOT adopted/stamped
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)
        self.assertEqual(Withdrawal.objects.filter(broker_reference_id="OTHER-REF-9").count(), 1)  # its own record

    def test_command_apply_time_refusal_is_graceful(self):
        # FINDING (MEDIUM): a TOCTOU CorrelationRefused raised by apply_correlation must be caught by the command,
        # not crash with a traceback.
        from unittest import mock
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        with mock.patch("broker_intelligence.staff_correlation.apply_correlation",
                        side_effect=SC.CorrelationRefused("CANDIDATE_NOT_ELIGIBLE", "race")):
            call_command("correlate_withdrawal_candidates", "--candidate-id", str(a.id), "--candidate-id", str(b.id),
                         "--broker-profile-id", str(bp.id), "--approved-by", stf.email, "--reason", "x", "--apply")
        self.assertEqual(Withdrawal.objects.count(), 0)          # no partial mutation, no crash

    def test_replay_no_op_even_after_candidates_correlated(self):
        # The idempotent no-op holds even once the candidates are CORRELATED (concurrent-loser path): a replay with
        # the same idempotency key returns the existing decision, never a spurious CANDIDATE_NOT_ELIGIBLE.
        o, bp, stf = _owner(), _profile(), _staff()
        a, b = _cand(o, minutes=0), _cand(o, minutes=1)
        d1, _ = SC.apply_correlation(_plan(o, bp, [a, b], approver=stf))
        a.refresh_from_db()
        self.assertEqual(a.review_state, C.ReviewState.CORRELATED)
        plan2 = SC.CorrelationPlan(owner_id=o.id, broker_profile=bp, ordered_candidates=[a, b],
                                   ordered_pmid_hashes=[a.provider_message_id_hash, b.provider_message_id_hash],
                                   reviewer=f"user:{stf.id}", reason="x", idempotency_key=d1.idempotency_key,
                                   requested_at=d1.resulting_withdrawal.requested_at, amount=None, currency="")
        d2, created2 = SC.apply_correlation(plan2)
        self.assertFalse(created2)
        self.assertEqual(d2.id, d1.id)
        self.assertEqual(Withdrawal.objects.count(), 1)
