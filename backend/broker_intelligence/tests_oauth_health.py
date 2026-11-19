"""pkt-149 / ADR-0113 — tests for the value-blind mailbox OAuth health + alert model.

Covers the Phase-6 security matrix: every status, severity thresholds, synchronizer-stall, alert dedup/cooldown/
recovery, the no-new-mail non-alert, cross-owner isolation + staff gating of the operator endpoint, DARK 404, and —
load-bearing — that NO token / authorization code / cursor value / credential ref / full email address ever appears in
any health output. Pure-function cases use hand-built secret-free base dicts; integration cases build real
ConnectedMailbox rows and run the genuine ``health.mailbox_health`` → classifier path."""
from __future__ import annotations

import datetime
import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from broker_intelligence import health as H
from broker_intelligence import oauth_health as OH
from broker_intelligence.ingest_diagnostics import Code
from broker_intelligence.models import ConnectedMailbox

User = get_user_model()
_UTC = datetime.timezone.utc
_n = 0


def _uid():
    global _n
    _n += 1
    return _n


def _base(**kw) -> dict:
    """A secret-free ``mailbox_health``-shaped dict, healthy by default; override per case."""
    d = dict(
        mailbox_id=1, provider="GMAIL", status="CONNECTED", masked_email="al…@example.com",
        state=H.MAILBOX_SYNC_SUCCESSFUL, healthy=True, degraded=False, recovery_required=False,
        elapsed_since_sync_seconds=60, last_successful_sync="2026-11-16T00:00:00+00:00",
        last_attempted_sync="2026-11-16T00:00:00+00:00", last_error_code="", last_error_http_status=None,
        last_error_at=None, consecutive_failures=0, cursor_present=True, last_pass_cursor_advanced=True,
        stale_seconds=900, recovery_seconds=3600,
    )
    d.update(kw)
    return d


