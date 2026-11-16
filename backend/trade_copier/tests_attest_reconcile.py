"""Tests for trade_copier.attest_reconcile + the copier_attest_reconcile command (packet 139).

The reconciliation classifier must, from EXISTING authoritative durable state only (CopierVerifierWindow.uses
+ CopierIngestNonce), decide whether an ambiguous attestation POST was PROCESSED_VERIFIED / PROCESSED_REJECTED
/ NOT_PROCESSED / AMBIGUOUS - fail-closed, never claiming NOT_PROCESSED (the only state that permits a retry)
unless it is provably safe. No key, no secret, no mutation, no host/broker contact.
"""
import json
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from trade_copier import attest_reconcile as R
from trade_copier.models import (CopierIngestNonce, CopierVerifierWindow, CopierVerifierWindowState as S,
                                 CopierVerifierPurpose as P)

NONCE = "nonce_" + "a" * 20


def _window(*, uses=0, state=S.OPEN, purpose=P.ATTEST, correlation_id="", ttl=300, expired=False):
    now = timezone.now()
    exp = now - timedelta(seconds=30) if expired else now + timedelta(seconds=ttl)
    return CopierVerifierWindow.objects.create(
        state=state, purpose=purpose, key_path="/dev/shm/x.key", max_uses=1, uses=uses,
        expires_at=exp, correlation_id=correlation_id)


def _burn(nonce=NONCE):
    return CopierIngestNonce.objects.create(nonce=nonce, expires_at=timezone.now() + timedelta(seconds=300))


class ReconcileClassifier(TestCase):
    def test_not_processed_when_open_unexpired_uses0_nonce_unburned(self):
        _window(uses=0, state=S.OPEN)
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.NOT_PROCESSED)
        self.assertEqual(out["window_uses"], 0)
        self.assertTrue(out["window_open"])
        self.assertFalse(out["nonce_burned"])

    def test_processed_verified_when_nonce_burned(self):
        _window(uses=1, state=S.CLOSED)   # verified path: key claimed (uses=1), window auto-closed
        _burn()
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.PROCESSED_VERIFIED)
        self.assertTrue(out["nonce_burned"])

    def test_processed_rejected_when_claimed_but_nonce_not_burned(self):
        _window(uses=1, state=S.CLOSED)   # rejected path: key claimed (uses=1) but nonce NOT burned
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.PROCESSED_REJECTED)
        self.assertEqual(out["window_uses"], 1)
        self.assertFalse(out["nonce_burned"])

    def test_ambiguous_when_window_closed_uses0(self):
        _window(uses=0, state=S.CLOSED)
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.AMBIGUOUS)

    def test_ambiguous_when_window_open_but_expired_uses0(self):
        _window(uses=0, state=S.OPEN, expired=True)
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.AMBIGUOUS)

    def test_ambiguous_conflict_burned_but_uses0(self):
        _window(uses=0, state=S.OPEN)
        _burn()                           # impossible real state: a burned nonce implies the key was claimed
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.AMBIGUOUS)
        self.assertEqual(out["reason"], "conflict_burned_but_uses0")

    def test_no_window_unburned_is_ambiguous(self):
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.AMBIGUOUS)
        self.assertFalse(out["window_found"])

    def test_no_window_but_burned_is_verified(self):
        _burn()
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertEqual(out["classification"], R.PROCESSED_VERIFIED)

    def test_too_long_nonce_is_bad_nonce_ambiguous(self):
        out = R.reconcile_attestation(nonce="x" * 81)
        self.assertEqual(out["classification"], R.AMBIGUOUS)
        self.assertEqual(out["reason"], "bad_nonce")

    def test_empty_nonce_without_correlation_is_ambiguous(self):
        # an empty nonce is only usable with a correlation (window-based); without one we cannot locate the
        # operation's window safely, so we fail closed.
        for bad in ["", "   "]:
            out = R.reconcile_attestation(nonce=bad)
            self.assertEqual(out["classification"], R.AMBIGUOUS)
            self.assertEqual(out["reason"], "no_nonce_no_correlation")

    def test_correlation_scopes_the_window(self):
        # a window for a DIFFERENT correlation must not satisfy OUR correlation -> AMBIGUOUS (no match).
        other = _window(uses=0, state=S.OPEN, correlation_id="other-corr")
        out = R.reconcile_attestation(nonce=NONCE, correlation_id="my-corr")
        self.assertEqual(out["classification"], R.AMBIGUOUS)
        self.assertFalse(out["correlation_match"])
        # the operation's OWN window (matching correlation) resolves to NOT_PROCESSED. Close the other first
        # to respect the single-OPEN partial-unique constraint.
        other.state = S.CLOSED
        other.save(update_fields=["state"])
        _window(uses=0, state=S.OPEN, correlation_id="my-corr")
        out2 = R.reconcile_attestation(nonce=NONCE, correlation_id="my-corr")
        self.assertEqual(out2["classification"], R.NOT_PROCESSED)
        self.assertTrue(out2["correlation_match"])

    def test_never_retryable_classifications_are_the_only_non_not_processed(self):
        # a defensive guarantee: only NOT_PROCESSED is a retry signal; every other case is terminal.
        _window(uses=1, state=S.CLOSED)   # processed-rejected
        out = R.reconcile_attestation(nonce=NONCE)
        self.assertIn(out["classification"], {R.PROCESSED_VERIFIED, R.PROCESSED_REJECTED, R.AMBIGUOUS})
        self.assertNotEqual(out["classification"], R.NOT_PROCESSED)

    def test_output_is_sanitised_no_secret(self):
        _window(uses=0, state=S.OPEN)
        out = R.reconcile_attestation(nonce=NONCE)
        blob = json.dumps(out)
        for bad in ["key", "secret", "/dev/shm", "x.key", "signature", "password"]:
            self.assertNotIn(bad, blob.lower())


