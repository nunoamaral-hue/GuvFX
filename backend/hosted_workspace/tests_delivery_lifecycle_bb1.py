"""BB#1 (Sponsor 2026-08-16) — Hosted delivery LIFECYCLE completion. Test bar A–L.

Proves the four flag-gated, fail-closed edges that turn a provisioned-but-stuck workspace into one whose
customer can open MetaTrader, WITHOUT redefining CONNECTED and WITHOUT touching Customer Zero:

  A  fresh provisioning autonomously runs PREPARE_OBSERVER (required + stage-timed) under the flag.
  B  PREPARE_OBSERVER is idempotent (re-run ok:true → still prepared, one stage row).
  C  Customer Zero is refused the observer-prep path (reserved account, fail closed before any host step).
  D  a published + authoritatively deliverable workspace projects DELIVERY_DELIVERABLE (button BEFORE CONNECTED).
  E  a non-deliverable workspace does NOT project DELIVERABLE.
  F  the delivery authority stays owner-scoped — deliverability is AVAILABILITY, not a client-side mint bypass.
  G  a TRUSTED LocalSystem session-up corroboration drives the single writer → CONNECTED.
  H  wrong-user / wrong-runtime / wrong-session / process-absent observations do NOT drive CONNECTED.
  I  duplicate / stale delivery events cannot regress or corrupt delivery state (monotonic seq gate).
  J  the delivery producer goes ONLY through the single writer (never writes delivery_state directly).
  K  broker bind acknowledges + persists identity WITHOUT advancing state from untrusted observation.
  L  Customer Zero + flag-OFF are byte-identical (no observer required, no DELIVERABLE, no CONNECTED edge).
"""
from __future__ import annotations

import os
from unittest import mock

from django.test import TestCase, override_settings

from execution.readiness import PERSISTENT_WORKSPACE
from terminal_provisioning.models import AccountProvisioning
from trading.crypto import encrypt_password
from trading.models import TradingAccount

from hosted_workspace import slot_preparation as SP
from hosted_workspace.delivery import workspace_delivery_ready
from hosted_workspace.delivery_observe_runner import run_hosted_delivery_observe
from hosted_workspace.live_observe import observe_remoteapp_session
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.onboarding_read_model import (
    DELIVERY_DELIVERABLE, DELIVERY_EXTERNAL_GATE, DELIVERY_READY, delivery_readiness)
from hosted_workspace.provisioning_timing import STAGE_OBSERVER_PREPARED
from hosted_workspace.state_machine import WorkspaceLifecycleState as S
from hosted_workspace.tests_provisioning import _FLAGS_ON, _node, _user
from hosted_workspace.tests_slot_preparation import FakeExecutor, _account

DS = HostedMt5Workspace.DeliveryState

# Flags: everything the lifecycle needs, reserved-ids emptied so a test account is not mistaken for CZ.
_ALL_ON = dict(
    _FLAGS_ON, HOSTED_MT5_REMOTEAPP_ENABLED="1", HOSTED_SLOT_PREP_ENABLED="1",
    HOSTED_MT5_OBSERVATION_ENABLED="1", HOSTED_DELIVERY_LIFECYCLE_ENABLED="1",
    HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS="")
_GUAC = {"GUAC_BASE_URL": "https://guac.example", "GUAC_JSON_SECRET_KEY_HEX": "aabbcc"}


def _bound(uname="u1", login="700900", rdp_host="10.9.9.9"):
    node = _node(hostname=f"node-{uname}", rdp_host=rdp_host)
    acct = _account(_user(uname), login)
    ws = HostedMt5Workspace.objects.create(trading_account=acct)
    ws.execution_node = node
    ws.workspace_node = node
    ws.save(update_fields=["execution_node", "workspace_node"])
    return ws, acct, node


def _provisioned(acct, *, admin=False, status=AccountProvisioning.Status.PROVISIONED, cred=None):
    return AccountProvisioning.objects.create(
        trading_account=acct, windows_username=f"guvfx_u_{acct.pk}", is_admin=admin,
        password_enc=cred if cred is not None else encrypt_password("winpw"),
        runtime_root=f"C:\\GuvFX\\accounts\\{acct.pk}", runtime_structure={}, status=status)


