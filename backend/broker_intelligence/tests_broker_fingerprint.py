"""ADR-0099 (Lane A) — data-driven certified broker fingerprint: model, detector, onboarding, seed, regression,
privacy and adversarial mutation tests. SYNTHETIC fixtures only."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from types import SimpleNamespace

from broker_intelligence import generic_detector as g
from broker_intelligence import fingerprint_onboarding as onb
from broker_intelligence import fingerprint_reprocess as rep
from broker_intelligence import fingerprint_seed as seed
from broker_intelligence.models import BrokerFingerprint, BrokerFingerprintAudit, BrokerProfile


# ---- a reusable certified-fingerprint dict (detector is pure; dict with cert=True == active+certified) ----
def _cert_fp(**over):
    base = {
        "name": "acme", "version": 1, "broker_display_name": "Acme FX", "cert": True,
        "authenticated_sender_domains": ["acme-fx.com"], "subject_indicators": ["withdrawal"],
        "lifecycle_phrases": {"COMPLETED": ["withdrawal completed"]},
        "amount_hint": "Amount", "currency_hint": "Currency Type", "reference_hint": "Transaction ID",
        "semantic_overrides": {},
    }
    base.update(over)
    return base


def _d(subject="", body="", *, domain="acme-fx.com", auth="pass", fps=None, dkim=(), env="", rt="", allow=False):
    return g.detect(subject=subject, body=body, from_domain=domain, auth_verdict=auth, sender_allowlisted=allow,
                    fingerprints=(fps if fps is not None else [_cert_fp()]), dkim_domains=dkim,
                    envelope_from_domain=env, reply_to_domain=rt)


# ==========================================================================================================
# Detector — certified emission, external-evidence gate, ambiguity, spoofing, extraction. No DB.
# ==========================================================================================================
class CertifiedEmission(SimpleTestCase):
    def test_certified_external_withdrawal(self):
        d = _d("Withdrawal completed", "GBP 42.00 sent to your bank account ending 7766")
        self.assertEqual(d.disposition, g.GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_EXTERNAL_WITHDRAWAL)
        self.assertIn(g.RC_CERTIFIED_FINGERPRINT, d.reason_codes)
        self.assertIn(g.RC_EXTERNAL_EVIDENCE, d.reason_codes)
        self.assertEqual((d.amount, d.currency), (Decimal("42.00"), "GBP"))
        self.assertEqual(d.masked_account_suffix, "••••7766")
        self.assertEqual(d.candidate_broker_name, "Acme FX")

    def test_certified_without_external_evidence_is_unknown(self):
        d = _d("Withdrawal completed", "GBP 42.00 withdrawal completed")   # no rail => not external
        self.assertEqual(d.disposition, g.GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_UNKNOWN)
        self.assertIn(g.RC_NO_EXTERNAL_EVIDENCE, d.reason_codes)

    def test_internal_transfer_is_not_external(self):
        d = _d("Withdrawal completed", "GBP 42.00 withdrawal completed, moved to your trading account ending 1")
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_UNKNOWN)

    def test_non_withdrawal_with_external_rail_is_not_counted(self):
        # Review Finding A: a NON-withdrawal broker email (rebate/refund) that merely says "has been completed … sent
        # to your bank" must NOT be counted as a certified EXTERNAL_WITHDRAWAL (no withdrawal INTENT token present).
        fp = _cert_fp(subject_indicators=[], lifecycle_phrases={"COMPLETED": ["has been completed"]})
        d = _d("Monthly rebate", "Your monthly rebate has been completed and $12.00 has been sent to your bank account",
               fps=[fp])
        if d.disposition == g.GENERIC_HIGH_CONFIDENCE:   # it may classify as a lifecycle event…
            self.assertEqual(d.parsed_event.transaction_category, g.CAT_UNKNOWN)  # …but NEVER a counted withdrawal

    def test_requires_reference_missing_downgrades(self):
        fp = _cert_fp(semantic_overrides={"requires_reference": True})
        d = _d("Withdrawal completed", "GBP 42.00 sent to your bank account ending 7766", fps=[fp])
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_UNKNOWN)
        self.assertIn(g.RC_REQUIRES_REFERENCE_MISSING, d.reason_codes)

    def test_requires_reference_present_allows_external(self):
        fp = _cert_fp(semantic_overrides={"requires_reference": True})
        d = _d("Withdrawal completed",
               "GBP 42.00 sent to your bank account ending 7766. Transaction ID: WD-123", fps=[fp])
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_EXTERNAL_WITHDRAWAL)
        self.assertEqual(d.broker_reference_id, "WD-123")


class Eligibility(SimpleTestCase):
    def test_uncertified_fingerprint_never_emits_certified(self):
        fp = _cert_fp()
        fp.pop("cert")
        fp.update({"certified_capability": False, "is_active": True, "governance_status": "CERTIFIED"})
        d = _d("Withdrawal completed", "GBP 42.00 sent to your bank account ending 7766", fps=[fp])
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, d.reason_codes)
        # no legacy pattern + not allowlisted => review, never an external event
        self.assertEqual(d.disposition, g.REVIEW_REQUIRED)

    def test_inactive_fingerprint_not_eligible(self):
        fp = _cert_fp()
        fp.pop("cert")
        fp.update({"certified_capability": True, "is_active": False, "governance_status": "CERTIFIED"})
        d = _d("Withdrawal completed", "sent to your bank", fps=[fp])
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, d.reason_codes)

    def test_wrong_status_not_eligible(self):
        fp = _cert_fp()
        fp.pop("cert")
        fp.update({"certified_capability": True, "is_active": True, "governance_status": "DEACTIVATED"})
        d = _d("Withdrawal completed", "sent to your bank", fps=[fp])
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, d.reason_codes)


class Spoofing(SimpleTestCase):
    def test_spoofed_from_domain_does_not_match(self):
        d = _d("Withdrawal completed", "sent to your bank account ending 7766", domain="acme-fx.com.evil.com")
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, d.reason_codes)

    def test_declared_dkim_must_align(self):
        fp = _cert_fp(dkim_signing_domains=["acme-fx.com"])
        miss = _d("Withdrawal completed", "sent to your bank", fps=[fp], dkim=["mailchimp.com"])
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, miss.reason_codes)
        ok = _d("Withdrawal completed", "GBP 5.00 sent to your bank account ending 1", fps=[fp], dkim=["acme-fx.com"])
        self.assertIn(g.RC_CERTIFIED_FINGERPRINT, ok.reason_codes)

    def test_declared_envelope_must_align(self):
        fp = _cert_fp(envelope_from_domains=["bounce.acme-fx.com"])
        miss = _d("Withdrawal completed", "sent to your bank", fps=[fp], env="evil.com")
        self.assertNotIn(g.RC_CERTIFIED_FINGERPRINT, miss.reason_codes)

    def test_auth_fail_quarantines_even_with_certified_fp(self):
        d = _d("Withdrawal completed", "sent to your bank account ending 7766", auth="fail")
        self.assertEqual(d.disposition, g.QUARANTINED)
        self.assertIn(g.RC_AUTH_FAIL, d.reason_codes)


class Ambiguity(SimpleTestCase):
    def test_two_distinct_certified_brokers_quarantine(self):
        a = _cert_fp(name="acme", authenticated_sender_domains=["acme-fx.com"], subject_indicators=[])
        b = _cert_fp(name="beta", broker_display_name="Beta", authenticated_sender_domains=["acme-fx.com"],
                     subject_indicators=[])
        d = _d("Withdrawal completed", "sent to your bank account ending 7766", fps=[a, b])
        self.assertEqual(d.disposition, g.QUARANTINED)
        self.assertIn(g.RC_MULTIPLE_FINGERPRINTS, d.reason_codes)


class ExtractionHints(SimpleTestCase):
    def test_field_labelled_amount_currency_and_reference(self):
        body = ("Your withdrawal has been processed successfully.\nAmount: 30.00\nCurrency Type: GBP\n"
                "The funds have been sent to your bank account ending 5017.\nTransaction ID: WD-SYNTH-5017\n")
        fp = _cert_fp(lifecycle_phrases={"COMPLETED": ["processed successfully"]},
                      semantic_overrides={"requires_reference": True, "currency_from_field": True})
        d = _d("Your withdrawal has been processed", body, domain="acme-fx.com", fps=[fp])
        self.assertEqual(d.proposed_state, g.S_COMPLETED)
        self.assertEqual((d.amount, d.currency), (Decimal("30.00"), "GBP"))
        self.assertEqual(d.masked_account_suffix, "••••5017")
        self.assertEqual(d.broker_reference_id, "WD-SYNTH-5017")
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_EXTERNAL_WITHDRAWAL)

    def test_fingerprint_lifecycle_phrase_drives_classification(self):
        # A broker-specific completion phrase NOT in the built-in set, supplied by the fingerprint.
        fp = _cert_fp(lifecycle_phrases={"COMPLETED": ["money is on its way to you"]})
        d = _d("Withdrawal", "Your money is on its way to you — sent to your bank account ending 2", fps=[fp])
        self.assertEqual(d.proposed_state, g.S_COMPLETED)

    def test_long_label_is_bounded_no_hang(self):
        # ReDoS guard: a > 48-char label is ignored (returns '') rather than compiling a huge pattern.
        self.assertEqual(g._labelled_value("Amount: 30.00", "A" * 100, r"\d+"), "")

    def test_amount_field_caps_giant_number_no_quadratic_blowup(self):
        # Review MEDIUM: a crafted giant comma-grouped number in the amount field must be length-capped BEFORE it
        # reaches the quadratic _AMOUNT_RE. With the cap this returns fast; without it the suite would hang.
        fp = _cert_fp(amount_hint="Withdrawal Amount")
        body = "Withdrawal Amount: 1" + ",234" * 6000
        amt, _cur = g._fp_amount_from_field(fp, body)
        self.assertIsNotNone(amt)   # extracted from the capped prefix, not the full 24KB run

    def test_currency_not_invented(self):
        self.assertEqual(g._fp_currency_from_field(_cert_fp(), "Currency Type: ZZZ"), "")

    def test_pure_digit_reference_is_dropped_privacy(self):
        # Review (privacy MEDIUM): a long PURE-DIGIT run under a (mis-chosen) reference label is indistinguishable
        # from a full account number — it must NOT be stored as a 'reference'.
        fp = _cert_fp(reference_hint="Account Number")
        self.assertEqual(g._fp_reference(fp, "Account Number: 40308669012345"), "")
        self.assertEqual(g._fp_reference(fp, "Account Number: WD-5017"), "WD-5017")  # marked reference is kept


class NoRegressionNoFingerprints(SimpleTestCase):
    def test_empty_fingerprints_is_byte_identical_legacy(self):
        pat = [{"name": "acme", "version": 1, "broker_display_name": "Acme FX",
                "domains": ["acme-fx.com"], "subject_indicators": ["withdrawal"], "body_indicators": []}]
        d = g.detect(subject="Withdrawal completed", body="GBP 42.00 sent to account ending 7766",
                     from_domain="acme-fx.com", auth_verdict="pass", sender_allowlisted=True, patterns=pat)
        self.assertEqual(d.disposition, g.GENERIC_HIGH_CONFIDENCE)
        self.assertEqual(d.parsed_event.transaction_category, "UNKNOWN")   # legacy cap preserved


# ==========================================================================================================
# Model — hashing, immutability (app + DB trigger), uniqueness, audit. DB.
# ==========================================================================================================
class ModelIntegrity(TestCase):
    def _fp(self, **over):
        d = dict(name="acme", version=1, broker_display_name="Acme FX",
                 authenticated_sender_domains=["acme-fx.com"], lifecycle_phrases={"COMPLETED": ["a", "b"]})
        d.update(over)
        return BrokerFingerprint.objects.create(**d)

    def test_source_hash_order_independent(self):
        f1 = BrokerFingerprint(name="x", version=1, broker_display_name="X",
                               authenticated_sender_domains=["a.com", "b.com"], subject_indicators=["w", "p"])
        f2 = BrokerFingerprint(name="x", version=1, broker_display_name="X",
                               authenticated_sender_domains=["b.com", "a.com"], subject_indicators=["p", "w"])
        self.assertEqual(f1.compute_source_hash(), f2.compute_source_hash())

    def test_source_hash_changes_with_content(self):
        f1 = BrokerFingerprint(name="x", version=1, broker_display_name="X", authenticated_sender_domains=["a.com"])
        f2 = BrokerFingerprint(name="x", version=1, broker_display_name="X", authenticated_sender_domains=["z.com"])
        self.assertNotEqual(f1.compute_source_hash(), f2.compute_source_hash())

    def test_unknown_semantic_key_rejected(self):
        with self.assertRaises(ValidationError):
            self._fp(semantic_overrides={"arbitrary_power": True})

    def test_non_bool_semantic_value_rejected(self):
        with self.assertRaises(ValidationError):
            self._fp(semantic_overrides={"requires_reference": "yes"})

    def test_certified_matching_field_immutable_app_layer(self):
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        fp.subject_indicators = ["changed"]
        with self.assertRaises(ValidationError):
            fp.save()

    def test_certified_governance_field_still_editable(self):
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        fp.is_active = True
        fp.save()   # activation changes no matching field => allowed
        self.assertTrue(BrokerFingerprint.objects.get(pk=fp.pk).is_active)

    def test_db_trigger_blocks_bulk_matching_column_mutation(self):
        # Review Finding 1 (CRITICAL): a bulk QuerySet.update() of a MATCHING COLUMN bypasses save() (so source_hash
        # stays stale) — the trigger must compare the columns directly and still reject it.
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerFingerprint.objects.filter(pk=fp.pk).update(subject_indicators=["attacker"])

    def test_db_trigger_blocks_bulk_source_hash_mutation(self):
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerFingerprint.objects.filter(pk=fp.pk).update(source_hash="0" * 64)

    def test_db_trigger_blocks_uncertify(self):
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerFingerprint.objects.filter(pk=fp.pk).update(certified_capability=False)

    def test_save_blocks_revoking_certified_capability(self):
        # Review Finding 4: the model save() itself (not only the trigger) must block revoking the capability.
        fp = self._fp(certified_capability=True, governance_status="CERTIFIED")
        fp.certified_capability = False
        with self.assertRaises(ValidationError):
            fp.save()

    def test_audit_db_trigger_blocks_bulk_update_and_delete(self):
        # Review Finding 2 (HIGH): the append-only audit must be enforced at the DB too (bulk update/delete bypass
        # the model guards).
        fp = self._fp()
        a = BrokerFingerprintAudit.objects.create(fingerprint=fp, action="PROPOSE", actor="user:1")
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerFingerprintAudit.objects.filter(pk=a.pk).update(actor="rewritten")
        with self.assertRaises(Exception):
            with transaction.atomic():
                BrokerFingerprintAudit.objects.filter(pk=a.pk).delete()

    def test_one_active_certified_per_name(self):
        self._fp(version=1, certified_capability=True, is_active=True, governance_status="CERTIFIED")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._fp(version=2, certified_capability=True, is_active=True, governance_status="CERTIFIED")

    def test_audit_append_only(self):
        fp = self._fp()
        a = BrokerFingerprintAudit.objects.create(fingerprint=fp, action="PROPOSE", actor="user:1")
        a.reason = "x"
        with self.assertRaises(ValidationError):
            a.save()
        with self.assertRaises(ValidationError):
            a.delete()


# ==========================================================================================================
# Onboarding — propose/validate/certify (fail-closed gates)/activate/deactivate/rollback. DB.
# ==========================================================================================================
class Onboarding(TestCase):
    def setUp(self):
        self.profile = BrokerProfile.objects.create(
            name="Acme FX", slug="acme", approved_sender_domains="acme-fx.com", is_active=True)

    def _propose(self, **over):
        profile = over.pop("broker_profile", self.profile)
        cfg = dict(authenticated_sender_domains=["acme-fx.com"], subject_indicators=["withdrawal"],
                   lifecycle_phrases={"COMPLETED": ["withdrawal completed"]},
                   positive_examples=[{"subject": "Withdrawal completed",
                                       "body": "GBP 5.00 withdrawal completed, sent to your bank account ending 1",
                                       "from_domain": "acme-fx.com", "auth_verdict": "pass"}],
                   negative_examples=[{"subject": "Deposit received", "body": "deposit received",
                                       "from_domain": "acme-fx.com", "auth_verdict": "pass"}])
        cfg.update(over)
        return onb.propose_fingerprint(name="acme", broker_display_name="Acme FX", actor="user:1",
                                       broker_profile=profile, reason="r", ticket="T-1", **cfg)

    def test_propose_creates_proposed_v1_with_audit(self):
        fp = self._propose()
        self.assertEqual((fp.version, fp.governance_status, fp.certified_capability), (1, "PROPOSED", False))
        self.assertTrue(BrokerFingerprintAudit.objects.filter(fingerprint=fp, action="PROPOSE").exists())

    def test_validate_marks_validated_and_report_is_redacted(self):
        fp = self._propose()
        report = onb.validate_fingerprint(fp, actor="user:1")
        self.assertEqual(BrokerFingerprint.objects.get(pk=fp.pk).governance_status, "VALIDATED")
        self.assertGreaterEqual(report["would_match"], 1)
        # structural-only: no raw subject/body anywhere in the report
        import json as _j
        blob = _j.dumps(report)
        self.assertNotIn("Withdrawal completed", blob)
        self.assertNotIn("sent to your bank", blob)

    def test_certify_requires_profile(self):
        fp = self._propose(broker_profile=None)
        fp.broker_profile = None
        fp.save()
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T", broker_profile=None)

    def test_certify_requires_domain_covered_by_profile(self):
        fp = self._propose(authenticated_sender_domains=["not-approved.com"])
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_certify_requires_reason_and_ticket(self):
        fp = self._propose()
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="", ticket="")

    def test_certify_blocks_on_failing_positive_fixture(self):
        fp = self._propose(positive_examples=[{"subject": "hello", "body": "nothing relevant here",
                                               "from_domain": "acme-fx.com", "auth_verdict": "pass"}])
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_certify_success_then_activate(self):
        fp = self._propose()
        onb.validate_fingerprint(fp, actor="user:1")
        fp = onb.certify_fingerprint(fp, actor="user:3", reason="ok", ticket="T-9")
        self.assertTrue(fp.certified_capability)
        self.assertEqual(fp.governance_status, "CERTIFIED")
        self.assertFalse(fp.is_active)   # certification != activation
        self.assertTrue(fp.approved_by)
        fp = onb.activate_fingerprint(fp, actor="user:3")
        self.assertTrue(fp.is_active)
        self.assertTrue(BrokerFingerprintAudit.objects.filter(fingerprint=fp, action="CERTIFY").exists())

    def test_certify_domain_conflict_blocks(self):
        # a different certified broker already owns the domain
        other_profile = BrokerProfile.objects.create(name="Beta", slug="beta",
                                                      approved_sender_domains="acme-fx.com", is_active=True)
        bfp = onb.propose_fingerprint(name="beta", broker_display_name="Beta", actor="user:1",
                                      broker_profile=other_profile, authenticated_sender_domains=["acme-fx.com"],
                                      subject_indicators=["withdrawal"],
                                      lifecycle_phrases={"COMPLETED": ["withdrawal completed"]},
                                      positive_examples=[{"subject": "Withdrawal completed",
                                                          "body": "GBP 5 withdrawal completed sent to your bank account ending 1",
                                                          "from_domain": "acme-fx.com", "auth_verdict": "pass"}],
                                      negative_examples=[{"subject": "Deposit", "body": "deposit received",
                                                          "from_domain": "acme-fx.com", "auth_verdict": "pass"}])
        onb.validate_fingerprint(bfp, actor="user:1")
        onb.certify_fingerprint(bfp, actor="user:1", reason="r", ticket="T")
        onb.activate_fingerprint(bfp, actor="user:1")
        fp = self._propose()
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_certify_blocks_domain_only_fingerprint(self):
        fp = self._propose(subject_indicators=[], body_indicators=[])
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_certify_requires_negative_fixture(self):
        fp = self._propose(negative_examples=[])
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_certify_rejects_sensitive_config(self):
        fp = self._propose(reference_hint="See https://evil.example.com/confirm?token=abc")
        onb.validate_fingerprint(fp, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp, actor="user:1", reason="r", ticket="T")

    def test_cannot_certify_two_brokers_on_one_domain_even_inactive(self):
        # Review Finding B: certify A (inactive) then try to certify B on the SAME domain — blocked at certify-time,
        # so the "activate both" ordering bypass is unreachable.
        fp_a = self._propose()
        onb.validate_fingerprint(fp_a, actor="user:1")
        onb.certify_fingerprint(fp_a, actor="user:1", reason="r", ticket="T")   # certified, NOT activated
        BrokerProfile.objects.create(name="Beta", slug="beta", approved_sender_domains="acme-fx.com", is_active=True)
        fp_b = onb.propose_fingerprint(
            name="beta", broker_display_name="Beta", actor="user:1",
            broker_profile=BrokerProfile.objects.get(slug="beta"), authenticated_sender_domains=["acme-fx.com"],
            subject_indicators=["withdrawal"], lifecycle_phrases={"COMPLETED": ["withdrawal completed"]},
            positive_examples=[{"subject": "Withdrawal completed",
                                "body": "GBP 5 withdrawal completed sent to your bank account ending 1",
                                "from_domain": "acme-fx.com", "auth_verdict": "pass"}],
            negative_examples=[{"subject": "Deposit", "body": "deposit received", "from_domain": "acme-fx.com"}])
        onb.validate_fingerprint(fp_b, actor="user:1")
        with self.assertRaises(ValidationError):
            onb.certify_fingerprint(fp_b, actor="user:1", reason="r", ticket="T")

    def test_rollback_restores_prior_version(self):
        fp1 = self._propose()
        onb.validate_fingerprint(fp1, actor="user:1")
        onb.certify_fingerprint(fp1, actor="user:1", reason="r", ticket="T")
        onb.activate_fingerprint(fp1, actor="user:1")
        fp2 = self._propose(subject_indicators=["withdrawal", "payout"])
        onb.validate_fingerprint(fp2, actor="user:1")
        onb.certify_fingerprint(fp2, actor="user:1", reason="r", ticket="T")
        onb.deactivate_fingerprint(fp1, actor="user:1")
        onb.activate_fingerprint(fp2, actor="user:1")
        restored = onb.rollback_to_version("acme", fp1.version, actor="user:1", reason="bad v2", ticket="T")
        self.assertEqual(restored.version, fp1.version)
        self.assertTrue(BrokerFingerprint.objects.get(pk=fp1.pk).is_active)
        self.assertFalse(BrokerFingerprint.objects.get(pk=fp2.pk).is_active)
        self.assertEqual(BrokerFingerprint.objects.get(pk=fp2.pk).governance_status, "ROLLED_BACK")


# ==========================================================================================================
# Seed + non-regression of existing brokers. DB.
# ==========================================================================================================
class SeedAndRegression(TestCase):
    def setUp(self):
        # The broker profiles are already seeded by migrations (0026/0027/0030). Upsert to guarantee the
        # approved sender domains + active state this test relies on, without colliding on the unique name/slug.
        for slug, name, dom in [("vantage", "Vantage", "vantagemarkets.com"),
                                ("pepperstone", "Pepperstone", "pepperstone.com"),
                                ("dominion_markets", "Dominion Markets", "dominionmarkets.com")]:
            prof = BrokerProfile.objects.filter(slug=slug).first() or BrokerProfile(slug=slug, name=name)
            prof.name = prof.name or name
            prof.approved_sender_domains = dom
            prof.is_active = True
            prof.save()

    def test_seed_creates_certified_fingerprints_with_stable_checksum(self):
        results = {r["name"]: r for r in seed.seed_fingerprints(actor="seed")}
        for nm in ("vantage", "pepperstone", "dominion_markets"):
            self.assertEqual(results[nm]["status"], "SEEDED")
            self.assertTrue(results[nm]["checksum_match"])
            self.assertTrue(results[nm]["active"] and results[nm]["certified"])

    def test_seed_idempotent(self):
        seed.seed_fingerprints(actor="seed")
        second = {r["name"]: r for r in seed.seed_fingerprints(actor="seed")}
        self.assertEqual(second["vantage"]["status"], "ALREADY_SEEDED")

    def test_vantage_fingerprint_reproduces_withdrawal_4(self):
        seed.seed_fingerprints(actor="seed")
        fp = BrokerFingerprint.objects.get(name="vantage", is_active=True, certified_capability=True)
        d = g.detect(
            subject="Your withdrawal has been processed",
            body=("Your withdrawal has been processed successfully.\nAmount: 30.00\nCurrency Type: GBP\n"
                  "The funds have been sent to your bank account ending 5017.\nTransaction ID: WD-SYNTH-5017\n"),
            from_domain="vantagemarkets.com", auth_verdict="pass", sender_allowlisted=False,
            fingerprints=[fp])
        self.assertEqual(d.proposed_state, g.S_COMPLETED)
        self.assertEqual((d.amount, d.currency), (Decimal("30.00"), "GBP"))
        self.assertEqual(d.masked_account_suffix, "••••5017")
        self.assertEqual(d.parsed_event.transaction_category, g.CAT_EXTERNAL_WITHDRAWAL)
        self.assertTrue(d.broker_reference_id)

    def test_specific_parsers_still_win_so_no_regression(self):
        # The keystone: with the default parsers registered, select_parser returns the SPECIFIC parser for each
        # broker domain, so the generic+fingerprint path NEVER runs for them (their behaviour cannot regress).
        from broker_intelligence import parsers
        from broker_intelligence.parsers_tradersway import register_default_parsers
        parsers.clear_registry()
        try:
            register_default_parsers()
            for dom, expect in [("ops@vantagemarkets.com", "vantage"), ("noreply@pepperstone.com", "pepperstone"),
                                ("payments@tradersway.com", "tradersway"), ("info@is6.com", "is6")]:
                p = parsers.select_parser(subject="Withdrawal", body="your withdrawal", from_address=dom)
                self.assertIsNotNone(p, f"no specific parser for {dom}")
                self.assertEqual(p.name, expect)
        finally:
            parsers.clear_registry()


# ==========================================================================================================
# Bounded reprocessing of retained evidence (pure planner). No DB.
# ==========================================================================================================
class ReprocessPlanner(SimpleTestCase):
    def _fps(self):
        return [_cert_fp(name="acme", version=2)]

    def _item(self, **o):
        base = {"subject": "Withdrawal completed", "body": "GBP 5.00 sent to your bank account ending 1",
                "from_domain": "acme-fx.com", "auth_verdict": "pass", "blob_sha256": "a" * 64, "existing_event": None}
        base.update(o)
        return base

    def test_pin_must_be_active_certified(self):
        with self.assertRaises(ValidationError):
            rep.plan_reprocess(owner_id=1, items=[self._item()], fingerprints=self._fps(),
                               fingerprint_name="acme", fingerprint_version=99)

    def test_owner_required(self):
        with self.assertRaises(ValidationError):
            rep.plan_reprocess(owner_id=0, items=[], fingerprints=self._fps(),
                               fingerprint_name="acme", fingerprint_version=2)

    def test_would_create_for_new_evidence(self):
        plan = rep.plan_reprocess(owner_id=1, items=[self._item()], fingerprints=self._fps(),
                                  fingerprint_name="acme", fingerprint_version=2)
        self.assertEqual(plan["results"][0]["action"], rep.A_WOULD_CREATE)
        self.assertEqual(plan["results"][0]["transaction_category"], g.CAT_EXTERNAL_WITHDRAWAL)

    def test_idempotent_skip_when_event_exists(self):
        ev = SimpleNamespace(transaction_category=g.CAT_EXTERNAL_WITHDRAWAL, event_type="WITHDRAWAL_COMPLETED")
        plan = rep.plan_reprocess(owner_id=1, items=[self._item(existing_event=ev)], fingerprints=self._fps(),
                                  fingerprint_name="acme", fingerprint_version=2)
        self.assertEqual(plan["results"][0]["action"], rep.A_SKIP_IDEMPOTENT)

    def test_conflict_stops_for_review(self):
        ev = SimpleNamespace(transaction_category=g.CAT_UNKNOWN, event_type="WITHDRAWAL_REQUESTED")
        plan = rep.plan_reprocess(owner_id=1, items=[self._item(existing_event=ev)], fingerprints=self._fps(),
                                  fingerprint_name="acme", fingerprint_version=2)
        self.assertEqual(plan["results"][0]["action"], rep.A_CONFLICT)

    def test_bounded_and_truncation_reported(self):
        items = [self._item(blob_sha256=f"{i:064d}") for i in range(10)]
        plan = rep.plan_reprocess(owner_id=1, items=items, fingerprints=self._fps(),
                                  fingerprint_name="acme", fingerprint_version=2, limit=3)
        self.assertEqual(plan["considered"], 3)
        self.assertTrue(plan["truncated"])

    def test_not_a_withdrawal_stays_na(self):
        plan = rep.plan_reprocess(owner_id=1, items=[self._item(subject="Deposit received", body="deposit received")],
                                  fingerprints=self._fps(), fingerprint_name="acme", fingerprint_version=2)
        self.assertEqual(plan["results"][0]["action"], rep.A_NOT_WITHDRAWAL)


# ==========================================================================================================
# End-to-end service: run_generic_detection with a canary owner + active certified fingerprint. DB.
# ==========================================================================================================
class ServiceIntegration(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from broker_intelligence.models import EvidenceBlob
        import datetime
        self.User = get_user_model()
        self.owner = self.User.objects.create_user(username="canary", email="canary@example.com", password="x")
        self.other = self.User.objects.create_user(username="other", email="other@example.com", password="x")
        prof = BrokerProfile.objects.filter(slug="vantage").first() or BrokerProfile(slug="vantage", name="Vantage")
        prof.name = prof.name or "Vantage"
        prof.approved_sender_domains = "vantagemarkets.com"
        prof.is_active = True
        prof.save()
        seed.seed_fingerprints(actor="seed", only=["vantage"])
        self.blob = EvidenceBlob.objects.create(
            sha256="e" * 64, storage_backend="file", storage_key="k", byte_size=10,
            received_at=datetime.datetime(2026, 11, 12, tzinfo=datetime.timezone.utc))
        self.msg = SimpleNamespace(
            from_address="payments@vantagemarkets.com", auth_verdict="pass",
            subject="Your withdrawal has been processed",
            body=("Your withdrawal has been processed successfully.\nAmount: 30.00\nCurrency Type: GBP\n"
                  "The funds have been sent to your bank account ending 5017.\nTransaction ID: WD-SYNTH-5017\n"),
            html_body="", received_at=self.blob.received_at, provider_message_id="pm-1")

    def _run(self, owner, allowlist):
        from django.test import override_settings
        from broker_intelligence import generic_detection
        with override_settings(BROKER_GENERIC_DETECTION_OWNER_ALLOWLIST=allowlist):
            return generic_detection.run_generic_detection(
                message=self.msg, blob=self.blob, owner=owner, provenance="SYNTHETIC")

    def test_dark_without_canary_creates_no_event(self):
        self.assertIsNone(self._run(self.owner, ""))  # owner not in canary => DARK

    def test_canary_owner_creates_certified_external_event(self):
        ev = self._run(self.owner, "canary@example.com")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.owner_id, self.owner.id)
        self.assertEqual(ev.transaction_category, g.CAT_EXTERNAL_WITHDRAWAL)
        self.assertEqual(ev.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(ev.currency, "GBP")
        self.assertEqual(ev.amount, Decimal("30.00"))

    def test_event_is_owner_scoped_not_leaked_cross_owner(self):
        from broker_intelligence.models import BrokerEvent
        self._run(self.owner, "canary@example.com")
        self.assertEqual(BrokerEvent.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(BrokerEvent.objects.filter(owner=self.other).count(), 0)
