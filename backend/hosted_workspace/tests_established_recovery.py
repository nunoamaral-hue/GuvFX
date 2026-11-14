"""ESTABLISHED-ACCOUNT-AUTO-RECOVERY-109 — tests for the established-vs-onboarding recovery distinction.

Proves (the packet-108 gap and its fix):
  * the fail-closed eligibility contract (``recovery_eligibility.established_account_recovery_eligible``) admits
    ONLY an established, enabled, owner-bound, identity-pinned account with PRIOR VERIFIED (immutable,
    identity-matched ``account_read``) evidence under the CURRENT pin, and refuses everything else with a
    specific reason code (genuinely-new, awaiting-login, identity drift, cross-owner evidence, reserved/CZ,
    disabled, tombstoned, no endpoint/pin, LIVE with recovery off, indeterminate env);
  * the liveness pass relaunches the DOWN terminal of such an established MONITORING-ONLY account (NOT armed)
    exactly once via the account-scoped RELAUNCH_TERMINAL primitive, bounded + single-flight, keyed off
    equity-snapshot staleness, and NEVER arms, logs in, or writes a snapshot;
  * onboarding stays fail-closed; a terminal that is UP but not producing reads is login-required (not
    relaunched); ambiguous observe never relaunches; one account's failure never relaunches another;
  * the #50-shaped regression recovers WITH the flag on and REPRODUCES (no relaunch) with it off
    (mutation discrimination); and
  * capability recovery uses the SAME classifier (an established-but-unconfirmed account is a sharper
    ``skipped_established`` reason, still a SKIP — no host call — behaviour preserved).
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from analytics.equity_snapshots import DEFAULT_FRESHNESS_S
from analytics.models import AccountEquitySnapshot
from execution import readiness as R
from execution.models import HostedExecutionEndpoint, TerminalNode
from reliability.models import AlertEvent
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import capability_recovery as CR
from hosted_workspace import liveness_recovery as LR
from hosted_workspace import recovery_eligibility as RE
from hosted_workspace.host_agent_dispatch import derive_slot
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()
_n = 0

MASTER = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_LIVENESS_RECOVERY_ENABLED="1")
EST_FLAGS = dict(MASTER, HOSTED_ESTABLISHED_RECOVERY_ENABLED="1")
EST_LIVE_FLAGS = dict(EST_FLAGS, HOSTED_LIVE_RECOVERY_ENABLED="1")
CAP_FLAGS = dict(HOSTED_PERSISTENT_MT5_ENABLED="1", HOSTED_CAPABILITY_RECOVERY_ENABLED="1",
                 HOSTED_BOUNDED_OBSERVATION_ENABLED="1")


def _uniq():
    global _n
    _n += 1
    return f"77{_n:04d}"


class FakeExecutor:
    """Records host calls; no real host contact."""

    def __init__(self, *, relaunch_ok=True):
        self.calls = []
        self.relaunch_ok = relaunch_ok

    def relaunch_terminal(self, username, runtime_root, rdp_host=None):
        self.calls.append(("relaunch_terminal", username, runtime_root, rdp_host))
        return {"ok": self.relaunch_ok}

    def apply_autotrading_config(self, runtime_root, rdp_host=None):
        self.calls.append(("apply_autotrading_config", runtime_root, rdp_host))
        return {"ok": True}


def _resolver(ex):
    return lambda account_id, rdp_host: ex


def _down(_ws):
    return {"ok": False, "process_running": False, "reason": "terminal_not_running"}


def _up(_ws):
    return {"ok": True, "process_running": True}


def _ambiguous(_ws):
    return None


def _mk_account(*, is_demo=True, confirmed=False, active=True, rdp="100.79.101.19"):
    login = _uniq()
    # Force a HIGH, deterministic id so a Postgres autoincrement never lands a test account on a reserved id
    # (1/18) or a Customer Zero id — that would make the account correctly INELIGIBLE (reserved_identity) and
    # mask the behaviour under test. Production #50-class accounts carry ordinary high ids, never reserved ones.
    acct_id = 900000 + int(login[2:])
    user = U.objects.create_user(username=f"er{login}", email=f"{login}@x.invalid", password="x")
    env = BrokerServer.DEMO if is_demo else BrokerServer.LIVE
    srv = BrokerServer.objects.create(server_name=f"S-{login}", environment=env)
    node = TerminalNode.objects.create(hostname=f"n-{login}", status=TerminalNode.Status.ACTIVE, rdp_host=rdp)
    acct = TradingAccount.objects.create(
        id=acct_id, user=user, name="a", broker_name="B", account_number=login, is_demo=is_demo,
        is_active=active, broker_server=srv, readiness_provider=R.PERSISTENT_WORKSPACE, terminal_node=node,
        workspace_confirmed_at=(timezone.now() if confirmed else None))
    return acct, node, srv


def _mk_ws(acct, node, *, armed=False, matched=True, canonical=S.CONNECTED):
    return HostedMt5Workspace.objects.create(
        trading_account=acct, canonical_state=canonical, proj_connected=True, proj_account_match=matched,
        proj_trade_allowed=False, last_decision_at=None, execution_node=node, execution_enabled=armed,
        execution_authorized_at=(timezone.now() if armed else None))


def _mk_endpoint(ws, acct, node, *, login, server, state=None):
    state = state or HostedExecutionEndpoint.State.READY
    port = 10000 + (acct.id % 50000)
    return HostedExecutionEndpoint.objects.create(
        workspace=ws, trading_account=acct, terminal_node=node, host="100.79.101.19", port=port,
        base_url=f"http://100.79.101.19:{port}", windows_username=f"guvfx_u_{acct.id}",
        runtime_path=rf"C:\GuvFX\accounts\{acct.id}\terminal", expected_login=login, expected_server=server,
        is_demo=acct.is_demo, workspace_uuid=ws.workspace_uuid, state=state)


def _mk_snapshot(acct, *, login, server, age_s, source="account_read", equity="250.00"):
    return AccountEquitySnapshot.objects.create(
        trading_account=acct, observed_at=timezone.now() - timezone.timedelta(seconds=age_s),
        equity=Decimal(equity), balance=Decimal(equity), observed_login=login, observed_server=server,
        source=source)


def _established_scenario(*, is_demo=True, snapshot_age_s=None, pin_login="PIN1", snap_login="PIN1",
                          with_endpoint=True, with_snapshot=True, server="SRV1"):
    """An ESTABLISHED monitoring-only account (NOT armed, NOT confirmed) with an endpoint identity pin and a
    prior identity-matched account_read snapshot. ``snapshot_age_s`` defaults to STALE (so monitoring has
    stopped) — the #50 condition. Returns (acct, node, ws, srv)."""
    if snapshot_age_s is None:
        snapshot_age_s = DEFAULT_FRESHNESS_S + 3600
    acct, node, srv = _mk_account(is_demo=is_demo, confirmed=False)
    ws = _mk_ws(acct, node, armed=False)
    if with_endpoint:
        _mk_endpoint(ws, acct, node, login=pin_login, server=server)
    if with_snapshot:
        _mk_snapshot(acct, login=snap_login, server=server, age_s=snapshot_age_s)
    return acct, node, ws, srv


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Eligibility contract — unit tests (fail-closed; one specific reason per rejection).
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class EligibilityTests(TestCase):
    def test_established_monitoring_account_is_eligible_even_when_unconfirmed(self):
        acct, node, ws, _ = _established_scenario()
        self.assertIsNone(acct.workspace_confirmed_at)   # the key point: NOT confirmed, yet eligible
        res = RE.established_account_recovery_eligible(ws, acct)
        self.assertTrue(res.eligible)
        self.assertEqual(res.reason, RE.RC_ELIGIBLE)

    def test_none_account(self):
        _, _, ws, _ = _established_scenario()
        self.assertEqual(RE.established_account_recovery_eligible(ws, None).reason, RE.RC_NO_ACCOUNT)

    def test_tombstoned_account(self):
        acct, node, ws, _ = _established_scenario()
        acct.disconnected_at = timezone.now()
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_DISCONNECTED)

    def test_disabled_account(self):
        acct, node, ws, _ = _established_scenario()
        acct.is_active = False
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_DISABLED)

    def test_no_owner(self):
        acct, node, ws, _ = _established_scenario()
        acct.user_id = None                               # in-memory only (not saved)
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_NO_OWNER)

    def test_reserved_identity(self):
        # Build a scenario, then coerce the in-memory id to a reserved one (18) with a matching ws binding.
        acct, node, ws, _ = _established_scenario()
        acct.id = 18
        ws.trading_account_id = 18
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_RESERVED)

    def test_slot_underivable(self):
        from types import SimpleNamespace
        acct = SimpleNamespace(id=0, disconnected_at=None, is_active=True, user_id=1, is_demo=True)
        ws = SimpleNamespace(trading_account_id=0, pk=1)
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_SLOT_UNDERIVABLE)

    def test_workspace_account_mismatch(self):
        acct, node, ws, _ = _established_scenario()
        ws.trading_account_id = acct.id + 99999
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_WS_ACCOUNT_MISMATCH)

    def test_no_endpoint(self):
        acct, node, ws, _ = _established_scenario(with_endpoint=False)
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_NO_ENDPOINT)

    def test_endpoint_workspace_mismatch(self):
        acct, node, ws, _ = _established_scenario()
        ep = ws.execution_endpoint
        ep.workspace_uuid = ep.workspace_uuid           # no-op; drift the denormalised pointer instead:
        HostedExecutionEndpoint.objects.filter(pk=ep.pk).update(workspace=_other_ws(acct, node))
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_ENDPOINT_WS_MISMATCH)

    def test_no_identity_pin(self):
        acct, node, ws, _ = _established_scenario()
        HostedExecutionEndpoint.objects.filter(trading_account_id=acct.id).update(expected_login="")
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_NO_IDENTITY_PIN)

    def test_no_prior_verified_read_is_fail_closed(self):
        acct, node, ws, _ = _established_scenario(with_snapshot=False)
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_NO_PRIOR_VERIFIED_READ)

    def test_awaiting_first_login_has_no_evidence(self):
        # A fresh account with an endpoint but never a successful read is indistinguishable from "no evidence".
        acct, node, ws, _ = _established_scenario(with_snapshot=False)
        self.assertFalse(RE.established_account_recovery_eligible(ws, acct).eligible)

    def test_non_account_read_snapshot_is_not_evidence(self):
        acct, node, ws, _ = _established_scenario(with_snapshot=False)
        _mk_snapshot(acct, login="PIN1", server="SRV1", age_s=100, source="observer")
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_NO_PRIOR_VERIFIED_READ)

    def test_identity_pin_drift(self):
        # Prior reads exist but under a DIFFERENT login than the current pin (a rebind) → fail closed.
        acct, node, ws, _ = _established_scenario(pin_login="NEWPIN", snap_login="OLDPIN")
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_IDENTITY_PIN_DRIFT)

    def test_cross_owner_evidence_cannot_qualify(self):
        # Account A has no reads; account B has a matching-login read. A must NOT borrow B's evidence.
        acct_a, node_a, ws_a, _ = _established_scenario(with_snapshot=False, pin_login="SHARED")
        acct_b, node_b, ws_b, _ = _established_scenario(pin_login="SHARED", snap_login="SHARED")
        self.assertEqual(RE.established_account_recovery_eligible(ws_a, acct_a).reason,
                         RE.RC_NO_PRIOR_VERIFIED_READ)
        self.assertTrue(RE.established_account_recovery_eligible(ws_b, acct_b).eligible)

    @override_settings(**EST_FLAGS)   # live-recovery flag OFF
    def test_live_env_without_live_recovery_is_refused(self):
        acct, node, ws, _ = _established_scenario(is_demo=False)
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_LIVE_RECOVERY_DISABLED)

    @override_settings(**EST_LIVE_FLAGS)
    def test_live_env_with_live_recovery_is_eligible(self):
        acct, node, ws, _ = _established_scenario(is_demo=False)
        self.assertTrue(RE.established_account_recovery_eligible(ws, acct).eligible)

    def test_indeterminate_env_is_fail_closed(self):
        # is_demo=True but the broker server says LIVE → classification_error → indeterminate → closed.
        acct, node, ws, srv = _established_scenario(is_demo=True)
        srv.environment = BrokerServer.LIVE
        srv.save(update_fields=["environment"])
        self.assertEqual(RE.established_account_recovery_eligible(ws, acct).reason, RE.RC_ENV_INDETERMINATE)