def _corr(acct, *, present=True, owner=None, session=5, runtime=None, collected=1_000_000.0):
    return {"account_id": acct.pk, "process_present": present,
            "owner_user": owner if owner is not None else f"guvfx_u_{acct.pk}",
            "session_id": session,
            "runtime_root": runtime if runtime is not None else f"C:\\GuvFX\\accounts\\{acct.pk}",
            "collected_at": collected, "remote_endpoints": []}


class _ObsExec:
    """Fake signed executor whose observe() returns a fixed result — for the delivery-session signal tests."""
    def __init__(self, result):
        self._result = result

    def observe(self, rdp_host=None):
        return self._result


# ── A / B / C: autonomous, idempotent, CZ-refused observer preparation ───────────────────────────────────
@override_settings(**_ALL_ON)
class ObserverPrepTests(TestCase):
    def test_A_fresh_provisioning_runs_prepare_observer_and_stage_times_it(self):
        ws, acct, _ = _bound()
        ex = FakeExecutor()
        res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertTrue(res.prepared, res.reason)
        self.assertFalse(res.observer_deferred)                 # REQUIRED step verified
        self.assertIn("register_observer", ex.calls)            # the signed PREPARE_OBSERVER primitive ran
        self.assertTrue(ws.stage_timings.filter(stage=STAGE_OBSERVER_PREPARED).exists())

    def test_A2_required_observer_failure_fails_closed(self):
        ws, _, _ = _bound()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(fail=("register_observer",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_OBSERVER_FAILED)   # never advance with no observer
        self.assertFalse(ws.stage_timings.filter(stage=STAGE_OBSERVER_PREPARED).exists())

    def test_A3_missing_observer_method_is_executor_incomplete(self):
        ws, _, _ = _bound()
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(drop=("register_observer",)))
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_EXECUTOR_INCOMPLETE)

    def test_B_prepare_observer_is_idempotent(self):
        ws, _, _ = _bound()
        SP.prepare_hosted_slot(ws, executor=FakeExecutor())
        SP.prepare_hosted_slot(ws, executor=FakeExecutor())     # re-run
        # get_or_create keeps exactly ONE observer-prepared timing row (host primitive is idempotent).
        self.assertEqual(ws.stage_timings.filter(stage=STAGE_OBSERVER_PREPARED).count(), 1)

    def test_C_customer_zero_refused_before_any_host_step(self):
        ws, acct, _ = _bound()
        ex = FakeExecutor()
        with override_settings(HOSTED_SLOT_PREP_RESERVED_ACCOUNT_IDS=str(acct.pk)):
            res = SP.prepare_hosted_slot(ws, executor=ex)
        self.assertFalse(res.prepared)
        self.assertEqual(res.reason, SP.PREP_REFUSED_RESERVED)
        self.assertEqual(ex.calls, [])                          # NO host contact at all for a reserved id