class ReconcileCommand(TestCase):
    def test_command_prints_classification_json_readonly(self):
        _window(uses=0, state=S.OPEN)
        buf = StringIO()
        call_command("copier_attest_reconcile", "--nonce", NONCE, stdout=buf)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["classification"], R.NOT_PROCESSED)
        # read-only: it created no new window / nonce
        self.assertEqual(CopierVerifierWindow.objects.count(), 1)
        self.assertEqual(CopierIngestNonce.objects.count(), 0)

    def test_command_requires_nonce(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("copier_attest_reconcile")


class ReconcileLostResult(TestCase):
    """Empty-nonce (lost trigger RESULT) reconciliation: window-based, scoped by correlation, never claims
    VERIFIED and never misreports a processed request as retryable."""

    def test_empty_nonce_uses1_is_processed_indeterminate(self):
        _window(uses=1, state=S.CLOSED, correlation_id="c1")
        out = R.reconcile_attestation(nonce="", correlation_id="c1")
        self.assertEqual(out["classification"], R.PROCESSED_INDETERMINATE)
        self.assertIsNone(out["nonce_burned"])          # no nonce => verdict unreadable, never VERIFIED
        self.assertEqual(out["window_uses"], 1)

    def test_empty_nonce_uses0_open_is_not_processed(self):
        _window(uses=0, state=S.OPEN, correlation_id="c1")
        out = R.reconcile_attestation(nonce="", correlation_id="c1")
        self.assertEqual(out["classification"], R.NOT_PROCESSED)

    def test_empty_nonce_no_correlation_is_ambiguous(self):
        _window(uses=0, state=S.OPEN)
        out = R.reconcile_attestation(nonce="", correlation_id="")
        self.assertEqual(out["classification"], R.AMBIGUOUS)
        self.assertEqual(out["reason"], "no_nonce_no_correlation")

    def test_empty_nonce_never_verified_even_with_a_burned_nonce_elsewhere(self):
        # a burned nonce for SOME other attestation must not leak VERIFIED to a no-nonce reconcile.
        _burn("some_other_nonce_aaaaaaaaaaaaa")
        _window(uses=1, state=S.CLOSED, correlation_id="c1")
        out = R.reconcile_attestation(nonce="", correlation_id="c1")
        self.assertEqual(out["classification"], R.PROCESSED_INDETERMINATE)
