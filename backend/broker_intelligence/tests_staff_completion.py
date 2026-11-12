"""ADR-0093 — staff-gated completion of a MANUAL_STAFF withdrawal. SYNTHETIC fixtures ONLY (no real email, name,
account, reference or PII). The completion email reproduces only the parser-relevant structure (a perfective
"...has been processed successfully" statement, no reference, an "account <digits>" login) with a synthetic login
ending 5017. Covers the positive completion and the fail-closed adversarial matrix.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import parsers, staff_correlation as SC
from broker_intelligence import staff_completion as SCMP
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import parse_rfc822
from broker_intelligence.models import (BrokerProfile, Withdrawal, StaffWithdrawalCorrelation,
                                        UnknownSenderDiscoveryCandidate as C)
from broker_intelligence.parsers_tradersway import register_default_parsers
from trading.models import TradingAccount

User = get_user_model()
_n = 0

_AR_VT = ("mx.google.com; dkim=pass header.d=vantagemarkets.com; spf=pass smtp.mailfrom=vantagemarkets.com; "
          "dmarc=pass header.from=vantagemarkets.com")


def _uniq(p="x"):
    global _n
    _n += 1
    return f"{p}{_n:06d}"


def _completion_raw(login="70005017", amount="30.00", date="Tue, 10 Nov 2026 06:39:17 +0000"):
    body = ("Your withdrawal request was successful.\r\n"
            f"Your request has been processed successfully for account {login}.\r\n"
            f"Amount: {amount}\r\n")
    return (f"From: no-reply@vantagemarkets.com\r\nTo: owner@x.invalid\r\n"
            f"Subject: Withdrawal Request Successful\r\nAuthentication-Results: {_AR_VT}\r\nDate: {date}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode("utf-8")


def _submission_raw(login="70005017", amount="30.00", date="Tue, 10 Nov 2026 06:38:58 +0000"):
    body = (f"Your withdrawal has been submitted for account {login}.\r\nAmount: {amount}\r\n")
    return (f"From: no-reply@vantagemarkets.com\r\nTo: owner@x.invalid\r\n"
            f"Subject: Withdrawal Submitted\r\nAuthentication-Results: {_AR_VT}\r\nDate: {date}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode("utf-8")


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="vantagemarkets.com",
                   BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class StaffCompletionBase(TestCase):
    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-comp-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.addCleanup(self._ctx.disable)
        self.addCleanup(lambda: shutil.rmtree(self._root, ignore_errors=True))
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()
        self.addCleanup(parsers.clear_registry)

        self.owner = User.objects.create_user(username=_uniq("own"), email="owner@x.invalid", password="x")
        self.staff = User.objects.create_user(username=_uniq("stf"), email="staff@guvfx.test", password="x")
        self.staff.is_staff = True
        self.staff.save(update_fields=["is_staff"])

        bp = BrokerProfile.objects.filter(slug="vantage").first()
        if bp is None:
            bp = BrokerProfile.objects.create(name="Vantage", slug="vantage",
                                              approved_sender_domains="vantagemarkets.com",
                                              provenance="OPERATOR_VERIFIED", is_active=True)
        else:
            bp.name, bp.approved_sender_domains, bp.provenance, bp.is_active = (
                "Vantage", "vantagemarkets.com", "OPERATOR_VERIFIED", True)
            bp.save()
        self.profile = bp

        self.acct = TradingAccount.objects.create(user=self.owner, name="Vantage NR", broker_name="Vantage NR",
                                                  account_number="70005017", is_demo=False, is_active=True)

        comp_raw = _completion_raw()
        self.comp_blob, _ = self.store.put(comp_raw, content_type="message/rfc822", source="EMAIL",
                                           received_at=parse_rfc822(comp_raw)["received_at"])
        sub_raw = _submission_raw()
        self.sub_blob, _ = self.store.put(sub_raw, content_type="message/rfc822", source="EMAIL",
                                          received_at=parse_rfc822(sub_raw)["received_at"])

        t0 = timezone.now() - timedelta(hours=2)
        self.c_sub = self._cand(self.sub_blob.sha256, received=t0)
        self.c_comp = self._cand(self.comp_blob.sha256, received=t0 + timedelta(seconds=60))
        plan = SC.plan_correlation(candidate_ids=[self.c_sub.id, self.c_comp.id],
                                   broker_profile_id=self.profile.id, approved_by=self.staff.email,
                                   reason="two stages, one withdrawal")
        dec, _ = SC.apply_correlation(plan)
        self.dec = dec
        self.wd = dec.resulting_withdrawal
        self.wd.refresh_from_db()

    def _cand(self, sha, *, received, domain="vantagemarkets.com"):
        i = _uniq("c")
        return C.objects.create(
            owner=self.owner, message_sha256=sha,
            provider_message_id_hash=hashlib.sha256(f"p{i}".encode()).hexdigest(),
            sender_domain_hash=hashlib.sha256(domain.encode()).hexdigest(), registrable_domain=domain,
            auth_policy_passed=True, lifecycle_class="REQUESTED", confidence_band="MEDIUM",
            reason_codes=["withdrawal_intent"], received_at=received, observed_at=received,
            review_state=C.ReviewState.NEW)

    def _plan(self, **over):
        kw = dict(withdrawal_id=self.wd.id, owner=self.owner, broker_profile_id=self.profile.id,
                  completion_candidate_id=self.c_comp.id, expected_account_suffix="5017",
                  expected_amount="30", approved_by=self.staff.email)
        kw.update(over)
        return SCMP.plan_staff_completion(**kw)

    def _cmd(self, **over):
        kw = dict(owner="owner@x.invalid", withdrawal_id=self.wd.id, broker_profile_id=self.profile.id,
                  completion_candidate_id=self.c_comp.id, expected_account_suffix="5017",
                  expected_amount="30", approved_by="staff@guvfx.test")
        kw.update(over)
        call_command("complete_staff_withdrawal", **kw)


class PositiveFlow(StaffCompletionBase):
    def test_setup_is_a_requested_manual_staff_withdrawal_unresolved(self):
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)
        self.assertEqual(self.wd.correlation_method, Withdrawal.CorrelationMethod.MANUAL_STAFF)
        self.assertIsNone(self.wd.trading_account_id)
        self.assertIsNone(self.wd.amount)
        self.assertEqual(self.wd.currency, "")

    def test_dry_run_makes_no_changes(self):
        self._cmd()   # apply defaults False
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)
        self.assertIsNone(self.wd.trading_account_id)
        self.assertIsNone(self.wd.completed_at)

    def test_apply_completes_with_truthful_anchors(self):
        plan = self._plan()
        wd, changed = SCMP.apply_staff_completion(plan)
        self.assertTrue(changed)
        wd.refresh_from_db()
        self.assertEqual(wd.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(wd.trading_account_id, self.acct.id)
        self.assertEqual(wd.amount, Decimal("30.00"))
        self.assertEqual(wd.currency, "")                               # currency never invented
        self.assertEqual(wd.requested_at, self.c_sub.received_at)       # requested anchor preserved
        self.assertEqual(wd.completed_at, self.c_comp.received_at)      # completion = completion evidence timestamp
        self.assertEqual(wd.duration_seconds(), 60)                     # from the two evidence timestamps
        self.assertIsNone(wd.completed_event_id)                        # candidate-based evidence model (no new event)
        self.assertEqual(wd.correlation_method, Withdrawal.CorrelationMethod.MANUAL_STAFF)

    def test_apply_is_idempotent(self):
        SCMP.apply_staff_completion(self._plan())
        # a re-plan on the now-completed withdrawal returns an already_completed no-op plan
        plan2 = self._plan()
        self.assertTrue(plan2.already_completed)
        wd, changed = SCMP.apply_staff_completion(plan2)
        self.assertFalse(changed)
        self.assertEqual(Withdrawal.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(wd.status, Withdrawal.Status.COMPLETED)

    def test_command_apply_then_dry_run_is_noop(self):
        self._cmd(apply=True)
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.COMPLETED)
        self.assertEqual(self.wd.trading_account_id, self.acct.id)
        self._cmd()   # idempotent no-op path, no exception
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.COMPLETED)


class FailClosedMatrix(StaffCompletionBase):
    def _assert_refused(self, code, **over):
        with self.assertRaises(SCMP.CompletionRefused) as ctx:
            self._plan(**over)
        self.assertEqual(ctx.exception.code, code)
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)   # no mutation on any refusal

    def test_non_staff_reviewer_refused(self):
        self._assert_refused("REVIEWER_NOT_STAFF", approved_by=self.owner.email)

    def test_empty_suffix_refused(self):
        self._assert_refused("EXPECTED_SUFFIX_REQUIRED", expected_account_suffix="")

    def test_wrong_owner_refused(self):
        other = User.objects.create_user(username=_uniq("oth"), email="other@x.invalid", password="x")
        self._assert_refused("OWNER_MISMATCH", owner=other)

    def test_amount_mismatch_refused(self):
        self._assert_refused("AMOUNT_MISMATCH", expected_amount="31")

    def test_suffix_mismatch_refused(self):
        self._assert_refused("ACCOUNT_SUFFIX_MISMATCH", expected_account_suffix="9999")

    def test_candidate_not_linked_refused(self):
        stray = self._cand(self.comp_blob.sha256, received=timezone.now())
        self._assert_refused("CANDIDATE_NOT_LINKED", completion_candidate_id=stray.id)

    def test_broker_profile_mismatch_refused(self):
        other_bp = BrokerProfile.objects.create(name="Other", slug=_uniq("slug"),
                                                approved_sender_domains="other.example",
                                                provenance="OPERATOR_VERIFIED", is_active=True)
        self._assert_refused("BROKER_PROFILE_MISMATCH", broker_profile_id=other_bp.id)

    def test_submission_candidate_as_completion_refused(self):
        # Passing the SUBMISSION candidate (re-parses to WITHDRAWAL_REQUESTED) must fail closed.
        self._assert_refused("EVIDENCE_NOT_A_COMPLETION", completion_candidate_id=self.c_sub.id)

    def test_account_unresolved_when_owner_has_no_matching_account_refused(self):
        self.acct.account_number = "70009999"   # owner no longer has the stated login
        self.acct.save(update_fields=["account_number"])
        self._assert_refused("ACCOUNT_UNRESOLVED")

    def test_cross_owner_account_is_never_used(self):
        # Another owner owns the 70005017 account; owner-scoped resolution must NOT bind it.
        other = User.objects.create_user(username=_uniq("oth"), email="o2@x.invalid", password="x")
        TradingAccount.objects.create(user=other, name="Vantage NR", broker_name="Vantage NR",
                                      account_number="70005017", is_demo=False, is_active=True)
        self.acct.account_number = "70001111"    # OUR owner has a different login
        self.acct.save(update_fields=["account_number"])
        self._assert_refused("ACCOUNT_UNRESOLVED")

    def test_already_resolved_account_refused(self):
        self.wd.trading_account = self.acct
        self.wd.save(update_fields=["trading_account"])
        self._assert_refused("ACCOUNT_ALREADY_RESOLVED")

    def test_rejected_withdrawal_refused(self):
        self.wd.status = Withdrawal.Status.REJECTED
        self.wd.save(update_fields=["status"])
        with self.assertRaises(SCMP.CompletionRefused) as ctx:
            self._plan()
        self.assertEqual(ctx.exception.code, "WITHDRAWAL_NOT_REQUESTED")

    def test_completion_before_request_refused(self):
        self.wd.requested_at = self.c_comp.received_at + timedelta(minutes=5)
        self.wd.save(update_fields=["requested_at"])
        self._assert_refused("COMPLETION_BEFORE_REQUEST")

    def test_command_refusal_raises_command_error(self):
        with self.assertRaises(CommandError):
            self._cmd(expected_amount="31", apply=True)
        self.wd.refresh_from_db()
        self.assertEqual(self.wd.status, Withdrawal.Status.REQUESTED)


class ProjectionContract(StaffCompletionBase):
    def test_completed_row_renders_truthfully_and_currency_safe(self):
        from broker_intelligence.metrics import withdrawal_history, withdrawal_metrics
        SCMP.apply_staff_completion(self._plan())
        H = withdrawal_history([self.acct], owner=self.owner)
        row = next(r for r in H["withdrawals"] if r["id"] == self.wd.id)
        self.assertEqual(row["state"], "completed")
        self.assertFalse(row["account_unresolved"])           # resolved — no "Account unresolved" warning
        self.assertEqual(row["account_masked"], "••••5017")   # masked suffix, never the full login
        self.assertEqual(row["trading_account_id"], self.acct.id)
        self.assertEqual(row["amount"], "30.00")
        self.assertEqual(row["currency"], "")                 # unknown — never invented
        self.assertIn("Vantage", row["broker"])               # resolved broker label
        self.assertEqual(row["broker_reference_id"], "")      # no fabricated reference
        self.assertFalse(row["reference_correlated"])
        # currency-safe: the known amount with UNKNOWN currency is NEVER summed into any currency total.
        M = withdrawal_metrics([self.acct], owner=self.owner)
        self.assertNotIn("", M["completed_amount_by_currency"])
        self.assertEqual(M["completed_amount_by_currency"], {})


class PrivacyAndIsolation(StaffCompletionBase):
    def test_no_full_account_or_body_in_command_output(self):
        from io import StringIO
        out = StringIO()
        call_command("complete_staff_withdrawal", owner="owner@x.invalid", withdrawal_id=self.wd.id,
                     broker_profile_id=self.profile.id, completion_candidate_id=self.c_comp.id,
                     expected_account_suffix="5017", expected_amount="30", approved_by="staff@guvfx.test",
                     apply=True, stdout=out)
        text = out.getvalue()
        self.assertIn("COMPLETED", text)
        self.assertIn("••••5017", text)
        self.assertNotIn("70005017", text)                 # the full login never appears
        self.assertNotIn("processed successfully", text)    # no raw body
        self.assertNotIn("owner@x.invalid", text)           # owner email is masked