# ── D / E / F: DELIVERABLE readiness (break the button⇄CONNECTED deadlock) + owner authority ─────────────
class DeliverableReadinessTests(TestCase):
    @override_settings(**_ALL_ON)
    def test_D_deliverable_workspace_projects_DELIVERABLE_before_connected(self):
        ws, acct, _ = _bound()
        _provisioned(acct)
        # HIGH fix: DELIVERABLE requires prep to have FINISHED (canonical advanced PAST PROVISIONING). A prepared
        # slot is at WAITING_FOR_LOGIN, so set it here — the whole point is "openable now, before CONNECTED".
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["canonical_state", "delivery_state"])
        with mock.patch.dict(os.environ, _GUAC, clear=False), \
             mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()):
            self.assertTrue(workspace_delivery_ready(ws))
            self.assertEqual(delivery_readiness(ws), DELIVERY_DELIVERABLE)
        # And once actually CONNECTED it is READY (connection wins over mere availability).
        ws.delivery_state = DS.CONNECTED
        with mock.patch.dict(os.environ, _GUAC, clear=False):
            self.assertEqual(delivery_readiness(ws), DELIVERY_READY)

    @override_settings(**_ALL_ON)
    def test_D2_provisioning_workspace_is_never_deliverable(self):
        # HIGH fix (adversarial review): at PROVISIONING the RemoteApp may be unpublished and no observer exists,
        # so even though the delivery AUTHORITY would mint (identity PROVISIONED at Stage 4), the customer must NOT
        # be shown a live "Open MetaTrader" — DELIVERABLE is withheld until prep FINISHES (past PROVISIONING).
        ws, acct, _ = _bound()
        _provisioned(acct)
        ws.canonical_state = S.PROVISIONING.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["canonical_state", "delivery_state"])
        with mock.patch.dict(os.environ, _GUAC, clear=False), \
             mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()):
            self.assertTrue(workspace_delivery_ready(ws))                       # authority WOULD mint...
            self.assertNotEqual(delivery_readiness(ws), DELIVERY_DELIVERABLE)   # ...but it is NOT surfaced openable

    @override_settings(**_ALL_ON)
    def test_D3_customer_zero_never_projects_DELIVERABLE(self):
        # Defence in depth: a prepared, deliverable CUSTOMER ZERO workspace never gets the NEW DELIVERABLE surface
        # (CZ uses the legacy Terminal Access path). Explicit CZ-refused, matching the packet's CZ-safety mandate.
        ws, acct, _ = _bound()
        _provisioned(acct)
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["canonical_state", "delivery_state"])
        with mock.patch.dict(os.environ, _GUAC, clear=False), \
             mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset({acct.pk})):
            self.assertTrue(workspace_delivery_ready(ws))                       # deliverable in the abstract...
            self.assertNotEqual(delivery_readiness(ws), DELIVERY_DELIVERABLE)   # ...but never for Customer Zero

    @override_settings(**_ALL_ON)
    def test_E_non_deliverable_workspace_does_not_project_DELIVERABLE(self):
        # No AccountProvisioning → not deliverable → never DELIVERABLE (stays host-pending EXTERNAL_GATE).
        ws, acct, _ = _bound()
        ws.delivery_state = DS.NONE
        with mock.patch.dict(os.environ, _GUAC, clear=False):
            self.assertFalse(workspace_delivery_ready(ws))
            self.assertEqual(delivery_readiness(ws), DELIVERY_EXTERNAL_GATE)
        # Provisioned but PENDING (not yet PROVISIONED) is also not deliverable.
        _provisioned(acct, status=AccountProvisioning.Status.PENDING)
        with mock.patch.dict(os.environ, _GUAC, clear=False):
            self.assertFalse(workspace_delivery_ready(ws))

    @override_settings(**_ALL_ON)
    def test_E2_missing_guac_is_not_deliverable(self):
        ws, acct, _ = _bound()
        _provisioned(acct)
        with mock.patch.dict(os.environ, {"GUAC_BASE_URL": "", "GUAC_JSON_SECRET_KEY_HEX": ""}, clear=False):
            self.assertFalse(workspace_delivery_ready(ws))

    @override_settings(**_ALL_ON)
    def test_F_deliverability_is_not_a_mint_bypass_owner_still_required(self):
        from hosted_workspace.delivery import DeliveryReason, authorize_workspace_delivery
        ws, acct, _ = _bound()
        _provisioned(acct)
        other = _user("intruder")
        with mock.patch.dict(os.environ, _GUAC, clear=False):
            self.assertTrue(workspace_delivery_ready(ws))       # deliverable...
            auth = authorize_workspace_delivery(other, ws.workspace_uuid)   # ...but a NON-owner is refused
        self.assertFalse(auth.authorized)
        self.assertEqual(auth.reason, DeliveryReason.NOT_OWNER)
        self.assertIsNone(auth.descriptor)


