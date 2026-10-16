"""INGESTION_INCIDENT_R1 (§7) — deterministic, synthetic regression suite for the ingestion diagnostic/health
subsystem. No network, no real Gmail, no credential decryption. Every test maps to a numbered §7 scenario (noted).

Covers: the secret-free failure classifier (§4), mailbox-level health observability (§5), the acquisition/cursor
safety contract (§6), per-mailbox isolation, idempotent retry, and the invariant that a successful service exit does
NOT imply mailbox health. Also asserts no secret/private-email data in logs and that no financial/trading path is
introduced."""
from __future__ import annotations

import datetime
import logging
import tempfile
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from broker_intelligence import gmail_source as GS
from broker_intelligence import health as H
from broker_intelligence import ingest_diagnostics as D
from broker_intelligence.credential_store import CredentialStoreError
from broker_intelligence.evidence import EvidenceStoreError
from broker_intelligence.gmail_oauth import GmailOAuthError
from broker_intelligence.ingest_diagnostics import BLOCKING_CODES, Code, Recovery, classify_exception
from broker_intelligence.mailbox_ingest import ingest_mailbox, run_mailbox_ingest
from broker_intelligence.models import ConnectedMailbox

User = get_user_model()


# ── fakes ──────────────────────────────────────────────────────────────────────────────────────────────────────
class _FakeClient:
    """A GmailClient stand-in. Either returns (messages, new_cursor) or raises a supplied exception from
    ``list_new_messages`` (simulating a fetch-layer failure) — never touches the network."""

    def __init__(self, messages=None, new_cursor="c2", raise_exc=None):
        self._messages = messages or []
        self._new_cursor = new_cursor
        self._raise = raise_exc

    def list_new_messages(self, cursor_state):
        if self._raise is not None:
            raise self._raise
        return list(self._messages), self._new_cursor


def _raw(frm="noreply@notbroker.invalid"):
    return ("From: %s\r\nTo: u@accounts.guvfx.com\r\nSubject: hello\r\n\r\nbody" % frm).encode("utf-8")


def _msg(frm="noreply@notbroker.invalid", auth="pass", mid="m1"):
    return {"raw_bytes": _raw(frm), "to_addresses": ("u@accounts.guvfx.com",), "from_address": frm,
            "subject": "hello", "body": "body", "received_at": timezone.now(),
            "provider_message_id": mid, "auth_verdict": auth}


_SEQ = [0]


def _mailbox(cursor="c1", **extra):
    _SEQ[0] += 1
    n = _SEQ[0]
    u = User.objects.create_user(username="diaguser%d" % n, email="diag%d@x.invalid" % n, password="x")
    return ConnectedMailbox.objects.create(
        user=u, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="pm%d" % n,
        primary_email="x@gmail.com", status=ConnectedMailbox.Status.CONNECTED, cursor_state=cursor, **extra)