class ClassifyMatrix(TestCase):
    def test_healthy_recent_success(self):
        c = OH.classify_mailbox_oauth_health(_base())
        self.assertEqual(c["oauth_status"], OH.HEALTHY)
        self.assertEqual(c["alert_severity"], OH.SEV_OK)
        self.assertFalse(c["recovery_required"])

    def test_no_new_mail_cursor_not_advanced_is_healthy_not_an_alert(self):
        # The exact "connected, synced, but nothing new so the cursor did not move" case — must NOT alert.
        c = OH.classify_mailbox_oauth_health(_base(last_pass_cursor_advanced=False))
        self.assertEqual(c["oauth_status"], OH.HEALTHY)
        self.assertEqual(c["alert_severity"], OH.SEV_OK)

    def test_transient_5xx_fresh(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.MAILBOX_SYNC_FAILED, last_error_code=Code.GMAIL_HTTP_5XX, last_error_http_status=503,
            consecutive_failures=1, elapsed_since_sync_seconds=120))
        self.assertEqual(c["oauth_status"], OH.TRANSIENT_FAILURE)
        self.assertEqual(c["alert_severity"], OH.SEV_INFO)

    def test_transient_rate_limit_fresh(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.MAILBOX_SYNC_FAILED, last_error_code=Code.GMAIL_HTTP_429, consecutive_failures=1,
            elapsed_since_sync_seconds=120))
        self.assertEqual(c["oauth_status"], OH.TRANSIENT_FAILURE)

    def test_transient_escalates_to_degraded_by_count(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.MAILBOX_SYNC_FAILED, last_error_code=Code.GMAIL_HTTP_5XX, consecutive_failures=3,
            elapsed_since_sync_seconds=200))
        self.assertEqual(c["oauth_status"], OH.DEGRADED)
        self.assertEqual(c["alert_severity"], OH.SEV_WARNING)

    def test_transient_escalates_to_degraded_by_age(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.MAILBOX_STALE, last_error_code=Code.GMAIL_TIMEOUT, consecutive_failures=1,
            elapsed_since_sync_seconds=1000))   # > stale 900
        self.assertEqual(c["oauth_status"], OH.DEGRADED)
        self.assertEqual(c["alert_severity"], OH.SEV_DEGRADED)

    def test_degraded_escalates_to_critical_by_hour(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.RECOVERY_REQUIRED, last_error_code=Code.GMAIL_HTTP_5XX, consecutive_failures=5,
            elapsed_since_sync_seconds=5000))   # > recovery 3600
        self.assertEqual(c["oauth_status"], OH.DEGRADED)
        self.assertEqual(c["alert_severity"], OH.SEV_CRITICAL)

    def test_reconsent_from_permission_denied(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.RECOVERY_REQUIRED, last_error_code=Code.OAUTH_PERMISSION_DENIED, consecutive_failures=10,
            elapsed_since_sync_seconds=400))
        self.assertEqual(c["oauth_status"], OH.RECONSENT_REQUIRED)
        self.assertEqual(c["alert_severity"], OH.SEV_CRITICAL)
        self.assertTrue(c["reconsent_required"])
        self.assertIn("Reconnect required", c["recommended_action"])

    def test_reconsent_from_401_and_403(self):
        for code in (Code.GMAIL_HTTP_401, Code.GMAIL_HTTP_403):
            c = OH.classify_mailbox_oauth_health(_base(
                state=H.RECOVERY_REQUIRED, last_error_code=code, consecutive_failures=2,
                elapsed_since_sync_seconds=400))
            self.assertEqual(c["oauth_status"], OH.RECONSENT_REQUIRED, code)

    def test_reconsent_from_status_error(self):
        # status=ERROR ("Needs re-auth") is a genuine reconsent signal — the owner must re-authorize. (REVOKED is
        # split off below; see ADR-0118.)
        c = OH.classify_mailbox_oauth_health(_base(status="ERROR", state=H.RECOVERY_REQUIRED,
                                                   elapsed_since_sync_seconds=400))
        self.assertEqual(c["oauth_status"], OH.RECONSENT_REQUIRED)
        self.assertTrue(c["reconsent_required"])

    def test_revoked_is_owner_disconnected_not_reconsent(self):
        # pkt-158 / ADR-0118 regression: a member who clicks "disconnect" (the ONLY writer of status=REVOKED) is a
        # benign, intentional action. It must classify as OWNER_DISCONNECTED — NOT an incident-grade reconsent fault,
        # NOT a CRITICAL alert, NOT a recovery-required condition.
        c = OH.classify_mailbox_oauth_health(_base(status="REVOKED", state=H.RECOVERY_REQUIRED,
                                                   elapsed_since_sync_seconds=400))
        self.assertEqual(c["oauth_status"], OH.OWNER_DISCONNECTED)
        self.assertEqual(c["alert_severity"], OH.SEV_OK)
        self.assertFalse(c["recovery_required"])
        self.assertFalse(c["reconsent_required"])
        self.assertIn("No action", c["recommended_action"])

    def test_revoked_with_leftover_reauth_code_still_owner_disconnected(self):
        # Adversarial: even with a stale re-auth code on the row and a long elapsed, a member's deliberate disconnect
        # wins — OWNER_DISCONNECTED, SEV_OK, never escalated to CRITICAL by age.
        c = OH.classify_mailbox_oauth_health(_base(status="REVOKED", state=H.RECOVERY_REQUIRED,
                                                   last_error_code=Code.OAUTH_PERMISSION_DENIED,
                                                   consecutive_failures=12, elapsed_since_sync_seconds=9000))
        self.assertEqual(c["oauth_status"], OH.OWNER_DISCONNECTED)
        self.assertEqual(c["alert_severity"], OH.SEV_OK)
        self.assertFalse(c["recovery_required"])

    def test_security_config_codes(self):
        for code in (Code.OAUTH_CLIENT_NOT_CONFIGURED, Code.CREDENTIAL_MISSING, Code.CREDENTIAL_STORE_ERROR):
            c = OH.classify_mailbox_oauth_health(_base(
                state=H.RECOVERY_REQUIRED, last_error_code=code, elapsed_since_sync_seconds=400))
            self.assertEqual(c["oauth_status"], OH.SECURITY_CONFIGURATION_ERROR, code)
            self.assertEqual(c["alert_severity"], OH.SEV_CRITICAL)
            # A config fault is an OPERATOR action, never a (wrong) "reconnect your Gmail" instruction to the owner.
            self.assertFalse(c["reconsent_required"], code)

    def test_cursor_invalid_is_degraded_not_reconsent(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.RECOVERY_REQUIRED, last_error_code=Code.GMAIL_HISTORY_CURSOR_INVALID,
            elapsed_since_sync_seconds=400))
        self.assertEqual(c["oauth_status"], OH.DEGRADED)
        self.assertEqual(c["failure_class"], OH.FC_CURSOR_INVALID)
        self.assertFalse(c["reconsent_required"])
        self.assertIn("cursor", c["recommended_action"].lower())

    def test_bounded_message_and_db_codes_degraded(self):
        for code in (Code.MESSAGE_TOO_LARGE, Code.MESSAGE_CAPTURE_FAILED, Code.DATABASE_COMMIT_FAILED):
            c = OH.classify_mailbox_oauth_health(_base(
                state=H.MAILBOX_SYNC_FAILED, last_error_code=code, consecutive_failures=1,
                elapsed_since_sync_seconds=120))
            self.assertEqual(c["oauth_status"], OH.DEGRADED, code)

    def test_unknown_failure_and_unrecognised_fail_closed(self):
        for code in (Code.UNKNOWN_FAILURE, "SOMETHING_WE_DO_NOT_KNOW"):
            c = OH.classify_mailbox_oauth_health(_base(
                state=H.MAILBOX_SYNC_FAILED, last_error_code=code, consecutive_failures=1,
                elapsed_since_sync_seconds=120))
            self.assertEqual(c["oauth_status"], OH.UNKNOWN_FAIL_CLOSED, code)
            self.assertNotEqual(c["alert_severity"], OH.SEV_OK, code)   # fail-closed is never "OK"

    def test_stale_no_error_without_stall_is_degraded(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.MAILBOX_STALE, last_error_code="", consecutive_failures=0,
            elapsed_since_sync_seconds=1000), synchronizer_stalled=False)
        self.assertEqual(c["oauth_status"], OH.DEGRADED)

    def test_stale_no_error_with_stall_is_synchronizer_stalled(self):
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.RECOVERY_REQUIRED, last_error_code="", consecutive_failures=0,
            elapsed_since_sync_seconds=5000), synchronizer_stalled=True)
        self.assertEqual(c["oauth_status"], OH.SYNCHRONIZER_STALLED)
        self.assertEqual(c["alert_severity"], OH.SEV_CRITICAL)

    def test_output_is_value_blind(self):
        # No raw cursor, credential ref, token, or full email ever appears — only the masked identity + stable code.
        c = OH.classify_mailbox_oauth_health(_base(
            state=H.RECOVERY_REQUIRED, last_error_code=Code.OAUTH_PERMISSION_DENIED,
            elapsed_since_sync_seconds=400))
        self.assertNotIn("cursor_state", c)          # key never present (only cursor_present: bool)
        self.assertEqual(c["masked_email"], "al…@example.com")   # masked form only
        blob = json.dumps(c)                          # ensure_ascii escapes the ellipsis; the full local must be absent
        self.assertNotIn("alice", blob)
        for forbidden in ("credential_ref", "refresh_token", "access_token", "authorization_code", "client_secret"):
            self.assertNotIn(forbidden, blob)