# ── G / H / I / J: the trusted CONNECTED producer + single-writer + monotonic-seq safety ─────────────────
class ConnectedProducerTests(TestCase):
    @override_settings(**_ALL_ON)
    def test_G_trusted_session_signal_drives_connected_via_single_writer(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["canonical_state", "delivery_state"])
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()):
            out = run_hosted_delivery_observe(observe_session_fn=lambda w: "CONNECTED")
        ws.refresh_from_db()
        self.assertEqual(ws.delivery_state, DS.CONNECTED)
        self.assertTrue(ws.remoteapp_ready)
        self.assertEqual(delivery_readiness(ws), DELIVERY_READY)
        self.assertEqual(out["connected"], 1)

    @override_settings(**_ALL_ON)
    def test_G2_observe_remoteapp_session_matches_localsystem_corroboration(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.save(update_fields=["canonical_state"])
        # ``now`` is pinned to the corroboration's collected_at so the freshness anchor is satisfied (the signal
        # is trusted only from a FRESH LocalSystem observation).
        with mock.patch("hosted_workspace.live_observe.supervised_single_tenant_beta_active", return_value=True), \
             mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor",
                        return_value=_ObsExec({"ok": True, "corroboration": _corr(acct)})):
            self.assertEqual(observe_remoteapp_session(ws, now=1_000_000.0), "CONNECTED")

    @override_settings(**_ALL_ON)
    def test_G3_stale_corroboration_is_refused_fresh_is_accepted(self):
        # RULE 11 positive+negative control on the delivery freshness anchor (adversarial-review MEDIUM fix): a
        # well-formed, daemon-signed corroboration whose LocalSystem collected_at is OUTSIDE the 60s window must NOT
        # drive CONNECTED (stale/replay → hold), while the identical FRESH one does. Proves the delivery path
        # applies the same staleness guard the certified producer applies (which this path bypasses).
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.save(update_fields=["canonical_state"])
        with mock.patch("hosted_workspace.live_observe.supervised_single_tenant_beta_active", return_value=True), \
             mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor",
                        return_value=_ObsExec({"ok": True, "corroboration": _corr(acct, collected=1_000_000.0)})):
            self.assertEqual(observe_remoteapp_session(ws, now=1_000_000.0), "CONNECTED")   # fresh → up (positive)
            self.assertIsNone(observe_remoteapp_session(ws, now=1_000_000.0 + 10_000))      # stale → hold (negative)

    @override_settings(**_ALL_ON)
    def test_H_untrusted_or_mismatched_observation_never_connects(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.save(update_fields=["canonical_state"])
        bad_results = [
            {"ok": False, "reason": "no_observer_task"},                       # observer not prepared
            {"ok": True, "corroboration": _corr(acct, owner="guvfx_u_999")},   # wrong Windows user
            {"ok": True, "corroboration": _corr(acct, runtime="C:\\evil")},    # wrong runtime root
            {"ok": True, "corroboration": _corr(acct, session=0)},             # non-interactive session
        ]
        for res in bad_results:
            with mock.patch("hosted_workspace.live_observe.supervised_single_tenant_beta_active", return_value=True), \
                 mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor",
                            return_value=_ObsExec(res)):
                self.assertIsNone(observe_remoteapp_session(ws, now=1_000_000.0), res)
        # A FRESH process-absent corroboration for THIS account is the trusted DISCONNECTED, never CONNECTED.
        with mock.patch("hosted_workspace.live_observe.supervised_single_tenant_beta_active", return_value=True), \
             mock.patch("hosted_workspace.host_executor.resolve_signed_host_executor",
                        return_value=_ObsExec({"ok": True, "corroboration": _corr(acct, present=False)})):
            self.assertEqual(observe_remoteapp_session(ws, now=1_000_000.0), "DISCONNECTED")

    @override_settings(**_ALL_ON)
    def test_I_ambiguous_cycle_holds_state_no_flap(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.CONNECTED       # already connected
        ws.remoteapp_ready = True
        ws.save(update_fields=["canonical_state", "delivery_state", "remoteapp_ready"])
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()):
            out = run_hosted_delivery_observe(observe_session_fn=lambda w: None)   # unavailable/ambiguous
        ws.refresh_from_db()
        self.assertEqual(ws.delivery_state, DS.CONNECTED)   # HELD — never flapped to DISCONNECTED
        self.assertEqual(out["held"], 1)
        self.assertEqual(out["disconnected"], 0)

    @override_settings(**_ALL_ON)
    def test_I2_steady_connected_does_not_rewrite_or_churn(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.CONNECTED
        ws.save(update_fields=["canonical_state", "delivery_state"])
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()), \
             mock.patch("hosted_workspace.delivery_persistence.record_remoteapp_connected") as rc:
            out = run_hosted_delivery_observe(observe_session_fn=lambda w: "CONNECTED")
        rc.assert_not_called()                 # transition-only: no re-write / no telemetry churn
        self.assertEqual(out["held"], 1)

    @override_settings(**_ALL_ON)
    def test_J_producer_goes_only_through_the_single_writer(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.save(update_fields=["canonical_state"])
        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids", return_value=frozenset()), \
             mock.patch("hosted_workspace.delivery_persistence.record_remoteapp_connected") as rc:
            run_hosted_delivery_observe(observe_session_fn=lambda w: "CONNECTED")
        rc.assert_called_once()
        _, kwargs = rc.call_args
        self.assertIn("event_seq", kwargs)     # monotonic seq supplied to the single writer


# ── K: broker bind acknowledges + persists WITHOUT advancing from untrusted observation ──────────────────
class BindAcknowledgementTests(TestCase):
    @override_settings(**_ALL_ON)
    def test_K_bind_persists_identity_without_advancing_delivery_or_canonical(self):
        from hosted_workspace.provisioning import bind_broker_identity
        # A fresh deferred-bind account: NO broker number yet (bind is the first, write-once, write).
        node = _node(hostname="node-k", rdp_host="10.9.9.9")
        acct = TradingAccount.objects.create(
            user=_user("kk"), name="Hosted", broker_name="Hosted", account_number="",
            is_demo=True, is_active=False, readiness_provider=PERSISTENT_WORKSPACE)
        ws = HostedMt5Workspace.objects.create(trading_account=acct)
        ws.execution_node = node
        ws.workspace_node = node
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["execution_node", "workspace_node", "canonical_state", "delivery_state"])
        res = bind_broker_identity(acct.user, ws, expected_login="1302587", expected_server="IS6Technologies-Demo")
        acct.refresh_from_db(); ws.refresh_from_db()
        self.assertTrue(getattr(res, "ok", False))
        self.assertEqual(acct.account_number, "1302587")                   # persisted (write-once)
        self.assertEqual(ws.delivery_state, DS.NONE)                       # NOT advanced by the bind
        self.assertEqual(ws.canonical_state, S.WAITING_FOR_LOGIN.value)    # identity never from observation


