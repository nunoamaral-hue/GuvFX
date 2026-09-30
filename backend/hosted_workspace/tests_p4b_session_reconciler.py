"""P4-b: the cold-boot SESSION recovery reconciler (session_reconciler.run_hosted_session_reconciler).

Proves the PINNED desired-state table, the two INDEPENDENT darkness gates (probe-only vs armed self-connect), the
claim-is-permission-not-proof ordering, the server-derived gate, CZ+18 exclusion, bounds/cooldown, and fail-closed
classification (ok:false / missing / out-of-enum / transport => UNKNOWN, never ABSENT). No host, injected seams.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from execution import readiness as R
from execution.models import TerminalNode
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import session_reconciler as SR
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

RECON = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_SESSION_RECONCILER_ENABLED="1")
ARMED = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_SESSION_RECONCILER_ENABLED="1",
             HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED="1")


def _uniq():
    global _n
    _n += 1
    return f"97{_n:04d}"


def _account(*, account_id=None, is_demo=True, confirmed=True, rdp_host="100.79.101.19"):
    login = _uniq()
    # Force a HIGH, unique, NON-reserved id by default so the first account created in an isolated test run is never
    # auto-assigned id 1 (Customer Zero) or 18 (which the reconciler correctly excludes) - that would make an
    # otherwise-valid candidate vanish and mask a real assertion. Tests that WANT a reserved id pass it explicitly.
    if account_id is None:
        account_id = 500_000 + _n
    user = U.objects.create_user(username=f"sr{login}", email=f"{login}@x.invalid", password="x")
    srv, _ = BrokerServer.objects.get_or_create(server_name="IS6-Demo")
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE, rdp_host=rdp_host)
    kw = dict(id=account_id, user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo,
              is_active=True, broker_server=srv, readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node,
              workspace_confirmed_at=(timezone.now() if confirmed else None))
    return TradingAccount.objects.create(**kw), node


def _armed_ws(acct, node, *, armed=True, matched=True, stale=True, **kw):
    ts = (timezone.now() - timezone.timedelta(seconds=R.WORKSPACE_OBSERVATION_FRESH_SECONDS + 60)
          if stale else timezone.now())
    base = dict(canonical_state=S.EXECUTION_READY, proj_connected=True, proj_account_match=matched,
                proj_trade_allowed=True, last_decision_at=ts, execution_node=node, workspace_node=node,
                execution_enabled=armed, execution_authorized_at=(timezone.now() if armed else None))
    base.update(kw)
    return HostedMt5Workspace.objects.create(trading_account=acct, **base)


class _Ex:
    """A resolvable executor stand-in (its probe_session is not used when a probe_fn is injected)."""
    def probe_session(self, rdp_host=None):
        return {"ok": True, "session_status": "UNKNOWN"}


def _resolver(ex=None):
    ex = ex or _Ex()
    return lambda account_id, rdp_host: ex


def _probe(status):
    """An injected probe_fn returning a given canonical status (or a raw dict to test normalisation)."""
    if isinstance(status, dict):
        return lambda ws: status
    return lambda ws: {"ok": True, "session_status": status}


def _recording_establish():
    calls = []

    def establish(ws, account, node, status):
        calls.append({"account_id": account.id, "status": status})
        return {"ok": True}
    return establish, calls


class DarkTests(TestCase):
    def test_dark_when_flags_off(self):
        acct, node = _account()
        _armed_ws(acct, node)
        est, calls = _recording_establish()
        out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                               establish_fn=est)
        self.assertFalse(out["enabled"])
        self.assertEqual(calls, [])

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_SESSION_RECONCILER_ENABLED="0")
    def test_dark_when_only_master_on(self):
        acct, node = _account()
        _armed_ws(acct, node)
        est, calls = _recording_establish()
        out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                               establish_fn=est)
        self.assertFalse(out["enabled"])
        self.assertEqual(calls, [])


@override_settings(**RECON)
class ProbeOnlyTests(TestCase):
    """Reconciler ON but self-connect NOT armed: select/probe/classify/audit, ZERO self-connect, ZERO claim."""

    def _run(self, status, est=None):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        est_fn, calls = (est if est else _recording_establish())
        out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe(status),
                                               establish_fn=est_fn)
        ws.refresh_from_db()
        return out, calls, ws

    def test_absent_is_probe_only_no_selfconnect_no_claim(self):
        out, calls, ws = self._run("ABSENT")
        self.assertTrue(out["enabled"])
        self.assertFalse(out["self_connect_armed"])
        self.assertEqual(out["absent"], 1)
        self.assertEqual(out["skipped_not_armed"], 1)
        self.assertEqual(out["established"], 0)
        self.assertEqual(calls, [])                       # ZERO self-connect
        self.assertEqual(ws.session_recovery_count, 0)    # ZERO claim (permission-to-attempt not consumed)

    def test_disconnected_is_probe_only(self):
        out, calls, ws = self._run("DISCONNECTED")
        self.assertEqual(out["disconnected"], 1)
        self.assertEqual(out["skipped_not_armed"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)

    def test_active_converges_no_action(self):
        out, calls, ws = self._run("ACTIVE")
        self.assertEqual(out["active"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)

    def test_unknown_fails_closed(self):
        out, calls, ws = self._run("UNKNOWN")
        self.assertEqual(out["unknown"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)


@override_settings(**ARMED)
class ArmedSelfConnectTests(TestCase):
    """Reconciler + arm sub-gate ON, establish_fn injected, delivery gate mocked open: the pinned action table."""

    def _run(self, status):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe(status),
                                                   establish_fn=est)
        ws.refresh_from_db()
        return out, calls, ws

    def test_absent_establishes_and_claims(self):
        out, calls, ws = self._run("ABSENT")
        self.assertTrue(out["self_connect_armed"])
        self.assertEqual(out["established"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "ABSENT")     # establish path
        self.assertEqual(ws.session_recovery_count, 1)     # claim consumed for the attempt

    def test_disconnected_reconnects_no_new_session(self):
        out, calls, ws = self._run("DISCONNECTED")
        self.assertEqual(out["reconnected"], 1)
        self.assertEqual(out["established"], 0)            # NOT counted as a fresh establish
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "DISCONNECTED")  # reconnect path; driver rejoins, never a 2nd session
        self.assertEqual(ws.session_recovery_count, 1)

    def test_active_never_establishes(self):
        out, calls, ws = self._run("ACTIVE")
        self.assertEqual(out["active"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)

    def test_unknown_never_establishes_even_when_armed(self):
        out, calls, ws = self._run("UNKNOWN")
        self.assertEqual(out["unknown"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)     # claim is not consumed for a fail-closed classification

    def test_ok_false_probe_is_unknown_never_absent(self):
        acct, node = _account()
        _armed_ws(acct, node)
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(),
                                                   probe_fn=_probe({"ok": False, "reason": "host_unavailable"}),
                                                   establish_fn=est)
        self.assertEqual(out["unknown"], 1)
        self.assertEqual(out["absent"], 0)                 # ok:false is UNKNOWN, NEVER ABSENT
        self.assertEqual(calls, [])

    def test_missing_and_out_of_enum_status_are_unknown(self):
        # Each raw runs in its own isolated DB state (one candidate) so the counts are unambiguous.
        for raw in ({"ok": True}, {"ok": True, "session_status": "WeirdState"}):
            with self.subTest(raw=raw):
                acct, node = _account()
                ws = _armed_ws(acct, node)
                est, calls = _recording_establish()
                with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
                    out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe(raw),
                                                           establish_fn=est)
                self.assertEqual(out["unknown"], 1, raw)
                self.assertEqual(out["absent"], 0, raw)   # missing/out-of-enum is UNKNOWN, NEVER ABSENT
                self.assertEqual(calls, [])
                ws.delete()                                # isolate: next raw sees a clean candidate set

    def test_delivery_gate_closed_blocks_establish_no_claim(self):
        acct, node = _account()
        ws = _armed_ws(acct, node)
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=False):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                   establish_fn=est)
        ws.refresh_from_db()
        self.assertEqual(out["skipped_gate"], 1)
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)     # no attempt claimed when the gate fails

    def test_cooldown_exhausted_blocks_establish_and_alerts(self):
        from reliability.models import AlertEvent
        acct, node = _account()
        _armed_ws(acct, node, session_recovery_count=SR.MAX_RECOVERY_ATTEMPTS,
                  session_recovery_at=timezone.now())
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                   establish_fn=est)
        self.assertEqual(out["skipped_cooldown"], 1)
        self.assertEqual(calls, [])
        self.assertTrue(AlertEvent.objects.filter(dedup_key=SR._alert_dedup_key(acct.id),
                                                  status=AlertEvent.Status.OPEN).exists())

    def test_node_divergence_fails_closed(self):
        # F1: if execution_node != workspace_node, we cannot know the probed host == the establish host -> fail
        # closed (never probe/establish a split host, which could create a session over a live one on the other).
        acct, node = _account()
        other = TerminalNode.objects.create(hostname="n-other", status=TerminalNode.Status.ACTIVE,
                                            rdp_host="100.79.101.99")
        ws = _armed_ws(acct, node)
        ws.execution_node = other      # diverge from workspace_node (still == node)
        ws.save(update_fields=["execution_node"])
        probed = []
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(
                executor_resolver=_resolver(),
                probe_fn=lambda w: probed.append(1) or {"ok": True, "session_status": "ABSENT"},
                establish_fn=est)
        ws.refresh_from_db()
        self.assertEqual(out["skipped_gate"], 1)
        self.assertEqual(probed, [])                   # never even probed a divergent-node workspace
        self.assertEqual(calls, [])
        self.assertEqual(ws.session_recovery_count, 0)

    def test_active_resets_recovery_counter(self):
        # F2: a confirmed-ACTIVE session clears the per-lifetime count so a later reboot has a fresh budget.
        acct, node = _account()
        ws = _armed_ws(acct, node, session_recovery_count=2, session_recovery_at=timezone.now())
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ACTIVE"),
                                                   establish_fn=_recording_establish()[0])
        ws.refresh_from_db()
        self.assertEqual(out["active"], 1)
        self.assertEqual(ws.session_recovery_count, 0)
        self.assertIsNone(ws.session_recovery_at)


class RealDeliveryGateTests(TestCase):
    """Close the F1 blind spot: exercise the REAL workspace_delivery_ready (not a mock) with a full deliverable
    fixture, so the gate<->node<->establish path is proven end to end, not stubbed."""

    @override_settings(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_SESSION_RECONCILER_ENABLED="1",
                       HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED="1", HOSTED_MT5_REMOTEAPP_ENABLED="1")
    def test_absent_establishes_through_real_gate(self):
        import os
        from terminal_provisioning.models import AccountProvisioning
        acct, node = _account()
        ws = _armed_ws(acct, node)
        AccountProvisioning.objects.create(
            trading_account=acct, windows_username=f"guvfx_u_{acct.id}", password_enc="fernet-blob-not-real",
            is_admin=False, runtime_root=rf"C:\GuvFX\accounts\{acct.id}",
            status=AccountProvisioning.Status.PROVISIONED)
        est, calls = _recording_establish()
        with mock.patch.dict(os.environ, {"GUAC_BASE_URL": "https://guac.invalid/guacamole",
                                          "GUAC_JSON_SECRET_KEY_HEX": "00" * 16}):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=_probe("ABSENT"),
                                                   establish_fn=est)
        ws.refresh_from_db()
        self.assertEqual(out["established"], 1, out)     # real gate passed -> establish fired
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "ABSENT")
        self.assertEqual(ws.session_recovery_count, 1)


@override_settings(**ARMED)
class SelectionGuardTests(TestCase):
    def test_customer_zero_and_18_excluded(self):
        probed = []

        def probe_fn(ws):
            probed.append(ws.trading_account_id)
            return {"ok": True, "session_status": "ABSENT"}
        for aid in (1, 18):
            acct, node = _account(account_id=aid)
            _armed_ws(acct, node)
        est, calls = _recording_establish()
        with mock.patch.object(SR, "workspace_delivery_ready", return_value=True):
            out = SR.run_hosted_session_reconciler(executor_resolver=_resolver(), probe_fn=probe_fn,
                                                   establish_fn=est)
        self.assertEqual(out["candidates"], 0)     # neither reserved id is ever a candidate
        self.assertEqual(probed, [])               # never probed
        self.assertEqual(calls, [])                # never established

    def test_fresh_workspace_skipped_healthy_not_probed(self):
        probed = []
        acct, node = _account()
        _armed_ws(acct, node, stale=False)         # fresh => healthy, not a recovery candidate
        out = SR.run_hosted_session_reconciler(
            executor_resolver=_resolver(), probe_fn=lambda ws: probed.append(1) or {"ok": True, "session_status": "ABSENT"})
        self.assertEqual(out["skipped_healthy"], 1)
        self.assertEqual(out["candidates"], 0)
        self.assertEqual(probed, [])

    def test_no_executor_skips_before_probe(self):
        probed = []
        acct, node = _account()
        _armed_ws(acct, node)
        out = SR.run_hosted_session_reconciler(
            executor_resolver=lambda account_id, rdp_host: None,   # DARK / unarmed host executor
            probe_fn=lambda ws: probed.append(1) or {"ok": True, "session_status": "ABSENT"})
        self.assertEqual(out["skipped_no_executor"], 1)
        self.assertEqual(probed, [])               # cannot even probe without an executor


class ManagementCommandTests(TestCase):
    def test_disabled_is_noop(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("run_hosted_session_reconciler", stdout=out)   # flag off => no-op
        self.assertIn("disabled", out.getvalue())

    @override_settings(**RECON)
    def test_force_runs_probe_only_summary(self):
        # --force runs the pass; with no host executor configured it is a safe probe-only no-op that still emits a
        # secret-free summary. The command wires NO establish_fn, so it can never self-connect.
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("run_hosted_session_reconciler", "--force", stdout=out)
        text = out.getvalue()
        self.assertIn("session_reconciler:", text)
        self.assertIn("enabled=True", text)
        self.assertIn("established=0", text)