# ── §7 scenarios 2–15, 23: the pure classifier ───────────────────────────────────────────────────────────────────
class ClassifyExceptionTests(TestCase):
    def test_s2_http_401_is_sponsor_reauth_blocking(self):
        d = classify_exception(GS.GmailApiError(401, "history.list"))
        self.assertEqual(d.code, Code.GMAIL_HTTP_401)
        self.assertEqual(d.http_status, 401)
        self.assertTrue(d.blocking)
        self.assertFalse(d.retryable)
        self.assertEqual(d.recovery, Recovery.SPONSOR_REAUTH)

    def test_s3_http_403_is_sponsor_reauth_blocking(self):
        d = classify_exception(GS.GmailApiError(403, "messages.get"))
        self.assertEqual(d.code, Code.GMAIL_HTTP_403)
        self.assertTrue(d.blocking)
        self.assertEqual(d.recovery, Recovery.SPONSOR_REAUTH)

    def test_s4_http_404_on_history_list_is_cursor_invalid_escalate(self):
        d = classify_exception(GS.GmailApiError(404, "history.list"))
        self.assertEqual(d.code, Code.GMAIL_HISTORY_CURSOR_INVALID)
        self.assertEqual(d.http_status, 404)
        self.assertEqual(d.operation, "history.list")
        self.assertTrue(d.blocking)
        self.assertFalse(d.retryable)
        self.assertEqual(d.recovery, Recovery.ESCALATE_CURSOR)

    def test_s5_http_404_on_messages_get_is_plain_404_not_cursor_invalid(self):
        d = classify_exception(GS.GmailApiError(404, "messages.get"))
        self.assertEqual(d.code, Code.GMAIL_HTTP_404)
        self.assertNotEqual(d.code, Code.GMAIL_HISTORY_CURSOR_INVALID)

    def test_s6_http_429_is_retryable_bounded(self):
        d = classify_exception(GS.GmailApiError(429, "history.list"))
        self.assertEqual(d.code, Code.GMAIL_HTTP_429)
        self.assertTrue(d.retryable)
        self.assertFalse(d.blocking)
        self.assertEqual(d.recovery, Recovery.RETRY_BOUNDED)

    def test_s7_http_5xx_is_retryable(self):
        for status in (500, 503):
            d = classify_exception(GS.GmailApiError(status, "history.list"))
            self.assertEqual(d.code, Code.GMAIL_HTTP_5XX)
            self.assertTrue(d.retryable)

    def test_http_400_and_other_are_operator_review(self):
        self.assertEqual(classify_exception(GS.GmailApiError(400, "messages.list")).code, Code.GMAIL_HTTP_400)
        self.assertEqual(classify_exception(GS.GmailApiError(418, "profile")).code, Code.GMAIL_HTTP_OTHER)

    def test_s8_missing_credential(self):
        d = classify_exception(GS.CredentialMissingError("no stored credential for mailbox"))
        self.assertEqual(d.code, Code.CREDENTIAL_MISSING)
        self.assertTrue(d.blocking)

    def test_credential_store_error(self):
        d = classify_exception(CredentialStoreError("decryption failed for credential: InvalidToken"))
        self.assertEqual(d.code, Code.CREDENTIAL_STORE_ERROR)
        self.assertTrue(d.blocking)

    def test_s9_oauth_refresh_failure_variants(self):
        self.assertEqual(classify_exception(GmailOAuthError("x", status=500)).code, Code.OAUTH_REFRESH_FAILED)
        self.assertEqual(classify_exception(GmailOAuthError("x")).code, Code.OAUTH_REFRESH_FAILED)
        # 400 invalid_grant / 401 / 403 => permission denied (needs re-auth), blocking.
        for status in (400, 401, 403):
            d = classify_exception(GmailOAuthError("x", status=status))
            self.assertEqual(d.code, Code.OAUTH_PERMISSION_DENIED)
            self.assertTrue(d.blocking)
            self.assertEqual(d.recovery, Recovery.SPONSOR_REAUTH)

    def test_s10_connection_timeout(self):
        self.assertEqual(classify_exception(GS.GmailTimeout("t")).code, Code.GMAIL_TIMEOUT)
        self.assertEqual(classify_exception(GS.GmailConnectionError("c")).code, Code.GMAIL_CONNECTION_ERROR)
        self.assertTrue(classify_exception(GS.GmailTimeout("t")).retryable)

    def test_s12_oversized_message(self):
        d = classify_exception(GS.MessageTooLargeError("too big"))
        self.assertEqual(d.code, Code.MESSAGE_TOO_LARGE)
        self.assertEqual(d.recovery, Recovery.RAISE_SIZE_CEILING)

    def test_s13_empty_payload_capture_failed(self):
        d = classify_exception(GS.MessageCaptureError("empty raw"))
        self.assertEqual(d.code, Code.MESSAGE_CAPTURE_FAILED)
        self.assertEqual(d.recovery, Recovery.PRESERVE_AND_RETRY)

    def test_too_large_is_distinct_from_plain_capture(self):
        # MessageTooLargeError subclasses MessageCaptureError; the subclass must classify FIRST (not collapse to
        # MESSAGE_CAPTURE_FAILED).
        self.assertEqual(classify_exception(GS.MessageTooLargeError("x")).code, Code.MESSAGE_TOO_LARGE)

    def test_s14_database_commit_failure(self):
        self.assertEqual(classify_exception(OperationalError("db gone")).code, Code.DATABASE_COMMIT_FAILED)

    def test_evidence_store_error_maps_to_durable_persistence(self):
        # EvidenceStoreError subclasses RuntimeError; it must be caught BEFORE the generic RuntimeError->UNKNOWN path.
        self.assertEqual(classify_exception(EvidenceStoreError("fs full")).code, Code.DATABASE_COMMIT_FAILED)

    def test_s15_parser_execution_error(self):
        self.assertEqual(classify_exception(D.ParserExecutionError("boom")).code, Code.MESSAGE_PARSE_FAILED)

    def test_s23_unknown_and_bare_runtimeerror_never_inferred(self):
        self.assertEqual(classify_exception(ValueError("boom")).code, Code.UNKNOWN_FAILURE)
        # §4: a bare RuntimeError whose *message* looks like an HTTP error must NOT be inferred to an HTTP code.
        d = classify_exception(RuntimeError("gmail api http 401"))
        self.assertEqual(d.code, Code.UNKNOWN_FAILURE)
        self.assertIsNone(d.http_status)
        self.assertFalse(d.blocking)
        self.assertEqual(d.recovery, Recovery.OPERATOR_REVIEW)

    def test_safe_dict_is_stable_and_secret_free(self):
        d = classify_exception(GS.GmailApiError(401, "history.list"))
        sd = d.safe_dict()
        self.assertEqual(set(sd), {"code", "http_status", "operation", "retryable", "blocking", "recovery"})
        # No free-text message leaks through.
        self.assertNotIn("message", sd)

    def test_blocking_codes_membership(self):
        for c in (Code.GMAIL_HTTP_401, Code.GMAIL_HTTP_403, Code.GMAIL_HISTORY_CURSOR_INVALID,
                  Code.OAUTH_PERMISSION_DENIED, Code.CREDENTIAL_MISSING, Code.CREDENTIAL_STORE_ERROR,
                  Code.OAUTH_CLIENT_NOT_CONFIGURED):
            self.assertIn(c, BLOCKING_CODES)
        self.assertNotIn(Code.GMAIL_HTTP_429, BLOCKING_CODES)
        self.assertNotIn(Code.GMAIL_HTTP_5XX, BLOCKING_CODES)


