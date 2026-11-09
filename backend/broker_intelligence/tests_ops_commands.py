"""Tests for the new DARK ops tooling: the broker_mailbox_health alert exit code, the read-only sanitised-fixture
extraction command, the generalized read-only certification harness, and the Provenance.SANITISED exemption.

All synthetic; no genuine sample. The command tests assert the strict READ-ONLY / fail-closed guarantees hold.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from broker_intelligence import external_withdrawal_classifier as _ewc
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.models import (ConnectedMailbox, EvidenceBlob, BrokerEvent, Withdrawal, Provenance,
                                        BrokerEmailIdentity, TransactionCategory)
from trading.models import TradingAccount

User = get_user_model()
_SEQ = [0]

# A TradersWay message WITH an Authentication-Results header that yields auth_verdict==pass (From-aligned DKIM from a
# trusted authserv-id), for exercising the genuine-evidence-chain gate.
_TRADERSWAY_AUTHED_RAW = (
    b"From: payments@tradersway.com\r\n"
    b"To: member@gmail.com\r\n"
    b"Subject: Please confirm your withdrawal request\r\n"
    b"Authentication-Results: mx.google.com; dkim=pass header.i=@tradersway.com header.d=tradersway.com; "
    b"spf=pass smtp.mailfrom=bounce.icpbounce.com; dmarc=fail (p=NONE) header.from=tradersway.com\r\n"
    b"Date: Sun, 12 Oct 2026 06:36:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"54548 in amount of 24.12 USD transferring to your trading account 647390. "
    b"Your withdrawal request number is: 4113188.\r\n")

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


def _named_owner(first="John", last="Smith"):
    """A GuvFX user with a known name, used as the owner-identity redaction input the sanitiser now requires
    (packet 008). Synthetic name only."""
    _SEQ[0] += 1
    n = _SEQ[0]
    return User.objects.create_user(username="owner%d" % n, email="owner%d@x.invalid" % n, password="x",
                                    first_name=first, last_name=last)


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
            owner = _named_owner()
            before = (BrokerEvent.objects.count(), EvidenceBlob.objects.count(), Withdrawal.objects.count())
            out, err = StringIO(), StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                         stdout=out, stderr=err)
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
            owner = _named_owner()
            out = StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                         "--json", stdout=out)
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
            owner = _named_owner()
            out_path = os.path.join(outdir, "fixture.sanitised.json")
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                         "--out", out_path, stdout=StringIO())
            self.assertTrue(os.path.exists(out_path))
            self.assertEqual(os.stat(out_path).st_mode & 0o777, 0o600)          # owner-only
            content = open(out_path, encoding="utf-8").read()
            artifact = json.loads(content.split("\n", 1)[1])                    # skip the leading comment line
        self.assertIn("<NAME>", artifact["body"])                              # detail IS present in the private file
        self.assertIn("24.12", artifact["body"])                               # amount shape preserved
        self.assertTrue(artifact["evidence_hash_verified"])
        self.assertEqual(artifact["provenance_tier"], "SANITISED")
        self.assertTrue(artifact["pii_validation"]["clean"])                   # fail-closed gate passed
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

    def test_default_summary_shows_residual_count_but_not_literal(self):
        # A decimal-glued identifier UNDER A NEUTRAL (non-reference) word survives the masker's decimal guard and is
        # surfaced ONLY as a residual COUNT in default output — never as a literal (non-trivial Blocker-B safety). A
        # LABELLED decimal-glued value (e.g. "Reference 2026...") is now actively removed as <REF> by the 008 masker.
        import re
        raw = (b"From: noreply@tradersway.com\r\nTo: member@gmail.com\r\nSubject: statement\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nStatement 20261014.998877 recorded.\r\n")
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(raw)
            owner = _named_owner()
            out = StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id), stdout=out)
            text = out.getvalue()
        self.assertNotIn("20261014", text)                       # literal withheld from default output
        self.assertNotIn("998877", text)
        m = re.search(r"residual_numeric_runs=(\d+)", text)
        self.assertTrue(m and int(m.group(1)) >= 1)              # but its presence IS surfaced as a count

    def test_out_via_symlinked_parent_into_protected_root_is_rejected(self):
        # Proves the inode-based containment: a symlinked PARENT dir resolving into the evidence root is rejected
        # (a plain string startswith would miss this — and misses the case-insensitive variant below).
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outbase, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            link_dir = os.path.join(outbase, "link")
            os.symlink(evroot, link_dir)
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id),
                             "--out", os.path.join(link_dir, "leak.json"), stdout=StringIO())
            self.assertFalse(os.path.exists(os.path.join(evroot, "leak.json")))

    def test_wrong_case_repo_path_rejected_on_case_insensitive_fs(self):
        # The CONFIRMED finding: on a case-insensitive FS (the operator's macOS host) a wrong-case path to the repo
        # is the SAME physical dir; inode containment rejects it where string startswith did not. Skips on a
        # case-sensitive FS, where the wrong-case path is simply a different (nonexistent) directory.
        repo = os.path.dirname(str(settings.BASE_DIR))
        swapped = os.path.join(os.path.dirname(repo), os.path.basename(repo).swapcase())
        in_repo_wrongcase = os.path.join(swapped, "backend", "broker_intelligence", "_wrongcase.json")
        if not os.path.isdir(os.path.dirname(in_repo_wrongcase)):
            self.skipTest("case-sensitive filesystem: wrong-case repo path is a different directory")
        with tempfile.TemporaryDirectory() as evroot, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob, _ = EvidenceStore().put(_TRADERSWAY_RAW)
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id),
                             "--out", in_repo_wrongcase, stdout=StringIO())
        self.assertFalse(os.path.exists(in_repo_wrongcase))

    def test_tampered_blob_fails_integrity_cleanly(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            store = EvidenceStore()
            blob, _ = store.put(_TRADERSWAY_RAW)
            with open(store._abs(blob.storage_key), "wb") as fh:
                fh.write(b"TAMPERED BYTES")
            with self.assertRaises(CommandError):
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=StringIO())


class ExtractSanitisedFixtureOwnerAndPiiGate(TestCase):
    """Packet 008: the owner name is a REQUIRED redaction input (fail closed if unavailable/ambiguous), the PII gate
    fails closed if any personal data would remain, and no owner name ever appears in output."""

    def _put(self, root, raw=_TRADERSWAY_RAW):
        return EvidenceStore().put(raw)[0]

    def test_fails_closed_when_owner_unavailable(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob = self._put(root)          # no mailbox, no event, no --owner-id
            with self.assertRaises(CommandError) as ctx:
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=StringIO())
            self.assertIn("owner identity", str(ctx.exception))

    def test_auto_resolves_single_mailbox_owner(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob = self._put(root)
            u = _named_owner("John", "Smith")
            ConnectedMailbox.objects.create(
                user=u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="solo-pm",
                primary_email="x@gmail.com", status=ConnectedMailbox.Status.CONNECTED, cursor_state="c1")
            out = StringIO()
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=out)   # no --owner-id
            text = out.getvalue()
        self.assertIn("owner_name_source=", text)
        self.assertIn("PII GATE: clean=True", text)
        for ident in ("John", "Smith"):
            self.assertNotIn(ident, text, ident)

    def test_ambiguous_owner_requires_explicit_owner_id(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob = self._put(root)
            _mailbox(); _mailbox()          # two distinct mailbox owners -> ambiguous
            with self.assertRaises(CommandError) as ctx:
                call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), stdout=StringIO())
            self.assertIn("ambiguous", str(ctx.exception))
            owner = _named_owner()          # an explicit owner id disambiguates
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                         stdout=StringIO())

    def test_pii_gate_fails_closed_and_writes_no_fixture(self):
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outdir, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob = self._put(evroot)
            owner = _named_owner()
            out_path = os.path.join(outdir, "must_not_exist.json")
            with mock.patch("broker_intelligence.fixture_extraction.validate_pii_clean",
                            return_value={"ok": False, "counts": {"known_or_discovered_name": 1}}):
                with self.assertRaises(CommandError) as ctx:
                    call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                                 "--out", out_path, stdout=StringIO())
            self.assertIn("PII validation", str(ctx.exception))
            self.assertFalse(os.path.exists(out_path))     # fail closed: NOTHING written

    def test_known_owner_name_masked_even_without_salutation(self):
        raw = (b"From: noreply@tradersway.com\r\nTo: m@gmail.com\r\nSubject: update\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\nYour withdrawal was approved for Zebediah Quill.\r\n")
        with tempfile.TemporaryDirectory() as evroot, tempfile.TemporaryDirectory() as outdir, \
                override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=evroot):
            blob = self._put(evroot, raw)
            owner = _named_owner("Zebediah", "Quill")
            out_path = os.path.join(outdir, "fx.json")
            call_command("extract_sanitised_fixture", "--blob-id", str(blob.id), "--owner-id", str(owner.id),
                         "--out", out_path, stdout=StringIO())
            content = open(out_path, encoding="utf-8").read()
        self.assertNotIn("Zebediah", content)              # known name masked though not in any salutation
        self.assertNotIn("Quill", content)
        self.assertIn("<NAME>", content)


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
        self.assertIn("EVIDENCE_CHAIN_PRECHECK=BLOCKED", text)     # no REAL external event for this blob
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


@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="tradersway.com",
                   BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class CertifyGenuineEvidenceChain(TestCase):
    """R1-CLOSEOUT Blocker C (final): GENUINE_POSITIVE_CLASSIFIER_CERTIFIED must require the WHOLE verified
    owner<->broker<->account<->identity chain on a matching REAL EXTERNAL_WITHDRAWAL event for THIS blob — a REAL
    BrokerEvent merely existing (nullable owner/account, wrong broker/category, mismatched hash) must NOT pass. And
    the machine EVIDENCE_CHAIN_PRECHECK, even when it passes, is NOT genuine certification without a certified broker
    AND an independent human attestation. All data here is SYNTHETIC (test DB)."""

    def _certify(self, *, make_event=True, owner_present=True, account=True, same_owner=True,
                 broker_on_event="TradersWay", category=TransactionCategory.EXTERNAL_WITHDRAWAL, good_hash=True,
                 identity_status="VERIFIED", attest=True, request_broker="TradersWay", external_live=True):
        import contextlib
        _SEQ[0] += 1
        n = _SEQ[0]
        with tempfile.TemporaryDirectory() as root, override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            blob, _ = EvidenceStore().put(_TRADERSWAY_AUTHED_RAW)
            owner = User.objects.create_user(username="chain%d" % n, email="chain%d@x.invalid" % n, password="x")
            acct = None
            if account:
                acct_user = owner if same_owner else User.objects.create_user(
                    username="other%d" % n, email="other%d@x.invalid" % n, password="x")
                acct = TradingAccount.objects.create(user=acct_user, name="A", broker_name="TradersWay",
                                                     account_number="990%05d" % n, is_demo=True, is_active=True)
            if make_event:
                BrokerEvent.objects.create(
                    trading_account=acct, broker=broker_on_event, owner=(owner if owner_present else None),
                    event_type="WITHDRAWAL_COMPLETED", transaction_category=category, received_at=timezone.now(),
                    evidence=blob, evidence_hash=(blob.sha256 if good_hash else "0" * 64),
                    provenance=Provenance.REAL, correlation_status=BrokerEvent.CorrelationStatus.UNRESOLVED)
                if identity_status and acct is not None and owner_present:
                    BrokerEmailIdentity.objects.create(
                        email="member@gmail.com", user=owner, broker_name="TradersWay", trading_account=acct,
                        status=getattr(BrokerEmailIdentity.Status, identity_status))
            args = ["certify_broker_blob", "--blob-id", str(blob.id), "--broker", request_broker]
            if attest:
                args += ["--attest-authenticity-signoff", "REVIEWER:test-signoff"]
            out = StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(_ewc, "CERTIFIED_BROKERS", frozenset({"TradersWay"})))
                if external_live:
                    # Simulate the FUTURE certified per-broker extractor: make the deployed parser live-classify
                    # these bytes EXTERNAL, so genuine cert can be reached and each chain negative isolates its link
                    # (today's real parsers never emit EXTERNAL, so this must be mocked — clearly test-only).
                    from broker_intelligence.parsers import ParsedBrokerEvent
                    from broker_intelligence.parsers_tradersway import TradersWayParser
                    ext = ParsedBrokerEvent(event_type="WITHDRAWAL_COMPLETED",
                                            transaction_category=TransactionCategory.EXTERNAL_WITHDRAWAL,
                                            broker="TradersWay", currency="USD")
                    stack.enter_context(mock.patch.object(TradersWayParser, "parse", return_value=ext))
                try:
                    call_command(*args, stdout=out, stderr=StringIO())
                except CommandError:
                    pass
            return out.getvalue()

    def test_full_chain_certified_broker_and_attestation_is_genuine_pass(self):
        text = self._certify()                                    # positive control (RULE-11)
        self.assertIn("EVIDENCE_CHAIN_PRECHECK=PASS", text)
        self.assertIn("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=PASS", text)

    def test_precheck_passes_but_without_attestation_genuine_is_blocked(self):
        text = self._certify(attest=False)
        self.assertIn("EVIDENCE_CHAIN_PRECHECK=PASS", text)       # preliminary check passed
        self.assertIn("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=POSITIVE_CERTIFICATION_BLOCKED", text)  # but NOT certified

    def test_stored_external_event_but_deployed_parser_not_external_is_blocked(self):
        # The DEPLOYED parser (not just the stored category) must live-classify EXTERNAL. A stored REAL EXTERNAL event
        # with the full chain + certified + attestation must still NOT certify if the current parser disagrees.
        text = self._certify(external_live=False)
        self.assertIn("EVIDENCE_CHAIN_PRECHECK=PASS", text)       # the stored evidence chain is intact
        self.assertIn("deployed_parser_external=False", text)
        self.assertIn("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=POSITIVE_CERTIFICATION_BLOCKED", text)

    def _assert_blocked(self, text):
        self.assertIn("EVIDENCE_CHAIN_PRECHECK=BLOCKED", text)
        self.assertIn("GENUINE_POSITIVE_CLASSIFIER_CERTIFIED=POSITIVE_CERTIFICATION_BLOCKED", text)

    def test_real_event_with_null_owner_is_blocked(self):
        self._assert_blocked(self._certify(owner_present=False))

    def test_real_event_with_no_account_is_blocked(self):
        self._assert_blocked(self._certify(account=False))

    def test_account_not_belonging_to_owner_is_blocked(self):
        self._assert_blocked(self._certify(same_owner=False))

    def test_wrong_broker_on_event_is_blocked(self):
        self._assert_blocked(self._certify(broker_on_event="OtherBroker"))

    def test_wrong_category_is_blocked(self):
        self._assert_blocked(self._certify(category=TransactionCategory.INTERNAL_TRANSFER))

    def test_mismatched_evidence_hash_is_blocked(self):
        self._assert_blocked(self._certify(good_hash=False))

    def test_unverified_identity_is_blocked(self):
        self._assert_blocked(self._certify(identity_status="PENDING"))

    def test_certified_broker_alone_without_chain_is_blocked(self):
        # Avoid circular proof: a certified broker + attestation but NO matching REAL event cannot certify.
        self._assert_blocked(self._certify(make_event=False))


class ExtractSanitisedFixtureContainerLayoutGuard(TestCase):
    """GFX-PEPPERSTONE-EXTRACTOR-PARSER-007 — the private-artifact ``--out`` guard must work in the DEPLOYED container
    layout (``BASE_DIR`` IS the app root, e.g. ``/app``, whose parent is the filesystem root ``/``) as well as the dev
    checkout layout, WITHOUT broadening access to any sensitive root. The previous logic protected
    ``os.path.dirname(BASE_DIR)`` which in the container is ``/`` — marking the entire filesystem protected and
    rejecting every private output path."""

    def _cmd(self):
        from broker_intelligence.management.commands.extract_sanitised_fixture import Command
        return Command()

    def test_container_base_dir_app_protects_app_not_filesystem_root(self):
        # MUTATION-KILLER: reverting the fix to ``roots.append(os.path.dirname(base))`` makes ``/`` a protected root
        # with BASE_DIR=/app, which this assertion forbids.
        with override_settings(BASE_DIR="/app", STATIC_ROOT=None, MEDIA_ROOT=""):
            roots = self._cmd()._unsafe_roots()
        self.assertIn("/app", roots)
        self.assertNotIn("/", roots)

    def test_dev_base_dir_protects_repo_parent_only(self):
        # Dev layout unchanged: BASE_DIR == <repo>/backend → protect <repo> (its parent), never the backend dir alone
        # and never ``/``.
        with override_settings(BASE_DIR="/srv/myrepo/backend", STATIC_ROOT=None, MEDIA_ROOT=""):
            roots = self._cmd()._unsafe_roots()
        self.assertIn("/srv/myrepo", roots)
        self.assertNotIn("/srv/myrepo/backend", roots)
        self.assertNotIn("/", roots)

    def test_evidence_credential_static_media_roots_stay_protected(self):
        with override_settings(BASE_DIR="/app", STATIC_ROOT="/srv/static_x", MEDIA_ROOT="/srv/media_x"):
            with mock.patch.dict(os.environ, {"BROKER_INTELLIGENCE_CREDENTIAL_ROOT": "/srv/cred_x"}):
                roots = self._cmd()._unsafe_roots()
        for r in ("/app", "/srv/static_x", "/srv/media_x", "/srv/cred_x"):
            self.assertIn(os.path.realpath(r), roots)

    def test_validate_out_accepts_private_rejects_protected_traversal_and_symlink(self):
        cmd = self._cmd()
        protected = os.path.realpath(tempfile.mkdtemp())   # stands in for the repo/app root
        private = os.path.realpath(tempfile.mkdtemp())     # operator's private dir — a sibling, OUTSIDE protected
        self.addCleanup(shutil.rmtree, protected, ignore_errors=True)
        self.addCleanup(shutil.rmtree, private, ignore_errors=True)
        with mock.patch.object(cmd, "_unsafe_roots", return_value=[protected]):
            # ACCEPTED: a private file outside every protected root.
            self.assertEqual(cmd._validate_out(os.path.join(private, "fixture.json")),
                             os.path.join(private, "fixture.json"))
            # REJECTED: directly inside the protected root.
            with self.assertRaises(CommandError):
                cmd._validate_out(os.path.join(protected, "leak.json"))
            # REJECTED: a ``..`` traversal that resolves back into the protected root.
            with self.assertRaises(CommandError):
                cmd._validate_out(os.path.join(private, "..", os.path.basename(protected), "leak.json"))
            # REJECTED: a symlink whose realpath lands in the protected root.
            sneaky = os.path.join(private, "sneaky")
            os.symlink(protected, sneaky)
            with self.assertRaises(CommandError):
                cmd._validate_out(os.path.join(sneaky, "leak.json"))