def _other_ws(acct, node):
    """A second workspace for a throwaway account, used to force an endpoint/workspace drift."""
    other_acct, other_node, _ = _mk_account()
    return _mk_ws(other_acct, other_node)


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Liveness pass — established MONITORING recovery (integration).
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class EstablishedLivenessTests(TestCase):
    @override_settings(**EST_FLAGS)
    def test_down_established_monitoring_account_is_relaunched_once(self):
        acct, node, ws, _ = _established_scenario()
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        est = out["established"]
        self.assertEqual(est["relaunched"], 1)
        self.assertEqual(est["down"], 1)
        self.assertEqual(est["eligible"], 1)
        slot = derive_slot(acct.id)
        self.assertEqual(ex.calls, [("relaunch_terminal", slot["username"], slot["runtime_root"], node.rdp_host)])

    @override_settings(**EST_FLAGS)
    def test_relaunch_never_arms_or_writes_snapshot(self):
        acct, node, ws, _ = _established_scenario()
        before = AccountEquitySnapshot.objects.count()
        LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        ws.refresh_from_db()
        self.assertFalse(ws.execution_enabled)                 # never armed
        self.assertIsNone(ws.execution_authorized_at)
        self.assertEqual(AccountEquitySnapshot.objects.count(), before)   # no manual/synthetic snapshot

    @override_settings(**EST_FLAGS)
    def test_fresh_monitoring_is_skipped_healthy(self):
        _established_scenario(snapshot_age_s=60)               # fresh snapshot → monitoring flowing
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["skipped_healthy"], 1)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_fresh_read_under_drifted_login_does_not_mask_stale(self):
        # Eligible (a prior PIN1 read exists), but the LATEST read is recent under a DRIFTED login. The pinned
        # freshness gate must NOT treat that as 'monitoring flowing' (that would silently suppress recovery while
        # the pinned identity is not actually being served). So it is observed → down → relaunched, not skipped.
        acct, node, ws, _ = _established_scenario(pin_login="PIN1", snap_login="PIN1")
        _mk_snapshot(acct, login="DRIFT", server="SRV1", age_s=60)   # recent, but WRONG identity
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["skipped_healthy"], 0)   # drifted fresh read did NOT mask it
        self.assertEqual(out["established"]["relaunched"], 1)
        self.assertEqual(len(ex.calls), 1)

    @override_settings(**EST_FLAGS)
    def test_terminal_up_but_stale_is_login_required_not_relaunched(self):
        acct, node, ws, _ = _established_scenario()
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_up)
        self.assertEqual(out["established"]["terminal_up_login_required"], 1)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(ex.calls, [])
        alert = AlertEvent.objects.get(dedup_key=f"hosted_established_terminal_down:account:{acct.id}")
        self.assertEqual(alert.detail["reason"], "monitoring_stale_terminal_up_login_required")

    @override_settings(**EST_FLAGS)
    def test_ambiguous_observe_is_not_relaunched(self):
        _established_scenario()
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_ambiguous)
        self.assertEqual(out["established"]["skipped_ambiguous"], 1)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_second_pass_within_cooldown_no_duplicate(self):
        _established_scenario()
        LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_down)
        ex2 = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex2), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(out["established"]["skipped_cooldown"], 1)
        self.assertEqual(ex2.calls, [])

    @override_settings(**EST_FLAGS)
    def test_bounded_by_max_attempts(self):
        acct, node, ws, _ = _established_scenario()
        ws.liveness_recovery_count = LR.MAX_RECOVERY_ATTEMPTS
        ws.save(update_fields=["liveness_recovery_count"])
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(out["established"]["skipped_cooldown"], 1)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_dark_executor_burns_no_attempt(self):
        acct, node, ws, _ = _established_scenario()
        out = LR.run_hosted_liveness_recovery(executor_resolver=lambda a, r: None, observe_fn=_down)
        self.assertEqual(out["established"]["skipped_no_executor"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 0)

    @override_settings(**EST_FLAGS)
    def test_relaunch_failure_still_claims_attempt(self):
        acct, node, ws, _ = _established_scenario()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor(relaunch_ok=False)),
                                              observe_fn=_down)
        self.assertEqual(out["established"]["errors"], 1)
        ws.refresh_from_db()
        self.assertEqual(ws.liveness_recovery_count, 1)        # attempt claimed → backs off (no restart loop)

    @override_settings(**EST_FLAGS)
    def test_new_account_not_relaunched(self):
        _established_scenario(with_snapshot=False)             # genuinely new / no verified evidence
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(out["established"]["ineligible"], 1)
        self.assertEqual(out["established"]["reasons"].get(RE.RC_NO_PRIOR_VERIFIED_READ), 1)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_identity_drift_not_relaunched(self):
        _established_scenario(pin_login="NEWPIN", snap_login="OLDPIN")
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(out["established"]["reasons"].get(RE.RC_IDENTITY_PIN_DRIFT), 1)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_disabled_account_excluded(self):
        acct, node, srv = _mk_account(active=False)
        ws = _mk_ws(acct, node)
        _mk_endpoint(ws, acct, node, login="PIN1", server="SRV1")
        _mk_snapshot(acct, login="PIN1", server="SRV1", age_s=DEFAULT_FRESHNESS_S + 3600)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 0)
        self.assertEqual(ex.calls, [])

    @override_settings(**EST_FLAGS)
    def test_armed_account_not_double_handled_by_established_pass(self):
        # An ARMED, confirmed, matched, demo workspace that is down is the armed pass's job; the established
        # pass must EXCLUDE it (disjoint candidate sets → never a double relaunch).
        acct, node, srv = _mk_account(is_demo=True, confirmed=True)
        ws = _mk_ws(acct, node, armed=True)
        _mk_endpoint(ws, acct, node, login="PIN1", server="SRV1")
        _mk_snapshot(acct, login="PIN1", server="SRV1", age_s=DEFAULT_FRESHNESS_S + 3600)
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["candidates"], 0)   # not an established candidate
        self.assertEqual(out["relaunched"], 1)                  # armed pass relaunched it once
        self.assertEqual(len(ex.calls), 1)                      # exactly once in total

    @override_settings(**EST_FLAGS)
    def test_one_account_failure_cannot_relaunch_another(self):
        _established_scenario()                                 # eligible + down
        _established_scenario(with_snapshot=False)              # ineligible + down
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 1)
        self.assertEqual(out["established"]["ineligible"], 1)
        self.assertEqual(len(ex.calls), 1)

    @override_settings(**EST_FLAGS)
    def test_recovered_alert_resolved_when_monitoring_restored(self):
        acct, node, ws, _ = _established_scenario(snapshot_age_s=60)   # fresh again
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL, component="MT5_TERMINAL", trading_account_id=acct.id,
            title="x", body="x", dedup_key=f"hosted_established_terminal_down:account:{acct.id}",
            status=AlertEvent.Status.OPEN, detail={"account_id": acct.id})
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(FakeExecutor()), observe_fn=_up)
        self.assertEqual(out["established"]["recovered"], 1)
        self.assertFalse(AlertEvent.objects.filter(
            dedup_key=f"hosted_established_terminal_down:account:{acct.id}",
            status=AlertEvent.Status.OPEN).exists())


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# #50-shaped regression + mutation discrimination.
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class Account50RegressionTests(TestCase):
    def _account_50_shaped(self):
        """LIVE (IS6Technologies-Live-shaped), NOT armed, confirmed=None, prior matched read, snapshot STALE
        (~12 days), terminal down — exactly the packet-108 #50 condition."""
        acct, node, srv = _mk_account(is_demo=False, confirmed=False)
        ws = _mk_ws(acct, node, armed=False)
        _mk_endpoint(ws, acct, node, login="LIVE50", server="IS6Technologies-Live")
        _mk_snapshot(acct, login="LIVE50", server="IS6Technologies-Live", age_s=12 * 24 * 3600)
        return acct, node, ws

    @override_settings(**EST_LIVE_FLAGS)
    def test_50_recovers_with_flag_on(self):
        acct, node, ws = self._account_50_shaped()
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"]["relaunched"], 1)
        slot = derive_slot(acct.id)
        self.assertEqual(ex.calls, [("relaunch_terminal", slot["username"], slot["runtime_root"], node.rdp_host)])

    @override_settings(**MASTER)   # established flag OFF + live flag OFF = pre-109 behaviour (the mutation)
    def test_50_regression_reproduces_with_flag_off(self):
        acct, node, ws = self._account_50_shaped()
        ex = FakeExecutor()
        out = LR.run_hosted_liveness_recovery(executor_resolver=_resolver(ex), observe_fn=_down)
        self.assertEqual(out["established"], {"enabled": False})   # established pass inert
        self.assertEqual(out["relaunched"], 0)                     # armed pass never saw it either (the gap)
        self.assertEqual(ex.calls, [])                             # #50 does NOT self-heal → regression reproduced


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# Capability recovery — SAME classifier, behaviour preserved (established-unconfirmed is a SKIP, sharper reason).
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
class CapabilityClassificationTests(TestCase):
    def _cap_candidate(self, *, established):
        """A CONNECTED + matched + trade_allowed=False (capability candidate), unconfirmed workspace. When
        ``established`` it also has an endpoint pin + prior matched read; otherwise it is genuinely new."""
        acct, node, srv = _mk_account(is_demo=True, confirmed=False)
        ws = _mk_ws(acct, node, armed=False, canonical=S.CONNECTED)
        if established:
            _mk_endpoint(ws, acct, node, login="PIN1", server="SRV1")
            _mk_snapshot(acct, login="PIN1", server="SRV1", age_s=DEFAULT_FRESHNESS_S + 3600)
        return acct, node, ws

    @override_settings(**CAP_FLAGS)
    def test_established_unconfirmed_is_skipped_established_no_host_call(self):
        acct, node, ws = self._cap_candidate(established=True)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["skipped_established"], 1)
        self.assertEqual(out["skipped_onboarding"], 0)
        self.assertEqual(out["relaunched"], 0)
        self.assertEqual(ex.calls, [])                       # behaviour preserved: STILL a skip, no host contact

    @override_settings(**CAP_FLAGS)
    def test_genuinely_new_unconfirmed_is_skipped_onboarding(self):
        acct, node, ws = self._cap_candidate(established=False)
        ex = FakeExecutor()
        out = CR.run_hosted_capability_recovery(executor_resolver=_resolver(ex))
        self.assertEqual(out["skipped_onboarding"], 1)
        self.assertEqual(out["skipped_established"], 0)
        self.assertEqual(ex.calls, [])