# ── §7 scenarios 1, 14, 16, 17, 18, 19: the worker ───────────────────────────────────────────────────────────────
class WorkerIntegrationTests(TestCase):
    def test_s1_successful_pass_advances_cursor_and_records_healthy(self):
        mb = _mailbox(cursor="c1")
        res = ingest_mailbox(mb, client=_FakeClient(messages=[_msg()], new_cursor="c2"))
        self.assertTrue(res["ok"])
        self.assertTrue(res["cursor_advanced"])
        self.assertIsNone(res["diagnostic"])
        mb.refresh_from_db()
        self.assertEqual(mb.cursor_state, "c2")
        self.assertEqual(mb.consecutive_failures, 0)
        self.assertEqual(mb.last_error_code, "")
        self.assertIsNotNone(mb.last_attempted_sync)
        self.assertTrue(mb.last_pass_cursor_advanced)
        self.assertEqual(H.mailbox_health(mb)["state"], H.MAILBOX_SYNC_SUCCESSFUL)

    def test_s18_cursor_unchanged_after_fetch_failure_and_diagnostic_recorded(self):
        mb = _mailbox(cursor="c1")
        res = ingest_mailbox(mb, client=_FakeClient(raise_exc=GS.GmailApiError(401, "history.list")))
        self.assertFalse(res["ok"])
        self.assertEqual(res["diagnostic"]["code"], Code.GMAIL_HTTP_401)
        mb.refresh_from_db()
        self.assertEqual(mb.cursor_state, "c1")                 # §6: cursor NOT advanced on failure
        self.assertEqual(mb.last_error_code, Code.GMAIL_HTTP_401)
        self.assertEqual(mb.last_error_http_status, 401)
        self.assertEqual(mb.consecutive_failures, 1)
        self.assertFalse(mb.last_pass_cursor_advanced)

    def test_s11_history_cursor_invalid_is_recorded_not_auto_recovered(self):
        mb = _mailbox(cursor="3204241")
        res = ingest_mailbox(mb, client=_FakeClient(raise_exc=GS.GmailApiError(404, "history.list")))
        self.assertFalse(res["ok"])
        self.assertEqual(res["diagnostic"]["code"], Code.GMAIL_HISTORY_CURSOR_INVALID)
        self.assertEqual(res["diagnostic"]["recovery"], Recovery.ESCALATE_CURSOR)
        mb.refresh_from_db()
        self.assertEqual(mb.cursor_state, "3204241")            # §6: never auto-anchored to the profile historyId
        self.assertEqual(H.mailbox_health(mb)["state"], H.RECOVERY_REQUIRED)

    def test_consecutive_failures_accumulate_then_reset_on_success(self):
        mb = _mailbox(cursor="c1")
        ingest_mailbox(mb, client=_FakeClient(raise_exc=GS.GmailApiError(500, "history.list")))
        mb.refresh_from_db()
        ingest_mailbox(mb, client=_FakeClient(raise_exc=GS.GmailApiError(500, "history.list")))
        mb.refresh_from_db()
        self.assertEqual(mb.consecutive_failures, 2)
        ingest_mailbox(mb, client=_FakeClient(messages=[], new_cursor="c2"))
        mb.refresh_from_db()
        self.assertEqual(mb.consecutive_failures, 0)
        self.assertEqual(mb.last_error_code, "")

    def test_s14_commit_db_failure_holds_cursor(self):
        from unittest import mock
        mb = _mailbox(cursor="c1")
        with mock.patch.object(GS.GmailMailSource, "commit_cursor", side_effect=OperationalError("db gone")):
            res = ingest_mailbox(mb, client=_FakeClient(messages=[_msg()], new_cursor="c2"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["diagnostic"]["code"], Code.DATABASE_COMMIT_FAILED)
        mb.refresh_from_db()
        self.assertEqual(mb.cursor_state, "c1")                 # not advanced when the commit failed

    def test_s16_s17_single_mailbox_failure_does_not_abort_others(self):
        from unittest import mock
        bad = _mailbox(cursor="b1")
        good = _mailbox(cursor="g1")

        real = ingest_mailbox

        def _patched(mailbox, **kw):
            if mailbox.id == bad.id:
                return real(mailbox, client=_FakeClient(raise_exc=GS.GmailApiError(401, "history.list")))
            return real(mailbox, client=_FakeClient(messages=[], new_cursor="g2"))

        with mock.patch("broker_intelligence.mailbox_ingest.ingest_mailbox", side_effect=_patched):
            summary = run_mailbox_ingest()
        self.assertEqual(summary["mailboxes"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["failure_codes"].get(Code.GMAIL_HTTP_401), 1)
        good.refresh_from_db()
        self.assertEqual(good.cursor_state, "g2")               # the healthy mailbox still advanced

    @override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="broker.invalid")
    def test_s19_no_duplicate_evidence_after_safe_retry(self):
        from broker_intelligence.models import EvidenceBlob
        root = tempfile.mkdtemp(prefix="bi-r1-")
        with override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=root):
            mb = _mailbox(cursor="c1")
            m = _msg(frm="payments@broker.invalid", auth="pass", mid="dup1")
            ingest_mailbox(mb, client=_FakeClient(messages=[m], new_cursor="c2"))
            mb.refresh_from_db()
            # Re-deliver the SAME raw message (idempotent by content hash) on a second pass.
            ingest_mailbox(mb, client=_FakeClient(messages=[m], new_cursor="c3"))
            self.assertEqual(EvidenceBlob.objects.count(), 1)   # content-addressed dedup -> exactly one blob


