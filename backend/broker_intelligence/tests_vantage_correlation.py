"""ADR-0090 (GFX-WITHDRAWAL-VANTAGE-CORRELATE-EXEC-019) — Vantage withdrawal lifecycle + reference correlation.

SYNTHETIC, PII-free fixtures ONLY (``vantagemarkets.com`` with invented references/amounts; no customer name, account
number, real reference or raw body). The two observed Vantage subjects ("Withdrawal Submitted" / "Withdrawal Request
Successful") are run through the REAL ``vantage`` adapter and the REAL correlation engine (the same driver the DARK
worker uses).

Headline safety proven here:
  * both subjects open at WITHDRAWAL_REQUESTED, NEVER COMPLETED;
  * two emails sharing ONE broker reference => ONE in-progress withdrawal, replay-idempotent;
  * two DIFFERENT references => two withdrawals (never merged by timing);
  * a reference-LESS Vantage withdrawal email creates NO withdrawal (held for review, never force-correlated);
  * conflicting money evidence fails closed;
  * HTML and plain-text paths agree;
  * the activation migration flips ONLY Vantage and reverses cleanly;
  * Pepperstone / TradersWay / certification / publication are unaffected.
"""
from __future__ import annotations

import importlib
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from broker_intelligence import correlation as C
from broker_intelligence.external_withdrawal_classifier import CERTIFIED_BROKERS
from broker_intelligence.models import BrokerEvent, BrokerProfile, BrokerProfileAudit, Provenance, Withdrawal
from broker_intelligence.parsers_stream_b import PEPPERSTONE, VANTAGE

User = get_user_model()
VTG_FROM = "no-reply@vantagemarkets.com"
_n = 0


# --- synthetic fixtures (no PII) ---------------------------------------------------------------------------------
def _fix(subject, body, html=""):
    return dict(from_address=VTG_FROM, subject=subject, body=body, html_body=html)


def _body(ref="VTG-1", lead="Your withdrawal request has been submitted."):
    return f"{lead}\nWithdrawal method: bank wire\nAmount: USD 100.00\nReference: {ref}\n"


VTG_SUBMITTED = _fix("Withdrawal Submitted", _body(lead="Your withdrawal request has been submitted."))
VTG_SUCCESSFUL = _fix("Withdrawal Request Successful", _body(lead="Good news - your withdrawal request was successful."))
VTG_SUBMITTED_REF2 = _fix("Withdrawal Submitted", _body(ref="VTG-2"))
VTG_NO_REF = _fix("Withdrawal Submitted",
                  "Your withdrawal request has been submitted.\nWithdrawal method: bank wire\nAmount: USD 100.00\n")
# No "Reference:" line, only ordinary boilerplate the loose shared _REFERENCE regex would otherwise capture as a
# SPURIOUS token — ALPHABETIC ('may'/'will'/'please') AND DIGIT-bearing (a duration, a date, a regulatory footer
# number) that a mere "contains a digit" check would NOT stop. The strict line-anchored, marker-bearing
# _REFERENCE_FIELD must treat ALL of these as reference-ABSENT.
VTG_BOILERPLATE_1 = _fix("Withdrawal Submitted",
                         "Your withdrawal request has been submitted.\nWithdrawal method: bank wire\n"
                         "Amount: USD 100.00\nThis transaction 24h to clear; estimated 3-5 business days.\n")
VTG_BOILERPLATE_2 = _fix("Withdrawal Request Successful",
                         "Good news - your withdrawal request was successful.\nWithdrawal method: bank wire\n"
                         "Amount: USD 100.00\nTransaction 2026-11-10 is now queued; for reference please keep this email.\n")
# A CONSTANT regulatory footer carrying a firm reference number — the worst case: every Vantage email shares it, so a
# loose capture would MERGE all distinct withdrawals under one token (dropping real records).
_FOOTER = "Vantage Global Prime is authorised and regulated under reference 428901.\n"
VTG_FOOTER_A = _fix("Withdrawal Submitted",
                    "Your withdrawal request has been submitted.\nWithdrawal method: bank wire\nAmount: USD 80.00\n" + _FOOTER)
VTG_FOOTER_B = _fix("Withdrawal Submitted",
                    "Your withdrawal request has been submitted.\nWithdrawal method: bank wire\nAmount: USD 999.00\n" + _FOOTER)

_HTMLROW = '<tr><td><strong>{label}</strong></td><td width="40">&nbsp;</td><td>{value}</td></tr>'


def _html(subject_phrase, rows):
    detail = "".join(_HTMLROW.format(label=l, value=v) for l, v in rows)
    return f"<html><body><p>{subject_phrase}</p><h2>Withdrawal details</h2><table border='0'>{detail}</table></body></html>"