class DecideAlert(TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 11, 16, 12, 0, 0, tzinfo=_UTC)

    def _healthy(self):
        return OH.classify_mailbox_oauth_health(_base())

    def _reconsent(self):
        return OH.classify_mailbox_oauth_health(_base(
            mailbox_id=7, state=H.RECOVERY_REQUIRED, last_error_code=Code.OAUTH_PERMISSION_DENIED,
            elapsed_since_sync_seconds=400))

    def _disconnected(self):
        return OH.classify_mailbox_oauth_health(_base(
            mailbox_id=9, status="REVOKED", state=H.RECOVERY_REQUIRED, elapsed_since_sync_seconds=400))

    def test_owner_disconnect_never_opens_an_alert(self):
        # pkt-158 regression: a benign member disconnect must NOT open an operator alert.
        self.assertEqual(OH.decide_alert(None, self._disconnected(), now=self.now)["action"], OH.ALERT_NONE)

    def test_owner_disconnect_after_alert_recovers_once(self):
        # A member disconnecting a previously-alerting (reconsent) mailbox CLEARS the alert with one recovery event,
        # never re-opens a new one.
        prev = {"dedup_key": OH.dedup_key(9, OH.RECONSENT_REQUIRED), "severity": OH.SEV_CRITICAL,
                "emitted_at": self.now - datetime.timedelta(minutes=10)}
        d = OH.decide_alert(prev, self._disconnected(), now=self.now)
        self.assertEqual(d["action"], OH.ALERT_RECOVER)
        self.assertEqual(d["severity"], OH.SEV_OK)

    def test_healthy_no_prev_is_none(self):
        d = OH.decide_alert(None, self._healthy(), now=self.now)
        self.assertEqual(d["action"], OH.ALERT_NONE)

    def test_healthy_after_alert_recovers_once(self):
        prev = {"dedup_key": OH.dedup_key(1, OH.DEGRADED), "severity": OH.SEV_DEGRADED,
                "emitted_at": self.now - datetime.timedelta(minutes=10)}
        d = OH.decide_alert(prev, self._healthy(), now=self.now)
        self.assertEqual(d["action"], OH.ALERT_RECOVER)
        self.assertEqual(d["severity"], OH.SEV_OK)

    def test_new_condition_opens(self):
        d = OH.decide_alert(None, self._reconsent(), now=self.now)
        self.assertEqual(d["action"], OH.ALERT_OPEN)

    def test_same_condition_within_cooldown_suppresses(self):
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": cur["alert_severity"],
                "emitted_at": self.now - datetime.timedelta(minutes=5)}
        d = OH.decide_alert(prev, cur, now=self.now, cooldown_seconds=30 * 60)
        self.assertEqual(d["action"], OH.ALERT_SUPPRESS)

    def test_same_condition_after_cooldown_reminds(self):
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": cur["alert_severity"],
                "emitted_at": self.now - datetime.timedelta(minutes=45)}
        d = OH.decide_alert(prev, cur, now=self.now, cooldown_seconds=30 * 60)
        self.assertEqual(d["action"], OH.ALERT_REMIND)

    def test_status_change_opens_new_not_suppressed(self):
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.TRANSIENT_FAILURE), "severity": OH.SEV_INFO,
                "emitted_at": self.now - datetime.timedelta(minutes=1)}
        d = OH.decide_alert(prev, cur, now=self.now)
        self.assertEqual(d["action"], OH.ALERT_OPEN)

    def test_severity_change_updates(self):
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": OH.SEV_WARNING,
                "emitted_at": self.now - datetime.timedelta(minutes=1)}
        d = OH.decide_alert(prev, cur, now=self.now)
        self.assertEqual(d["action"], OH.ALERT_UPDATE)

    def test_unusable_timestamp_suppresses_not_spams(self):
        # A present-but-unparseable emitted_at (a delivery-layer bug) must NOT re-emit every poll (anti-spam).
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": cur["alert_severity"],
                "emitted_at": "not-a-timestamp"}
        self.assertEqual(OH.decide_alert(prev, cur, now=self.now)["action"], OH.ALERT_SUPPRESS)

    def test_naive_vs_aware_timestamp_does_not_spam(self):
        # A tz-naive emitted_at against an aware now must be normalised (not forced to REMIND); within cooldown → SUPPRESS.
        cur = self._reconsent()
        naive = datetime.datetime(2026, 11, 16, 11, 55, 0)   # 5 min before self.now, but tz-naive
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": cur["alert_severity"],
                "emitted_at": naive}
        self.assertEqual(OH.decide_alert(prev, cur, now=self.now, cooldown_seconds=30 * 60)["action"], OH.ALERT_SUPPRESS)

    def test_missing_timestamp_reminds(self):
        # No prior timestamp at all → we cannot suppress forever; re-emit.
        cur = self._reconsent()
        prev = {"dedup_key": OH.dedup_key(7, OH.RECONSENT_REQUIRED), "severity": cur["alert_severity"]}
        self.assertEqual(OH.decide_alert(prev, cur, now=self.now)["action"], OH.ALERT_REMIND)