# ── §7 scenarios 20, 21: health read-model ───────────────────────────────────────────────────────────────────────
class MailboxHealthTests(TestCase):
    def _now(self):
        return timezone.now()

    def test_s20_staleness_detected(self):
        now = self._now()
        fresh = SimpleNamespace(id=1, provider="GMAIL", status="CONNECTED",
                                last_successful_sync=now - datetime.timedelta(seconds=60), cursor_state="c",
                                last_error_code="", consecutive_failures=0, masked_email=lambda: "a…@x")
        stale = SimpleNamespace(id=2, provider="GMAIL", status="CONNECTED",
                                last_successful_sync=now - datetime.timedelta(minutes=30), cursor_state="c",
                                last_error_code="", consecutive_failures=0, masked_email=lambda: "a…@x")
        dead = SimpleNamespace(id=3, provider="GMAIL", status="CONNECTED",
                               last_successful_sync=now - datetime.timedelta(hours=35), cursor_state="c",
                               last_error_code="", consecutive_failures=0, masked_email=lambda: "a…@x")
        self.assertEqual(H.mailbox_health(fresh, now=now)["state"], H.MAILBOX_SYNC_SUCCESSFUL)
        self.assertEqual(H.mailbox_health(stale, now=now)["state"], H.MAILBOX_STALE)
        self.assertEqual(H.mailbox_health(dead, now=now)["state"], H.RECOVERY_REQUIRED)

    def test_blocking_code_forces_recovery_even_if_recent(self):
        now = self._now()
        mb = SimpleNamespace(id=1, provider="GMAIL", status="CONNECTED",
                             last_successful_sync=now - datetime.timedelta(seconds=30), cursor_state="c",
                             last_error_code=Code.GMAIL_HISTORY_CURSOR_INVALID, consecutive_failures=1,
                             masked_email=lambda: "a…@x")
        self.assertEqual(H.mailbox_health(mb, now=now)["state"], H.RECOVERY_REQUIRED)

    def test_recent_nonblocking_failure_is_sync_failed(self):
        now = self._now()
        mb = SimpleNamespace(id=1, provider="GMAIL", status="CONNECTED",
                             last_successful_sync=now - datetime.timedelta(seconds=30), cursor_state="c",
                             last_error_code=Code.GMAIL_HTTP_5XX, consecutive_failures=1, masked_email=lambda: "a…@x")
        self.assertEqual(H.mailbox_health(mb, now=now)["state"], H.MAILBOX_SYNC_FAILED)

    def test_mailbox_health_without_error_fields_uses_staleness_only(self):
        # Unit-level: include_error_fields=False (the unmigrated call) classifies from staleness alone, never reading
        # an R1 attribute.
        now = self._now()
        legacy = SimpleNamespace(id=9, provider="GMAIL", status="CONNECTED",
                                 last_successful_sync=now - datetime.timedelta(hours=35), cursor_state="c",
                                 masked_email=lambda: "a…@x")
        h = H.mailbox_health(legacy, now=now, include_error_fields=False)
        self.assertEqual(h["state"], H.RECOVERY_REQUIRED)
        self.assertEqual(h["consecutive_failures"], 0)
        self.assertIsNone(h["last_error_http_status"])

    def test_s21_service_success_does_not_imply_mailbox_health(self):
        mb = _mailbox(cursor="c1")
        mb.last_successful_sync = timezone.now() - datetime.timedelta(hours=35)
        mb.save(update_fields=["last_successful_sync"])
        report = H.service_health()
        self.assertIn("does NOT imply", report["service_running_note"])
        self.assertTrue(report["any_recovery_required"])
        self.assertTrue(report["any_degraded"])


