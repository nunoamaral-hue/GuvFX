"""Control-plane completion tests (2026-11-10) — revoke / reconcile commands + preflight.

Covers the supported single-authorization revoke, the expired-ACTIVE reconciliation, and the read-only demo
preflight. No broker contact, no host contact, no flag enablement.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from trade_copier.models import (CopierLiveAuthorization, CopierLiveAuthorizationStatus, CopierLiveKillSwitch)

User = get_user_model()


def _acct(owner, name, num, server, display, env="live"):
    from trading.models import BrokerServer, TradingAccount
    bs, _ = BrokerServer.objects.get_or_create(
        server_name=server, defaults={"broker_display_name": display, "environment": env})
    return TradingAccount.objects.create(
        user=owner, name=name, account_number=num, is_demo=False, is_active=True, broker_server=bs)


def _authz(owner, approver, macct, facct, *, status=CopierLiveAuthorizationStatus.ACTIVE, expired=False,
           minutes=15):
    now = timezone.now()
    exp = now - timedelta(minutes=1) if expired else now + timedelta(minutes=minutes)
    return CopierLiveAuthorization.objects.create(
        user=owner, master_account=macct, follower_account=facct,
        master_login=macct.account_number, master_server=macct.broker_server.server_name, master_environment="LIVE",
        follower_login=facct.account_number, follower_server=facct.broker_server.server_name, follower_environment="LIVE",
        source_symbol="XAUUSD", follower_symbol="XAUUSD.std", max_volume=Decimal("0.010"),
        max_concurrent_positions=1, max_orders=1, orders_consumed=0, valid_from=now, expires_at=exp,
        watermark_at=now, watermark_event_sequence=0, status=status, approved_by=approver,
        reason="test", modify_authority=True, full_close_authority=True)


class RevokeCommandTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("own", "own@guvfx.invalid", "x")
        self.other = User.objects.create_user("oth", "oth@guvfx.invalid", "x")
        self.staff = User.objects.create_user("stf", "stf@guvfx.invalid", "x", is_staff=True)
        self.nonstaff = User.objects.create_user("nst", "nst@guvfx.invalid", "x")
        self.m = _acct(self.owner, "M", "55442", "Tradersway-Live", "TW")
        self.f = _acct(self.owner, "F", "51763831", "IS6Technologies-Live", "IS6")
        self.other_f = _acct(self.other, "OF", "99999", "Other-Live", "OB")

    def _revoke(self, authz, *, owner="own@guvfx.invalid", master=None, follower=None,
                by="stf@guvfx.invalid", reason="cleanup"):
        out = StringIO()
        call_command("revoke_copier_live_authorization", authorization_id=authz.id, owner_email=owner,
                     master_account_id=master or self.m.id, follower_account_id=follower or self.f.id,
                     revoked_by=by, reason=reason, stdout=out)
        return out.getvalue()

    def _ks_before(self):
        return CopierLiveKillSwitch.objects.count()

    def _no_jobs(self):
        from execution.models import ExecutionJob
        return ExecutionJob.objects.count() == 0

    def test_expired_active_revoked(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=True)
        ks = self._ks_before()
        self._revoke(a)
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)
        self.assertIsNotNone(a.revoked_at)
        self.assertEqual(a.revoked_by_id, self.staff.id)
        self.assertEqual(CopierLiveKillSwitch.objects.count(), ks)   # kill switch unchanged
        self.assertTrue(self._no_jobs())

    def test_unexpired_active_revoked(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=False)
        self._revoke(a)
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)
        # scope preserved — only revocation fields changed
        self.assertEqual(a.max_volume, Decimal("0.010"))
        self.assertEqual(a.source_symbol, "XAUUSD")

    def test_mismatched_route_refused(self):
        a = _authz(self.owner, self.staff, self.m, self.f)
        with self.assertRaises(CommandError):
            self._revoke(a, follower=self.other_f.id)   # wrong follower account
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_wrong_owner_refused(self):
        a = _authz(self.owner, self.staff, self.m, self.f)
        with self.assertRaises(CommandError):
            self._revoke(a, owner="oth@guvfx.invalid")
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_nonstaff_approver_refused(self):
        a = _authz(self.owner, self.staff, self.m, self.f)
        with self.assertRaises(CommandError):
            self._revoke(a, by="nst@guvfx.invalid")
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_empty_reason_refused(self):
        a = _authz(self.owner, self.staff, self.m, self.f)
        with self.assertRaises(CommandError):
            self._revoke(a, reason="   ")

    def test_already_revoked_idempotent(self):
        a = _authz(self.owner, self.staff, self.m, self.f, status=CopierLiveAuthorizationStatus.REVOKED)
        a.revoked_at = timezone.now(); a.save(update_fields=["revoked_at"])
        prev = a.revoked_at
        self._revoke(a)   # no error, no-op
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)
        self.assertEqual(a.revoked_at, prev)   # unchanged

    def test_already_consumed_idempotent(self):
        a = _authz(self.owner, self.staff, self.m, self.f, status=CopierLiveAuthorizationStatus.CONSUMED)
        self._revoke(a)   # terminal -> no-op success
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.CONSUMED)   # NOT flipped to REVOKED

    def test_two_concurrent_revocations_second_is_noop(self):
        a = _authz(self.owner, self.staff, self.m, self.f)
        self._revoke(a)
        a.refresh_from_db()
        first = a.revoked_at
        self._revoke(a)   # second attempt: already terminal -> idempotent no-op
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)
        self.assertEqual(a.revoked_at, first)


class ReconcileCommandTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("own", "own@guvfx.invalid", "x")
        self.staff = User.objects.create_user("stf", "stf@guvfx.invalid", "x", is_staff=True)
        self.m = _acct(self.owner, "M", "55442", "Tradersway-Live", "TW")
        self.f = _acct(self.owner, "F", "51763831", "IS6Technologies-Live", "IS6")

    def test_dry_run_changes_nothing(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=True)
        call_command("reconcile_expired_copier_authorizations", stdout=StringIO())   # no --commit
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_commit_terminalises_expired_active(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=True)
        call_command("reconcile_expired_copier_authorizations", commit=True, stdout=StringIO())
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)
        self.assertIn("expired_window_reconciled", a.revoke_reason)

    def test_unexpired_active_untouched(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=False)
        call_command("reconcile_expired_copier_authorizations", commit=True, stdout=StringIO())
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.ACTIVE)

    def test_idempotent_second_commit_noop(self):
        a = _authz(self.owner, self.staff, self.m, self.f, expired=True)
        call_command("reconcile_expired_copier_authorizations", commit=True, stdout=StringIO())
        call_command("reconcile_expired_copier_authorizations", commit=True, stdout=StringIO())
        a.refresh_from_db()
        self.assertEqual(a.status, CopierLiveAuthorizationStatus.REVOKED)

    def test_kill_switch_and_jobs_unchanged(self):
        from execution.models import ExecutionJob
        a = _authz(self.owner, self.staff, self.m, self.f, expired=True)
        call_command("reconcile_expired_copier_authorizations", commit=True, stdout=StringIO())
        self.assertEqual(CopierLiveKillSwitch.objects.count(), 0)
        self.assertEqual(ExecutionJob.objects.count(), 0)


class PreflightCommandTests(TestCase):
    def test_preflight_reports_not_ready_and_mutates_nothing(self):
        owner = User.objects.create_user("own", "own@guvfx.invalid", "x")
        m = _acct(owner, "M", "55442", "Tradersway-Live", "TW")
        f = _acct(owner, "F", "51763831", "IS6Technologies-Live", "IS6")
        out = StringIO()
        call_command("copier_live_demo_preflight", master_account_id=m.id, follower_account_id=f.id,
                     source_symbol="XAUUSD", follower_symbol="XAUUSD.std", stdout=out)
        import json
        data = json.loads(out.getvalue())
        # host gates always force NOT_READY from a backend-only probe (fail-closed)
        self.assertEqual(data["verdict"], "NOT_READY")
        self.assertIn("host_bridge_watchdog", data["host_probe_required"])
        # read-only: nothing created
        self.assertEqual(CopierLiveAuthorization.objects.count(), 0)
        self.assertEqual(CopierLiveKillSwitch.objects.count(), 0)
        # workers gate sees the new commands
        self.assertEqual(data["gates"]["workers"]["code"], "WORKERS_OK")


class WorkerInertWhenOffTests(TestCase):
    """The new producer/dispatcher worker commands are no-ops (create NO ExecutionJob, contact no broker)
    with all copier flags OFF — the standing production posture."""

    def test_new_workers_noop_when_flags_off(self):
        from execution.models import ExecutionJob
        for cmd in ("run_copier_execute_closes", "run_copier_execute_modifies",
                    "run_copier_execute_partial_closes", "run_copier_mutation_dispatch"):
            out = StringIO()
            call_command(cmd, stdout=out)   # all flags default OFF
        self.assertEqual(ExecutionJob.objects.count(), 0)
        self.assertEqual(CopierLiveKillSwitch.objects.count(), 0)


class PreflightFollowerEndpointResolverTests(TestCase):
    """Regression (GFX-COPIER-PREFLIGHT-ENDPOINT-FIX-053): the follower-endpoint gate must call
    ``resolve_account_snapshot_base(account, *, global_base_url)`` with the KEYWORD-ONLY ``global_base_url``.
    The previous positional call raised ``TypeError`` for every follower, masking the true endpoint state as a
    spurious ``ENDPOINT_RESOLVE_ERROR`` (fail-closed, so never unsafe, but never truthful either). These tests
    pin the keyword call, the follower identity binding, truthful evaluation of a present endpoint, and — most
    importantly — that fail-closed readiness is preserved: absence/error never becomes a READY result, and the
    preflight still arms no flag, creates no authorization, dispatches no job, and relaxes no live/demo wall."""

    def _accounts(self):
        owner = User.objects.create_user("own", "own@guvfx.invalid", "x")
        m = _acct(owner, "M", "55442", "Tradersway-Live", "TW")
        f = _acct(owner, "F", "51763831", "IS6Technologies-Live", "IS6")
        return owner, m, f

    def _run(self, m, f, *, source="XAUUSD", follower="XAUUSD.std"):
        import json
        out = StringIO()
        call_command("copier_live_demo_preflight", master_account_id=m.id, follower_account_id=f.id,
                     source_symbol=source, follower_symbol=follower, stdout=out)
        return json.loads(out.getvalue())

    def test_real_resolver_no_typeerror_and_fail_closed_non_per_tenant(self):
        # REAL resolver, plain live follower with no per-tenant endpoint. The OLD positional call raised
        # TypeError here (-> ENDPOINT_RESOLVE_ERROR); the fix evaluates the gate truthfully and fail-closed.
        _owner, m, f = self._accounts()
        data = self._run(m, f)
        g = data["gates"]["follower_endpoint"]
        self.assertNotEqual(g["code"], "ENDPOINT_RESOLVE_ERROR")     # the exact bug symptom must be gone
        self.assertNotEqual(g.get("detail"), "TypeError")
        self.assertFalse(g["ok"])                                    # no per-tenant endpoint -> fail-closed
        self.assertEqual(g["code"], "ENDPOINT_NOT_PER_TENANT")
        self.assertEqual(data["verdict"], "NOT_READY")
        # no arming / no authorization / no job / no kill switch / no live->demo relaxation
        self.assertEqual(CopierLiveAuthorization.objects.count(), 0)
        self.assertEqual(CopierLiveKillSwitch.objects.count(), 0)
        from execution.models import ExecutionJob
        self.assertEqual(ExecutionJob.objects.count(), 0)
        f.refresh_from_db()
        self.assertFalse(f.is_demo)                                  # the live account is NOT relaxed to demo

    def test_resolver_called_keyword_only_and_bound_to_follower(self):
        _owner, m, f = self._accounts()
        from execution.snapshot_transport import SnapshotTransport, ST_PER_TENANT_OK
        spy = mock.Mock(return_value=SnapshotTransport(
            True, ST_PER_TENANT_OK, "http://10.50.0.48:8800", per_tenant=True))
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base", spy):
            data = self._run(m, f)
        self.assertEqual(spy.call_count, 1)
        # the keyword-only arg is passed BY NAME; a positional 2nd arg is exactly the regressed defect
        self.assertIn("global_base_url", spy.call_args.kwargs)
        self.assertEqual(len(spy.call_args.args), 1)                 # only the account is positional
        self.assertEqual(spy.call_args.args[0].id, f.id)            # identity-bound to the FOLLOWER (never master/sibling)
        # a present, READY per-tenant endpoint is evaluated normally -> ENDPOINT_OK (not an exception path)
        self.assertTrue(data["gates"]["follower_endpoint"]["ok"])
        self.assertEqual(data["gates"]["follower_endpoint"]["code"], "ENDPOINT_OK")

    def test_resolver_exception_stays_fail_closed_never_ready(self):
        # A genuine resolver error must remain a fail-closed gate, never converted into a READY/ok result.
        _owner, m, f = self._accounts()
        with mock.patch("execution.snapshot_transport.resolve_account_snapshot_base",
                        side_effect=RuntimeError("boom")):
            data = self._run(m, f)
        g = data["gates"]["follower_endpoint"]
        self.assertFalse(g["ok"])
        self.assertEqual(g["code"], "ENDPOINT_RESOLVE_ERROR")
        self.assertNotEqual(data["verdict"], "READY_TO_ARM")

    def test_route_account_symbol_restrictions_retained(self):
        # The #46->#48 identity/account/symbol pinning is unchanged by the fix.
        _owner, m, f = self._accounts()
        data = self._run(m, f)
        idg = data["gates"]["identity"]
        self.assertEqual(idg["master"]["id"], m.id)
        self.assertEqual(idg["master"]["server"], "Tradersway-Live")
        self.assertEqual(idg["follower"]["id"], f.id)
        self.assertEqual(idg["follower"]["server"], "IS6Technologies-Live")
        self.assertEqual(data["route"]["master"], m.id)
        self.assertEqual(data["route"]["follower"], f.id)
        self.assertEqual(data["route"]["source_symbol"], "XAUUSD")
        self.assertEqual(data["route"]["follower_symbol"], "XAUUSD.std")