def _mb(user, *, status="CONNECTED", code="", consecutive=0, sync_age_s=120, attempt_age_s=120,
        local="alice", domain="example.com"):
    """Build a real ConnectedMailbox with migration-0011 health fields set to model a scenario."""
    now = timezone.now()
    last_sync = None if sync_age_s is None else now - datetime.timedelta(seconds=sync_age_s)
    last_attempt = None if attempt_age_s is None else now - datetime.timedelta(seconds=attempt_age_s)
    return ConnectedMailbox.objects.create(
        user=user, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id="pmid-%d" % _uid(),
        primary_email="%s@%s" % (local, domain), status=status, credential_ref="cred-ref-SECRET-%d" % _uid(),
        cursor_state="HIST-CURSOR-SECRET-%d" % _uid(), last_successful_sync=last_sync,
        last_attempted_sync=last_attempt, last_error_code=code, consecutive_failures=consecutive,
        last_pass_cursor_advanced=False)


class AggregateIntegration(TestCase):
    def setUp(self):
        self.u = User.objects.create_user(username="agg%d" % _uid(), email="agg%d@x.invalid" % _uid(), password="x")

    def test_healthy_and_reconsent_counts_and_blindness(self):
        _mb(self.u, status="CONNECTED", code="", sync_age_s=120, attempt_age_s=120, local="alice")
        _mb(self.u, status="CONNECTED", code=Code.OAUTH_PERMISSION_DENIED, consecutive=5, sync_age_s=4000,
            attempt_age_s=60, local="roberta")
        agg = OH.aggregate_oauth_health()
        self.assertEqual(agg["monitored_mailbox_count"], 2)
        self.assertEqual(agg["healthy_count"], 1)
        self.assertEqual(agg["reconsent_required_count"], 1)
        self.assertTrue(agg["any_reconsent_required"])
        self.assertEqual(agg["worst_alert_severity"], OH.SEV_CRITICAL)
        self.assertFalse(agg["synchronizer_stalled"])   # one mailbox attempted 60s ago → worker is running
        self.assertIsNotNone(agg["operator_notice"])
        blob = json.dumps(agg, default=str)
        for secret in ("alice@example.com", "roberta@example.com", "HIST-CURSOR-SECRET", "cred-ref-SECRET"):
            self.assertNotIn(secret, blob)

    def test_synchronizer_stalled_when_no_recent_attempt(self):
        # Two connected mailboxes, both last attempted 2h ago, no error, no recent success → worker is stalled.
        _mb(self.u, status="CONNECTED", code="", sync_age_s=8000, attempt_age_s=7200, local="alice")
        _mb(self.u, status="CONNECTED", code="", sync_age_s=8000, attempt_age_s=7200, local="bob")
        agg = OH.aggregate_oauth_health()
        self.assertTrue(agg["synchronizer_stalled"])
        statuses = {m["oauth_status"] for m in agg["mailboxes"]}
        self.assertEqual(statuses, {OH.SYNCHRONIZER_STALLED})
        self.assertEqual(agg["worst_alert_severity"], OH.SEV_CRITICAL)

    def test_all_healthy_has_no_operator_notice(self):
        _mb(self.u, status="CONNECTED", code="", sync_age_s=60, attempt_age_s=60)
        agg = OH.aggregate_oauth_health()
        self.assertIsNone(agg["operator_notice"])
        self.assertEqual(agg["worst_alert_severity"], OH.SEV_OK)

    def test_member_revoked_mailbox_is_benign_not_a_fault(self):
        # pkt-158 / ADR-0118 regression across the aggregate: one healthy + one member-revoked mailbox. The revoked
        # one is OWNER_DISCONNECTED, counted separately, and must NOT inflate reconsent / recovery / worst-severity
        # or raise an operator notice.
        _mb(self.u, status="CONNECTED", code="", sync_age_s=120, attempt_age_s=120, local="alice")
        _mb(self.u, status="REVOKED", code="", sync_age_s=4000, attempt_age_s=None, local="dave")
        agg = OH.aggregate_oauth_health()
        self.assertEqual(agg["monitored_mailbox_count"], 2)
        self.assertEqual(agg["healthy_count"], 1)
        self.assertEqual(agg["owner_disconnected_count"], 1)
        self.assertEqual(agg["reconsent_required_count"], 0)
        self.assertFalse(agg["any_reconsent_required"])
        self.assertFalse(agg["any_recovery_required"])
        self.assertEqual(agg["worst_alert_severity"], OH.SEV_OK)
        self.assertIsNone(agg["operator_notice"])
        self.assertEqual(agg["counts_by_status"]["OWNER_DISCONNECTED"], 1)