# ── §7 scenarios 22, 24, 25: secrecy, flag stability, no financial path ───────────────────────────────────────────
class SecrecyAndSafetyTests(TestCase):
    def test_s22_failure_log_is_secret_free(self):
        mb = _mailbox(cursor="c1")
        with self.assertLogs("guvfx.broker_intelligence.mailbox_ingest", level="WARNING") as cm:
            ingest_mailbox(mb, client=_FakeClient(raise_exc=GS.GmailApiError(401, "history.list")))
        blob = "\n".join(cm.output)
        self.assertIn("code=GMAIL_HTTP_401", blob)
        # Must NOT leak the mailbox address, a token, a URL, the API host, a message id, or a subject. (The stable
        # field name "http_status" and the code "GMAIL_HTTP_401" are deliberately present and are NOT secrets.)
        for secret in ("x@gmail.com", "googlemail", "Bearer", "https://", "http://", "googleapis",
                       "access_token", "refresh_token", "Subject:", "hello"):
            self.assertNotIn(secret, blob)

    def test_s24_dark_flags_unchanged(self):
        from broker_intelligence import flags
        # This change must not flip any DARK default on.
        self.assertFalse(flags.broker_intelligence_ingest_enabled())
        for name in ("broker_withdrawal_correlation_enabled", "broker_withdrawal_ux_enabled"):
            fn = getattr(flags, name, None)
            if fn is not None:
                self.assertFalse(fn())

    def test_s25_no_financial_or_trading_import_path(self):
        import broker_intelligence.ingest_diagnostics as a
        import broker_intelligence.health as b
        import broker_intelligence.mailbox_ingest as c
        import inspect
        forbidden = ("execution", "strategies", "mt5", "order", "place_order", "trade_ingest")
        for mod in (a, b, c):
            src = inspect.getsource(mod)
            for tok in forbidden:
                self.assertNotIn("import %s" % tok, src)
                self.assertNotIn("from %s" % tok, src)


