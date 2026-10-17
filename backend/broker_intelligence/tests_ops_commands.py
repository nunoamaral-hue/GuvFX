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
    b"54548 in amount of 24.12 USD transferring to your trading account 647390. "
    b"Your withdrawal request number is: 4113188.\r\n"
    b"If you want to confirm the withdrawal request, click: https://tradersway.com/c?token=AbC123XyZ987jklmnop\r\n"
    b"Best regards, Finance Department\r\n")


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


_RAW_IDENTIFIERS = ("John", "Smith", "54548", "647390", "4113188", "noreply@tradersway.com", "https://")


class ExtractSanitisedFixtureDefaultOutputSafe(TestCase):
    """Blocker B: the DEFAULT terminal output is a SAFE SUMMARY — never the body or any residual identifier literal."""

    def test_default_stdout_is_summary_only(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
            out, err = StringIO(), StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=out, stderr=err)
            text = out.getvalue() + err.getvalue()
        self.assertIn("NO_MUTATION=True", text)
        self.assertIn("tradersway.com", text)                     # domain is safe
        self.assertIn("residual_numeric_runs=", text)             # counts, not literals
        self.assertNotIn("<NAME>", text)                          # the (redacted) body is NOT printed
        self.assertNotIn("24.12", text)                           # no body content at all by default
        for ident in _RAW_IDENTIFIERS:
            self.assertNotIn(ident, text, ident)
        self.assertEqual(before, (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count()))

    def test_json_default_is_summary_only(self):
        import json
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out = StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--json", stdout=out)
            payload = json.loads(out.getvalue())
        self.assertNotIn("body", payload)                         # summary carries no body / residual literals
        self.assertNotIn("residual_numeric_runs", payload)
        self.assertEqual(payload["residual_numeric_run_count"], 0)
        self.assertTrue(payload["evidence_hash_verified"])


class ExtractSanitisedFixturePrivateArtifact(TestCase):
    """Blocker B: full detail only via a securely-written private artifact, and the unsafe-export negatives."""

    def test_private_artifact_masks_pii_preserves_amount_and_is_0600(self):
        import json
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outdir, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out_path = os.path.join(outdir, "fixture.sanitised.json")
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", out_path, stdout=StringIO())
            self.assertTrue(os.path.exists(out_path))
            self.assertEqual(os.stat(out_path).st_mode & 0o777, 0o600)          # owner-only
            content = open(out_path, encoding="utf-8").read()
            artifact = json.loads(content.split("\n", 1)[1])                    # skip the leading comment line
        self.assertIn("<NAME>", artifact["body"])                              # detail IS present in the private file
        self.assertIn("24.12", artifact["body"])                               # amount shape preserved
        self.assertTrue(artifact["evidence_hash_verified"])
        self.assertEqual(artifact["provenance_tier"], "SANITISED")
        for ident in _RAW_IDENTIFIERS:
            self.assertNotIn(ident, content, ident)                            # raw identifiers never in the artifact

    def test_existing_target_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outdir, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out_path = os.path.join(outdir, "exists.json")
            with open(out_path, "w") as fh:
                fh.write("ORIGINAL")
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", out_path, stdout=StringIO())
            self.assertEqual(open(out_path).read(), "ORIGINAL")                 # untouched

    def test_symlink_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outdir, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            real_target = os.path.join(outdir, "real.json")
            link = os.path.join(outdir, "link.json")
            os.symlink(real_target, link)
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", link, stdout=StringIO())
            self.assertFalse(os.path.exists(real_target))                      # nothing written through the symlink

    def test_out_inside_repo_is_rejected(self):
        in_repo = os.path.join(str(settings.BASE_DIR), "broker_intelligence", "_should_not_write.json")
        with tempfile.TemporaryDirectory() as evroot, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", in_repo, stdout=StringIO())
        self.assertFalse(os.path.exists(in_repo))

    def test_out_inside_evidence_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as evroot, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            in_evidence = os.path.join(evroot, "leak.json")
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--out", in_evidence, stdout=StringIO())
            self.assertFalse(os.path.exists(in_evidence))

    def test_tampered_blob_fails_integrity_cleanly(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            store = EvidenceStore()
            blob, _ = store.put(_TRADERSWAY_RAW)
            with open(store._abs(blob.storage_key), "wb") as fh:
                fh.write(b"TAMPERED BYTES")
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=StringIO())


class CertifyBrokerBlobCommand(TestCase):
    def test_parser_inspection_pass_but_genuine_certification_blocked(self):
        # Blocker C: a successful parser inspection must NOT read as a genuine certification.
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out = StringIO()
            call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay", stdout=out)
            text = out.getvalue()
        self.assertIn("NO_MUTATION=True", text)
        self.assertIn("PARSER_INSPECTION_PASS=True", text)
        self.assertIn("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=POSITIVE_CERTIFICATION_BLOCKED", text)
        self.assertIn("broker_certified=False", text)
        self.assertIn("fail_closed_when_uncertified=True", text)

    def test_secret_free_output_masks_identifiers(self):
        # Blocker A: sender/account/reference identifiers never appear in stdout/stderr (masked forms only).
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            out, err = StringIO(), StringIO()
            call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay",
                         stdout=out, stderr=err)
            text = out.getvalue() + err.getvalue()
        for ident in ("noreply@tradersway.com", "54548", "647390", "4113188"):
            self.assertNotIn(ident, text, ident)
        self.assertIn("tradersway.com", text)              # domain is shown (safe)
        self.assertIn("account(masked)=", text)            # a masked rendering is present

    def test_require_genuine_certified_fails_and_error_is_secret_free(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            with self.assertRaises(CommandError) as ctx:
                call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay",
                             "--require-genuine-certified", stdout=StringIO(), stderr=StringIO())
            msg = str(ctx.exception)
        self.assertIn("POSITIVE_CERTIFICATION_BLOCKED", msg)
        for ident in ("54548", "647390", "4113188", "noreply@tradersway.com"):
            self.assertNotIn(ident, msg, ident)            # error message leaks no raw identifier

    def test_account_compared_on_last4_only(self):
        # Blocker A: no full account identifier need appear in argv — last-4 comparison suffices.
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)   # genuine template dest account ends 7390
            out = StringIO()
            call_command("certify_broker_blob", "--blob-id", str(blob.id), "--broker", "TradersWay",
                         "--expect-account-last4", "7390", stdout=out)
            text = out.getvalue()
        self.assertIn("account_last4", text)
        self.assertIn("PARSER_INSPECTION_PASS=True", text)   # the last-4 check passed (would FAIL otherwise)


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