class SynchronizerStallDetector(TestCase):
    """pkt-149 adversarial-review fix: on an UNMIGRATED schema the attempt column is absent (age None for all), so a
    stall must NEVER be asserted — else healthy, actively-syncing mailboxes false-positive as SYNCHRONIZER_STALLED."""

    def _row(self, status="CONNECTED", attempt_age=None):
        return {"status": status, "elapsed_since_attempt_seconds": attempt_age}

    def test_recent_attempt_is_not_stalled(self):
        rows = [self._row(attempt_age=60), self._row(attempt_age=120)]
        self.assertFalse(OH._detect_synchronizer_stalled(rows, recovery_seconds=3600, r1_columns_present=True))

    def test_old_attempt_is_stalled(self):
        rows = [self._row(attempt_age=7200)]
        self.assertTrue(OH._detect_synchronizer_stalled(rows, recovery_seconds=3600, r1_columns_present=True))

    def test_no_connected_is_not_stalled(self):
        rows = [self._row(status="REVOKED", attempt_age=None)]
        self.assertFalse(OH._detect_synchronizer_stalled(rows, recovery_seconds=3600, r1_columns_present=True))

    def test_migrated_no_attempt_means_worker_never_ran(self):
        rows = [self._row(attempt_age=None), self._row(attempt_age=None)]
        self.assertTrue(OH._detect_synchronizer_stalled(rows, recovery_seconds=3600, r1_columns_present=True))

    def test_unmigrated_never_asserts_a_stall(self):
        # The column is absent → age None for every row → we CANNOT prove a stall → must return False.
        rows = [self._row(attempt_age=None), self._row(attempt_age=None)]
        self.assertFalse(OH._detect_synchronizer_stalled(rows, recovery_seconds=3600, r1_columns_present=False))