def _user():
    global _n
    _n += 1
    return User.objects.create_user(username="vtg%04d" % _n, email="vtg%04d@x.invalid" % _n, password="x")


def _mk(owner, fixture, *, minutes, ref=None, account=None):
    """Parse a synthetic Vantage fixture with the REAL adapter and persist it as an owner-scoped BrokerEvent
    (account-UNRESOLVED, mirroring the genuine Vantage observation). ``ref`` overrides the fixture reference."""
    parsed = VANTAGE.parse(**fixture)
    assert parsed is not None, fixture["subject"]
    occurred = timezone.now() - timedelta(days=1) + timedelta(minutes=minutes)
    return BrokerEvent.objects.create(
        owner=owner, trading_account=account, broker=parsed.broker, event_type=parsed.event_type,
        transaction_category=parsed.transaction_category,
        broker_reference_id=(ref if ref is not None else parsed.broker_reference_id),
        amount=parsed.amount, currency=parsed.currency, occurred_at=occurred, received_at=occurred,
        provenance=Provenance.SYNTHETIC, correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)


# --- (1,2) lifecycle classification ------------------------------------------------------------------------------
class VantageLifecycleClassification(TestCase):
    def test_submitted_classifies_requested_external(self):
        p = VANTAGE.parse(**VTG_SUBMITTED)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(p.amount, Decimal("100.00"))
        self.assertEqual(p.currency, "USD")
        self.assertEqual(p.broker_reference_id, "VTG-1")

    def test_request_successful_classifies_requested_never_completed(self):
        p = VANTAGE.parse(**VTG_SUCCESSFUL)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")          # acceptance of the REQUEST
        self.assertNotEqual(p.event_type, "WITHDRAWAL_COMPLETED")       # NEVER completion
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")

    def test_negative_misleading_successful_and_future_and_marketing_never_requested(self):
        # "successful" not anchored to "withdrawal request" must never open a withdrawal.
        self.assertIsNone(VANTAGE.parse(from_address=VTG_FROM, subject="Deposit Successful",
                                        body="Your deposit was successful. Amount: USD 100.00\n"))
        self.assertIsNone(VANTAGE.parse(from_address=VTG_FROM, subject="Payment successful",
                                        body="Your card payment was successful.\n"))
        # future tense breaks the "submitted" adjacency -> fail closed.
        self.assertIsNone(VANTAGE.parse(from_address=VTG_FROM, subject="Heads up",
                                        body="Your withdrawal will be submitted shortly.\n"))
        # marketing noise with a withdraw keyword but no lifecycle wording.
        self.assertIsNone(VANTAGE.parse(from_address=VTG_FROM, subject="Successful traders withdraw profits",
                                        body="Join the most successful traders and withdraw your profits today!\n"))

    def test_negative_cancelled_and_failed_keep_their_own_terminal(self):
        c = VANTAGE.parse(from_address=VTG_FROM, subject="Withdrawal request cancelled",
                          body="Your withdrawal request has been cancelled.\n")
        self.assertEqual(c.event_type, "WITHDRAWAL_CANCELLED")
        f = VANTAGE.parse(from_address=VTG_FROM, subject="Withdrawal request failed",
                          body="Your withdrawal request has failed.\n")
        self.assertEqual(f.event_type, "WITHDRAWAL_FAILED")


