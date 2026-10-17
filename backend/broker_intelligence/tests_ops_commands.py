"""Tests for the new DARK ops tooling: the broker_mailbox_health alert exit code, the read-only sanitised-fixture
extraction command, the generalized read-only certification harness, and the Provenance.SANITISED exemption.

All synthetic; no genuine sample. The command tests assert the strict READ-ONLY / fail-closed guarantees hold.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import ConnectedMailbox, EvidenceBlob, BrokerEvent, Withdrawal, Provenance

User = get_user_model()
_SEQ = [0]

_TRADERSWAY_RAW = (
    b"From: noreply@tradersway.com\r\n"
    b"To: member@gmail.com\r\n"
    b"Subject: Please confirm your withdrawal request\r\n"
    b"Date: Tue, 14 Oct 2026 21:08:25 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Dear John Smith,\r\n"
    b"Please confirm your withdrawal request. This will transfer the amount of 24.12 USD from account 54548 "
    b"to account 647390. Your request number is: 4113188.\r\n"
    b"Confirm here: https://tradersway.com/confirm?token=AbC123XyZ987jklmnop\r\n")


def _mailbox(**extra):
    _SEQ[0] += 1
    n = _SEQ[0]
    u = User.objects.create_user(username="opsuser%d" % n, email="ops%d@x.invalid" % n, password="x")
    return ConnectedMailbox.objects.create(
        user=u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="opspm%d" % n,
        primary_email="x@gmail.com", status=ConnectedMailbox.Status.CONNECTED, cursor_state="c1", **extra)


class MailboxHealthExitCode(TestCase):
    def test_default_invocation_exits_zero_even_when_recovery_required(self):
        _mailbox(last_successful_sync=timezone.now() - timedelta(days=100))   # very stale -> RECOVERY_REQUIRED
        call_command("broker_mailbox_health", stdout=StringIO())              # must NOT raise (exit 0 preserved)

    def test_exit_nonzero_on_recovery_raises_when_recovery_required(self):
        _mailbox(last_successful_sync=timezone.now() - timedelta(days=100))
        with self.assertRaises(CommandError):
            call_command("broker_mailbox_health", "--exit-nonzero-on-recovery", stdout=StringIO())

    def test_exit_nonzero_on_recovery_is_quiet_when_healthy(self):
        _mailbox(last_successful_sync=timezone.now())                         # fresh -> SUCCESSFUL
        call_command("broker_mailbox_health", "--exit-nonzero-on-recovery", stdout=StringIO())  # no raise


class ExtractSanitisedFixtureCommand(TestCase):
    def test_read_only_extraction_masks_pii_preserves_amount(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
            out = StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=out)
            text = out.getvalue()
        self.assertIn("NO_MUTATION=True", text)
        self.assertIn("<NAME>", text)                 # recipient name masked
        self.assertNotIn("John", text)
        self.assertNotIn("Smith", text)
        self.assertNotIn("54548", text)               # account masked
        self.assertNotIn("647390", text)
        self.assertNotIn("4113188", text)             # reference masked
        self.assertNotIn("https://", text)            # confirmation URL masked
        self.assertIn("24.12", text)                  # amount SHAPE preserved
        self.assertEqual(before, (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count()))

    def test_tampered_blob_fails_integrity_cleanly(self):
        # Negative control (RULE-11): a real tamper is reported as a clean failure, not an uncaught traceback, and the
        # hash-verified arm of the guard is actually reachable.
        from broker_intelligence.evidence import EvidenceStore
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            store = EvidenceStore()
            blob, _ = store.put(_TRADERSWAY_RAW)
            with open(store._abs(blob.storage_key), "wb") as fh:    # corrupt the stored bytes (hash now mismatches)
                fh.write(b"TAMPERED BYTES")
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=StringIO())

    def test_out_path_inside_repo_is_rejected(self):
        in_repo = os.path.join(str(settings.BASE_DIR), "broker_intelligence", "_should_not_write.json")
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", in_repo,
                             stdout=StringIO())
        self.assertFalse(os.path.exists(in_repo))


class CertifyBrokerBlobCommand(TestCase):
    def test_read_only_and_fail_closed_posture_for_uncertified_broker(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out = StringIO()
            call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay", stdout=out)
            text = out.getvalue()
        self.assertIn("NO_MUTATION=True", text)
        self.assertIn("fail_closed_when_uncertified=True", text)
        self.assertIn("in CERTIFIED_BROKERS=False", text)

    def test_require_certified_fails_for_uncertified_broker(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            with self.assertRaises(CommandError):
                call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay",
                             "--require-certified", stdout=StringIO())


class SanitisedProvenanceExemption(TestCase):
    def test_sanitised_is_exempt_from_real_allowlist_gate_but_real_is_not(self):
        from broker_intelligence.ingestion import ingest_message
        from broker_intelligence.mail_source import MailMessage
        raw = (b"From: friend@example.com\r\nTo: m@gmail.com\r\nSubject: hi\r\n\r\nhello there\r\n")
        msg = MailMessage(raw_bytes=raw, to_addresses=("m@gmail.com",), from_address="friend@example.com",
                          subject="hi", body="hello there", received_at=timezone.now())
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            before = EvidenceBlob.objects.count()
            # REAL mail from a NON-allowlisted sender is skipped BEFORE storage (nothing retained).
            self.assertIsNone(ingest_message(msg, provenance=Provenance.REAL))
            self.assertEqual(EvidenceBlob.objects.count(), before)
            # SANITISED (a controlled, PII-stripped fixture) is EXEMPT from the allowlist -> evidence IS stored.
            ingest_message(msg, provenance=Provenance.SANITISED)
            self.assertEqual(EvidenceBlob.objects.count(), before + 1)