@override_settings(BROKER_INTELLIGENCE_INGEST_ENABLED="1")
class MailboxIngestProbeOwnerDisconnect(TestCase):
    """pkt-158 / ADR-0118 end-to-end regression: the operational-health probe (the surface that would OPEN the
    CRITICAL alert and degrade the whole system to MISCONFIGURED) must treat a member-revoked mailbox as benign."""

    def test_member_revoked_mailbox_does_not_fault_the_subsystem(self):
        from core.operational_health import probe_mailbox_ingest, HealthState
        u = User.objects.create_user(username="pr%d" % _uid(), email="pr%d@x.invalid" % _uid(), password="x")
        _mb(u, status="CONNECTED", code="", sync_age_s=120, attempt_age_s=120, local="alice")
        _mb(u, status="REVOKED", code="", sync_age_s=4000, attempt_age_s=None, local="dave")
        h = probe_mailbox_ingest()
        # The whole bug: a benign disconnect used to roll RECONSENT_REQUIRED → MISCONFIGURED (a FAULT). It must not.
        self.assertNotIn(h.state, HealthState.FAULTS)


class MailboxHealthCommandReconciliation(TestCase):
    """pkt-158 downstream completeness: the broker_mailbox_health CLI (a consumer of aggregate_oauth_health) must
    surface owner_disconnected_count so its summary reconciles, and a benign member disconnect must not trip the
    pkt-149 reconsent exit-gate."""

    def setUp(self):
        self.u = User.objects.create_user(username="cmd%d" % _uid(), email="cmd%d@x.invalid" % _uid(), password="x")

    def test_oauth_summary_surfaces_owner_disconnected_and_reconciles(self):
        _mb(self.u, status="CONNECTED", code="", sync_age_s=120, attempt_age_s=120, local="alice")
        _mb(self.u, status="REVOKED", code="", sync_age_s=4000, attempt_age_s=None, local="dave")
        out = StringIO()
        call_command("broker_mailbox_health", "--oauth-health", "--all-statuses", stdout=out)
        text = out.getvalue()
        self.assertIn("monitored=2", text)
        self.assertIn("owner_disconnected=1", text)        # the new count is surfaced (summary now reconciles)
        self.assertIn("reconsent_required=0", text)        # a benign disconnect is NOT a reconsent in the summary
        self.assertIn("oauth_status=OWNER_DISCONNECTED", text)

    def test_member_disconnect_does_not_trip_reconsent_exit_gate(self):
        _mb(self.u, status="REVOKED", code="", sync_age_s=4000, attempt_age_s=None, local="dave")
        out = StringIO()
        # Must NOT raise CommandError: a member disconnect is not a reconsent condition (would have before pkt-158).
        call_command("broker_mailbox_health", "--all-statuses", "--exit-nonzero-on-reconsent", stdout=out)
        self.assertIn("OWNER_DISCONNECTED", out.getvalue())