# --- (3,4,5,6,10,11) correlation + idempotence -------------------------------------------------------------------
class VantageReferenceCorrelation(TestCase):
    def test_two_messages_same_reference_become_one_in_progress_withdrawal(self):
        u = _user()
        _mk(u, VTG_SUBMITTED, minutes=0)        # ref VTG-1
        _mk(u, VTG_SUCCESSFUL, minutes=1)       # ref VTG-1
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        w = Withdrawal.objects.get()
        self.assertEqual(w.status, Withdrawal.Status.REQUESTED)         # (10) in progress
        self.assertIsNotNone(w.requested_at)
        self.assertIsNone(w.completed_at)                              # (11) no fabricated completion
        self.assertIsNone(w.confirmed_at)
        self.assertIsNone(w.processing_started_at)
        self.assertEqual(str(w.amount), "100.00")
        self.assertEqual(w.currency, "USD")
        self.assertEqual(w.broker_reference_id, "VTG-1")

    def test_order_independent_still_one_withdrawal(self):
        u = _user()
        _mk(u, VTG_SUCCESSFUL, minutes=1)       # acceptance arrives first
        _mk(u, VTG_SUBMITTED, minutes=0)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.REQUESTED)

    def test_replay_is_idempotent(self):
        u = _user()
        _mk(u, VTG_SUBMITTED, minutes=0)
        _mk(u, VTG_SUCCESSFUL, minutes=1)
        C.run_correlation(); C.run_correlation(); C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertIsNone(Withdrawal.objects.get().completed_at)

    def test_different_references_remain_two_withdrawals(self):
        u = _user()
        _mk(u, VTG_SUBMITTED, minutes=0)        # VTG-1
        _mk(u, VTG_SUBMITTED_REF2, minutes=1)   # VTG-2
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 2)

    def test_missing_reference_creates_no_withdrawal_held_for_review(self):
        # ADR-0090 fail-closed: no stable reference => category downgraded to UNKNOWN => correlation never creates a
        # record. Two ref-less Vantage REQUEST emails must NOT be force-correlated into one, and must NOT double-count.
        u = _user()
        p = VANTAGE.parse(**VTG_NO_REF)
        self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")
        self.assertEqual(p.transaction_category, "UNKNOWN")            # downgraded (no reference)
        self.assertEqual(p.broker_reference_id, "")
        _mk(u, VTG_NO_REF, minutes=0)
        _mk(u, VTG_NO_REF, minutes=1)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)               # held; never fabricated, never double-counted

    def test_boilerplate_derived_reference_is_not_trusted(self):
        # Adversarial-review regression (CONFIRMED HIGH + its digit-bearing MEDIUM residual): the loose shared
        # _REFERENCE regex makes the id/no/# marker optional, so mid-sentence boilerplate — alphabetic OR digit-bearing
        # (a duration "24h", a date "2026-11-10") — would yield a NON-EMPTY spurious token. The strict line-anchored,
        # marker-bearing _REFERENCE_FIELD treats all of these as reference-ABSENT, so two ref-less Vantage emails are
        # HELD (0 withdrawals), never opening the TWO withdrawals the guard exists to prevent.
        for fx in (VTG_BOILERPLATE_1, VTG_BOILERPLATE_2):
            p = VANTAGE.parse(**fx)
            self.assertEqual(p.event_type, "WITHDRAWAL_REQUESTED")
            self.assertEqual(p.transaction_category, "UNKNOWN", f"{fx['subject']} must downgrade (no stable ref)")
            self.assertEqual(p.broker_reference_id, "")            # spurious token dropped, not stored
        u = _user()
        _mk(u, VTG_BOILERPLATE_1, minutes=0)
        _mk(u, VTG_BOILERPLATE_2, minutes=1)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)           # NOT 2 — held, never double-counted

    def test_constant_footer_reference_never_merges_distinct_withdrawals(self):
        # Worst case from adversarial review: a CONSTANT regulatory footer number ("...regulated under reference
        # 428901") shared by every email would, under a loose capture, MERGE two genuinely distinct withdrawals
        # ($80 and $999) under one token — silently dropping the $999 record. The footer is mid-sentence => no stable
        # reference => each is HELD (category UNKNOWN), so there is no merge and no dropped/mis-surfaced record.
        u = _user()
        for fx in (VTG_FOOTER_A, VTG_FOOTER_B):
            p = VANTAGE.parse(**fx)
            self.assertEqual(p.broker_reference_id, "")
            self.assertEqual(p.transaction_category, "UNKNOWN")
        _mk(u, VTG_FOOTER_A, minutes=0)
        _mk(u, VTG_FOOTER_B, minutes=1)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)           # neither merged nor mis-surfaced

    def test_genuine_labeled_reference_variants_still_correlate(self):
        # The strict field still accepts genuinely LABELED references (colon, No:, #) so real withdrawals correlate.
        u = _user()
        a = _fix("Withdrawal Submitted",
                 "Your withdrawal request has been submitted.\nWithdrawal method: bank wire\nAmount: USD 100.00\nReference No: VTG-7\n")
        b = _fix("Withdrawal Request Successful",
                 "Good news - your withdrawal request was successful.\nWithdrawal method: bank wire\nAmount: USD 100.00\nReference No: VTG-7\n")
        self.assertEqual(VANTAGE.parse(**a).broker_reference_id, "VTG-7")
        self.assertEqual(VANTAGE.parse(**a).transaction_category, "EXTERNAL_WITHDRAWAL")
        _mk(u, a, minutes=0)
        _mk(u, b, minutes=1)
        C.run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 1)
        self.assertEqual(Withdrawal.objects.get().status, Withdrawal.Status.REQUESTED)