class UnmigratedHealthTests(TransactionTestCase):
    """NON-VACUOUS unmigrated-safety (regression for the review HIGH). Runs WITHOUT an enclosing transaction
    (TransactionTestCase), so the committed INSERT leaves no pending trigger events and the ALTER TABLE DROP COLUMN is
    clean. Drops the 6 R1 columns from the REAL table, then runs service_health() through the ACTUAL ORM query: it must
    NOT raise and must still flag the ~35h-stale mailbox as RECOVERY_REQUIRED (the exact incident state the diagnostic
    exists to reveal). The previous SimpleNamespace test never exercised the ORM SELECT and hid this."""

    def test_service_health_survives_unmigrated_db_real_orm_path(self):
        mb = _mailbox(cursor="c1")
        mb.last_successful_sync = timezone.now() - datetime.timedelta(hours=35)
        mb.save(update_fields=["last_successful_sync"])
        r1_names = ["last_attempted_sync", "last_pass_cursor_advanced", "last_error_code",
                    "last_error_http_status", "last_error_at", "consecutive_failures"]
        fields = [ConnectedMailbox._meta.get_field(n) for n in r1_names]
        dropped = []
        try:
            with connection.schema_editor() as se:
                for f in fields:
                    se.remove_field(ConnectedMailbox, f)
                    dropped.append(f)
            self.assertFalse(H.r1_columns_present())            # introspection sees the columns gone
            report = H.service_health()                         # MUST NOT raise against the column-less table
            self.assertFalse(report["r1_columns_present"])
            self.assertTrue(report["any_recovery_required"])
            self.assertEqual(report["mailboxes"][0]["state"], H.RECOVERY_REQUIRED)
        finally:
            with connection.schema_editor() as se:              # restore the schema for subsequent tests
                for f in dropped:
                    se.add_field(ConnectedMailbox, f)