@override_settings(BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED="1")
class OperatorEndpoint(TestCase):
    URL = "/api/broker-intelligence/operator/mailbox-health/"

    def setUp(self):
        self.staff = User.objects.create_user(username="op%d" % _uid(), email="op%d@x.invalid" % _uid(),
                                               password="x", is_staff=True)
        self.member = User.objects.create_user(username="mem%d" % _uid(), email="mem%d@x.invalid" % _uid(),
                                                password="x")
        _mb(self.member, status="CONNECTED", code=Code.OAUTH_PERMISSION_DENIED, consecutive=3, sync_age_s=4000,
            attempt_age_s=60, local="carol")

    def test_staff_gets_projection(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        r = c.get(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reconsent_required_count"], 1)
        blob = json.dumps(r.json())
        self.assertNotIn("carol@example.com", blob)         # value-blind over the wire
        self.assertNotIn("HIST-CURSOR-SECRET", blob)

    def test_non_staff_forbidden(self):
        c = APIClient()
        c.force_authenticate(user=self.member)
        self.assertEqual(c.get(self.URL).status_code, 403)

    def test_unauthenticated_denied(self):
        self.assertIn(APIClient().get(self.URL).status_code, (401, 403))

    @override_settings(BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED="0")
    def test_dark_returns_404_even_for_staff(self):
        c = APIClient()
        c.force_authenticate(user=self.staff)
        self.assertEqual(c.get(self.URL).status_code, 404)