# --- (7,8) conflict fail-closed + HTML/text consistency ----------------------------------------------------------
class VantageParserRobustness(TestCase):
    def test_conflicting_amount_currency_fails_closed(self):
        # Amount field embeds USD but the Currency field says EUR -> inconsistent money evidence -> decline (None).
        html = _html("Withdrawal Submitted", [("Withdrawal method", "Bank Wire"),
                                              ("Withdrawal amount", "USD 250.00"), ("Currency", "EUR"),
                                              ("Reference number", "VTG-9")])
        self.assertIsNone(VANTAGE.parse(from_address=VTG_FROM, subject="Withdrawal Submitted", body="", html_body=html))

    def test_html_and_text_paths_agree(self):
        html = _html("Withdrawal Submitted", [("Withdrawal method", "Bank Wire"),
                                              ("Withdrawal amount", "100.00"), ("Currency", "USD"),
                                              ("Reference number", "VTG-1")])
        ph = VANTAGE.parse(from_address=VTG_FROM, subject="Withdrawal Submitted", body="", html_body=html)
        pt = VANTAGE.parse(**VTG_SUBMITTED)
        for attr in ("event_type", "transaction_category", "currency", "broker_reference_id"):
            self.assertEqual(getattr(ph, attr), getattr(pt, attr), attr)
        self.assertEqual(ph.amount, pt.amount)
        self.assertEqual(ph.transaction_category, "EXTERNAL_WITHDRAWAL")


# --- (9) existing brokers unaffected -----------------------------------------------------------------------------
class ExistingBrokersUnaffected(TestCase):
    def test_requires_reference_is_vantage_scoped_pepperstone_unchanged(self):
        # Pepperstone does NOT require a reference: a ref-less Pepperstone withdrawal email still asserts EXTERNAL
        # exactly as before (the ADR-0090 guard is per-broker and OFF for Pepperstone/Dominion/IS6).
        p = PEPPERSTONE.parse(from_address="no-reply@pepperstone.com", subject="Withdrawal request received",
                              body="Your withdrawal request has been received.\nWithdrawal method: bank wire\nAmount: USD 50.00\n")
        self.assertEqual(p.transaction_category, "EXTERNAL_WITHDRAWAL")
        self.assertEqual(p.broker_reference_id, "")


# --- (12) publication / certification stays disabled -------------------------------------------------------------
class PublicationStaysDisabled(TestCase):
    def test_vantage_not_certified_and_no_public_path(self):
        self.assertEqual(CERTIFIED_BROKERS, frozenset())
        self.assertNotIn("vantage", {b.lower() for b in CERTIFIED_BROKERS})


# --- (13) migration activates Vantage only + reverses cleanly ----------------------------------------------------
class VantageActivationMigration(TestCase):
    def test_activates_vantage_only_and_reverses_cleanly(self):
        mig = importlib.import_module("broker_intelligence.migrations.0030_activate_vantage_profile")
        v = BrokerProfile.objects.get(slug="vantage")
        self.assertTrue(v.is_active)                                   # 0030 ran during test-DB setup
        self.assertEqual(v.provenance, "OPERATOR_VERIFIED")
        self.assertTrue(BrokerProfileAudit.objects.filter(broker_profile=v, changed_by=mig._FWD).exists())

        mig._retire(django_apps, None)                                # TRUE INVERSE
        v.refresh_from_db()
        self.assertFalse(v.is_active)
        self.assertEqual(v.provenance, "SEED")
        self.assertFalse(BrokerProfileAudit.objects.filter(broker_profile=v, changed_by=mig._FWD).exists())
        # ONLY Vantage toggled — the others are exactly as their own migrations left them.
        self.assertTrue(BrokerProfile.objects.get(slug="pepperstone").is_active)
        self.assertTrue(BrokerProfile.objects.get(slug="is6").is_active)
        self.assertTrue(BrokerProfile.objects.get(slug="tradersway").is_active)
        self.assertFalse(BrokerProfile.objects.get(slug="dominion_markets").is_active)

        mig._activate(django_apps, None)                              # re-activate
        v.refresh_from_db()
        self.assertTrue(v.is_active)
        self.assertEqual(v.provenance, "OPERATOR_VERIFIED")
        n = BrokerProfileAudit.objects.filter(broker_profile=v).count()
        mig._activate(django_apps, None)                             # idempotent: no second audit row
        self.assertEqual(BrokerProfileAudit.objects.filter(broker_profile=v).count(), n)


# --- (14) privacy / redaction ------------------------------------------------------------------------------------
class VantagePrivacy(TestCase):
    def test_parser_output_carries_no_name_or_email(self):
        body = ("Dear Alex Doe, your withdrawal request has been submitted.\n"
                "Withdrawal method: bank wire\nAmount: USD 100.00\nReference: VTG-1\n"
                "Questions? contact alex.doe@example.com\n")
        p = VANTAGE.parse(from_address=VTG_FROM, subject="Withdrawal Submitted", body=body)
        blob = "|".join(str(x) for x in (p.event_type, p.transaction_category, p.broker, p.broker_reference_id,
                                         p.amount, p.currency, p.source_account, p.destination_account))
        for leak in ("Alex", "alex", "Doe", "@", "example.com"):
            self.assertNotIn(leak, blob)
        self.assertEqual(p.broker_reference_id, "VTG-1")              # only the opaque reference is kept