# ── L: Customer Zero + flag-OFF byte-identical ───────────────────────────────────────────────────────────
class CustomerZeroAndDarkTests(TestCase):
    @override_settings(**dict(_FLAGS_ON, HOSTED_MT5_REMOTEAPP_ENABLED="1", HOSTED_SLOT_PREP_ENABLED="1"))
    def test_L_flag_off_no_deliverable_no_observer_required_byte_identical(self):
        # Flag OFF: never DELIVERABLE (deadlock-break inert), observer stays best-effort deferred (prepared).
        ws, acct, _ = _bound()
        _provisioned(acct)
        ws.delivery_state = DS.NONE
        with mock.patch.dict(os.environ, _GUAC, clear=False):
            self.assertNotEqual(delivery_readiness(ws), DELIVERY_DELIVERABLE)
        # observer non-blocking when the flag is off, even if the host has no register_observer method.
        res = SP.prepare_hosted_slot(ws, executor=FakeExecutor(drop=("register_observer",)))
        self.assertTrue(res.prepared)
        self.assertTrue(res.observer_deferred)
        self.assertFalse(ws.stage_timings.filter(stage=STAGE_OBSERVER_PREPARED).exists())

    @override_settings(**_ALL_ON)
    def test_L2_customer_zero_excluded_from_delivery_producer(self):
        ws, acct, _ = _bound()
        ws.canonical_state = S.WAITING_FOR_LOGIN.value
        ws.delivery_state = DS.NONE
        ws.save(update_fields=["canonical_state", "delivery_state"])
        called = {"n": 0}

        def _never(w):
            called["n"] += 1
            return "CONNECTED"

        with mock.patch("hosted_workspace.tenant_isolation.customer_zero_account_ids",
                        return_value=frozenset({acct.pk})):
            out = run_hosted_delivery_observe(observe_session_fn=_never)
        ws.refresh_from_db()
        self.assertEqual(ws.delivery_state, DS.NONE)     # CZ byte-identical — never touched
        self.assertEqual(called["n"], 0)                 # never even observed
        self.assertEqual(out["cz_skipped"], 1)
        self.assertEqual(out["connected"], 0)

    def test_L3_delivery_producer_dark_when_flag_off(self):
        out = run_hosted_delivery_observe(observe_session_fn=lambda w: "CONNECTED")
        self.assertEqual(out, {"enabled": False, "polled": 0, "connected": 0, "disconnected": 0,
                               "held": 0, "cz_skipped": 0, "errors": 0})
