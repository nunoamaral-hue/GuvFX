"""hosted_workspace.corroboration_diagnosis — WHY-not-CONNECTED diagnosis (DARK, read-only).

Proves:
  * the FAITHFUL classifier returns the EXACT reason code for each failed corroboration gate (constructed per
    gate), mirroring ``live_observe.corroboration_matches`` + ``build_observation_from_host``;
  * the STATE-DERIVED diagnosis maps the durable persisted fields to the right code (incl. the Account-49
    ``no_runtime_populated`` case);
  * the output is REDACTED — no full login, Windows username, runtime/terminal path, or secret appears;
  * the diagnosis MUTATES NOTHING (delivery_state / proj_* unchanged after a run);
  * the fleet metrics are fail-open and count the stuck states.
"""
import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from execution.readiness import PERSISTENT_WORKSPACE
from trading.models import BrokerServer, TradingAccount

from hosted_workspace import corroboration_diagnosis as CD
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

U = get_user_model()

# Sentinels a redacted payload must NEVER contain.
_SECRET_LOGIN = "700900123"           # a distinctive full login number
_SECRET_SERVER = "IS6-Demo"


def _user(name):
    return U.objects.create_user(username=name, email=f"{name}@x.invalid", password="x")


def _acct(user, *, login=_SECRET_LOGIN, server_name=_SECRET_SERVER, env=BrokerServer.DEMO, is_demo=True):
    srv, _ = BrokerServer.objects.get_or_create(server_name=server_name, defaults={"environment": env})
    if srv.environment != env:
        srv.environment = env
        srv.save(update_fields=["environment"])
    return TradingAccount.objects.create(
        user=user, name="B", broker_name="B", account_number=login, is_demo=is_demo,
        broker_server=srv, readiness_provider=PERSISTENT_WORKSPACE, is_active=False)


def _ws(acct, **kw):
    return HostedMt5Workspace.objects.create(trading_account=acct, **kw)


def _ok_corroboration(acct_id, now_ts, **override):
    corr = {
        "account_id": acct_id,
        "process_present": True,
        "owner_user": f"guvfx_u_{acct_id}",
        "session_id": 3,
        "runtime_root": rf"C:\GuvFX\accounts\{acct_id}",
        "collected_at": now_ts,
        "remote_endpoints": ["203.0.113.10:443"],   # a routable PUBLIC endpoint → network active
    }
    corr.update(override)
    return corr


def _ok_result(acct_id, now_ts, *, login=_SECRET_LOGIN, server=_SECRET_SERVER, trade_mode=0,
               connected=True, corr_override=None, **override):
    result = {
        "ok": True,
        "terminal_connected": connected,
        "attach_attempted": True,
        "attach_succeeded": True,
        "ipc_available": True,
        "process_running": True,
        "trade_allowed": True,
        "observed_login": login,
        "observed_server": server,
        "observed_trade_mode": trade_mode,
        "corroboration": _ok_corroboration(acct_id, now_ts, **(corr_override or {})),
    }
    result.update(override)
    return result


class ClassifierAccuracyTests(TestCase):
    """Each gate, constructed in its failed state, yields the EXACT code. Positive control proves the path
    can reach CORROBORATED (RULE 11)."""

    def setUp(self):
        self.acct = _acct(_user("c1"))
        self.ws = _ws(self.acct, canonical_state=S.WAITING_FOR_LOGIN)
        self.aid = self.acct.id
        self.now = timezone.now().timestamp()

    def _code(self, result):
        code, _ev = CD.classify_corroboration_result(result, workspace=self.ws, now=self.now)
        return code

    def test_positive_control_corroborated(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now)), CD.CORROBORATED)

    def test_observer_task_not_scheduled(self):
        self.assertEqual(self._code({"ok": False, "reason": "no_observer_task"}),
                         CD.OBSERVER_TASK_NOT_SCHEDULED)

    def test_observe_unavailable_other_reason(self):
        self.assertEqual(self._code({"ok": False, "reason": "host_unavailable"}), CD.OBSERVE_UNAVAILABLE)

    def test_no_runtime_populated_missing_corroboration(self):
        r = _ok_result(self.aid, self.now); r["corroboration"] = None
        self.assertEqual(self._code(r), CD.NO_RUNTIME_POPULATED)

    def test_no_runtime_populated_wrong_account(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"account_id": self.aid + 7})),
                         CD.NO_RUNTIME_POPULATED)

    def test_no_slot_process(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"process_present": False})),
                         CD.NO_SLOT_PROCESS_OR_SESSION)

    def test_no_slot_session_not_interactive(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"session_id": 0})),
                         CD.NO_SLOT_PROCESS_OR_SESSION)

    def test_wrong_session_owner(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"owner_user": "guvfx_u_99999"})),
                         CD.WRONG_SESSION_OWNER)

    def test_runtime_root_mismatch(self):
        self.assertEqual(
            self._code(_ok_result(self.aid, self.now, corr_override={"runtime_root": r"C:\Windows\Temp\evil"})),
            CD.RUNTIME_ROOT_MISMATCH)

    def test_stale_heartbeat_missing_anchor(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"collected_at": None})),
                         CD.STALE_HEARTBEAT)

    def test_stale_heartbeat_too_old(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, corr_override={"collected_at": self.now - 600})),
                         CD.STALE_HEARTBEAT)

    def test_connection_not_corroborated_private_only(self):
        # tenant claims connected but LocalSystem saw only a PRIVATE endpoint → the claim is refused.
        r = _ok_result(self.aid, self.now, corr_override={"remote_endpoints": ["10.0.0.5:443"]})
        self.assertEqual(self._code(r), CD.CONNECTION_NOT_CORROBORATED)

    def test_broker_not_connected(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, connected=False)), CD.BROKER_NOT_CONNECTED)

    def test_ipc_not_ready(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, attach_succeeded=False)), CD.IPC_NOT_READY)

    def test_identity_login_mismatch(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, login="555555")), CD.IDENTITY_LOGIN_MISMATCH)

    def test_identity_server_mismatch(self):
        self.assertEqual(self._code(_ok_result(self.aid, self.now, server="OtherBroker-Demo")),
                         CD.IDENTITY_SERVER_MISMATCH)

    def test_environment_unresolved_classification_mismatch(self):
        # Clean demo account env, but the OBSERVED terminal reports a REAL trade_mode ⇒ classification mismatch
        # (demo-only default when HOSTED_LIVE_MONITORING_ENABLED is off) ⇒ environment_unresolved.
        self.assertEqual(self._code(_ok_result(self.aid, self.now, trade_mode=2)), CD.ENVIRONMENT_UNRESOLVED)

    def test_live_account_flag_off_not_corroborated(self):
        # Clean LIVE account correctly connected on a LIVE terminal, but HOSTED_LIVE_MONITORING_ENABLED is OFF
        # (the DARK default): the producer keeps matching demo-only, so this is NOT corroborated — guards against
        # the false-'corroborated' the adversarial review found. The classifier mirrors the producer exactly.
        acct = _acct(_user("c3"), login="900900", server_name="Clean-Live", env=BrokerServer.LIVE, is_demo=False)
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN)
        result = _ok_result(acct.id, self.now, login="900900", server="Clean-Live", trade_mode=2)
        code, _ = CD.classify_corroboration_result(result, workspace=ws, now=self.now)
        self.assertEqual(code, CD.ENVIRONMENT_UNRESOLVED)

    @override_settings(HOSTED_LIVE_MONITORING_ENABLED="1")
    def test_live_account_flag_on_corroborated(self):
        # Same clean LIVE account + LIVE terminal, now with live-monitoring ON ⇒ the producer opts the LIVE
        # account in and it MATCHES ⇒ corroborated. Proves the classifier is faithful in BOTH flag states.
        acct = _acct(_user("c4"), login="901901", server_name="Clean-Live2", env=BrokerServer.LIVE, is_demo=False)
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN)
        result = _ok_result(acct.id, self.now, login="901901", server="Clean-Live2", trade_mode=2)
        code, _ = CD.classify_corroboration_result(result, workspace=ws, now=self.now)
        self.assertEqual(code, CD.CORROBORATED)

    def test_inconsistent_demo_account_matches_demo_only(self):
        # is_demo=True bound to a LIVE server (integrity-inconsistent) but observed on a DEMO terminal: the
        # producer keeps every account demo-only by default, so the classifier faithfully reports CORROBORATED
        # (the state-derived diagnose_workspace lens separately flags the env config as environment_unresolved).
        acct = _acct(_user("c5"), login="902902", server_name="Inconsistent-Live", env=BrokerServer.LIVE, is_demo=True)
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN)
        result = _ok_result(acct.id, self.now, login="902902", server="Inconsistent-Live", trade_mode=0)
        code, _ = CD.classify_corroboration_result(result, workspace=ws, now=self.now)
        self.assertEqual(code, CD.CORROBORATED)

    def test_identity_not_observable(self):
        # Connected + IPC up, but the active account id is not yet readable (observed login/server absent) ⇒ the
        # distinct benign 'identity_not_observable', NOT a mislabelled 'broker_not_connected'.
        self.assertEqual(self._code(_ok_result(self.aid, self.now, login=None, server=None)),
                         CD.IDENTITY_NOT_OBSERVABLE)

    def test_gate_order_structural_before_identity(self):
        # A result failing BOTH a structural gate (owner) AND identity must report the STRUCTURAL code.
        r = _ok_result(self.aid, self.now, login="555555", corr_override={"owner_user": "guvfx_u_99999"})
        self.assertEqual(self._code(r), CD.WRONG_SESSION_OWNER)


class StateDerivedDiagnosisTests(TestCase):
    """The persisted-state diagnosis maps durable fields → the right code without any host contact."""

    def setUp(self):
        self.now = timezone.now()

    def _diag(self, ws):
        return CD.diagnose_workspace(ws, now=self.now)

    def test_no_runtime_populated_account_49_case(self):
        # AUTHORIZED / WAITING_FOR_LOGIN, never observed (observation_version 0) → no_runtime_populated.
        acct = _acct(_user("s1"))
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN,
                 delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED, proj_connected=False)
        d = self._diag(ws)
        self.assertEqual(d["reason"], CD.NO_RUNTIME_POPULATED)
        self.assertFalse(d["observed"])
        self.assertTrue(d["is_fault"])

    def test_stale_heartbeat(self):
        acct = _acct(_user("s2"), login="810810")
        ws = _ws(acct, canonical_state=S.CONNECTED, observation_version=5,
                 proj_process_running=True, proj_ipc_available=True, proj_connected=True, proj_account_match=True)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(
            last_decision_at=self.now - timezone.timedelta(seconds=CD.STATE_STALE_AFTER_SECONDS + 120))
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.STALE_HEARTBEAT)

    def test_environment_unresolved(self):
        acct = _acct(_user("s3"), login="820820", server_name="SM-Live", env=BrokerServer.LIVE, is_demo=True)
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN, observation_version=3)
        self.assertEqual(self._diag(ws)["reason"], CD.ENVIRONMENT_UNRESOLVED)

    def test_no_slot_process(self):
        acct = _acct(_user("s4"), login="830830")
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN, observation_version=2, proj_process_running=False)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.NO_SLOT_PROCESS_OR_SESSION)

    def test_ipc_not_ready(self):
        acct = _acct(_user("s5"), login="840840")
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN, observation_version=2,
                 proj_process_running=True, proj_ipc_available=False)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.IPC_NOT_READY)

    def test_broker_not_connected(self):
        acct = _acct(_user("s6"), login="850850")
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN, observation_version=2,
                 proj_process_running=True, proj_ipc_available=True, proj_connected=False)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.BROKER_NOT_CONNECTED)

    def test_identity_server_mismatch_from_attached_fields(self):
        acct = _acct(_user("s7"), login="860860")
        ws = _ws(acct, canonical_state=S.CONNECTED, observation_version=2,
                 proj_process_running=True, proj_ipc_available=True, proj_connected=True, proj_account_match=False,
                 currently_attached_server="WrongBroker-Demo")
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.IDENTITY_SERVER_MISMATCH)

    def test_identity_login_mismatch_default_when_no_observed_identity(self):
        acct = _acct(_user("s8"), login="870870")
        ws = _ws(acct, canonical_state=S.CONNECTED, observation_version=2,
                 proj_process_running=True, proj_ipc_available=True, proj_connected=True, proj_account_match=False)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        self.assertEqual(self._diag(ws)["reason"], CD.IDENTITY_LOGIN_MISMATCH)

    def test_corroborated(self):
        acct = _acct(_user("s9"), login="880880")
        ws = _ws(acct, canonical_state=S.EXECUTION_READY, observation_version=4,
                 proj_process_running=True, proj_ipc_available=True, proj_connected=True,
                 proj_account_match=True, proj_trade_allowed=True)
        HostedMt5Workspace.objects.filter(pk=ws.pk).update(last_decision_at=self.now)
        ws.refresh_from_db()
        d = self._diag(ws)
        self.assertEqual(d["reason"], CD.CORROBORATED)
        self.assertFalse(d["is_fault"])


class RedactionTests(TestCase):
    """No full login, Windows username, runtime/terminal path, or secret in any diagnosis output."""

    def _assert_clean(self, blob: str):
        for forbidden in (_SECRET_LOGIN, "guvfx_u_", r"C:\GuvFX", "C:\\\\GuvFX", "accounts\\",
                          "password", "secret", "keyring"):
            self.assertNotIn(forbidden, blob, f"redaction leak: {forbidden!r}")

    def test_state_diagnosis_redacted(self):
        acct = _acct(_user("r1"))
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN,
                 currently_attached_login=_SECRET_LOGIN, currently_attached_server=_SECRET_SERVER)
        d = CD.diagnose_workspace(ws)
        self._assert_clean(json.dumps(d))

    def test_classifier_evidence_redacted(self):
        acct = _acct(_user("r2"))
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN)
        now = timezone.now().timestamp()
        # A fully-formed result carrying the real login/username/path in the corroboration — the evidence must
        # still echo NONE of them (only booleans/ages).
        for result in (_ok_result(acct.id, now),
                       _ok_result(acct.id, now, login="555555"),
                       _ok_result(acct.id, now, corr_override={"owner_user": "guvfx_u_99999"})):
            _code, ev = CD.classify_corroboration_result(result, workspace=ws, now=now)
            self._assert_clean(json.dumps(ev))

    def test_metrics_redacted(self):
        acct = _acct(_user("r3"))
        _ws(acct, canonical_state=S.WAITING_FOR_LOGIN, currently_attached_login=_SECRET_LOGIN)
        self._assert_clean(json.dumps(CD.observation_health_metrics()))


class NoMutationTests(TestCase):
    """A diagnosis run leaves delivery_state / proj_* / canonical_state untouched."""

    def test_diagnose_and_metrics_mutate_nothing(self):
        acct = _acct(_user("m1"))
        ws = _ws(acct, canonical_state=S.WAITING_FOR_LOGIN,
                 delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED,
                 proj_connected=False, proj_account_match=False, observation_version=0)
        before = {
            "delivery_state": ws.delivery_state, "canonical_state": ws.canonical_state,
            "proj_connected": ws.proj_connected, "proj_account_match": ws.proj_account_match,
            "observation_version": ws.observation_version, "updated_at": ws.updated_at,
        }
        now = timezone.now()
        CD.diagnose_workspace(ws, now=now)
        CD.classify_corroboration_result(_ok_result(acct.id, now.timestamp()), workspace=ws, now=now.timestamp())
        CD.observation_health_metrics(now=now)
        ws.refresh_from_db()
        after = {
            "delivery_state": ws.delivery_state, "canonical_state": ws.canonical_state,
            "proj_connected": ws.proj_connected, "proj_account_match": ws.proj_account_match,
            "observation_version": ws.observation_version, "updated_at": ws.updated_at,
        }
        self.assertEqual(before, after)


class MetricsTests(TestCase):
    def test_counts_stuck_states(self):
        u = _user("mt")
        # one never-observed AUTHORIZED, one NONE, one fully corroborated.
        _ws(_acct(u, login="900001"), canonical_state=S.WAITING_FOR_LOGIN,
            delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED)
        _ws(_acct(u, login="900002"), canonical_state=S.PROVISIONING,
            delivery_state=HostedMt5Workspace.DeliveryState.NONE)
        good = _ws(_acct(u, login="900003"), canonical_state=S.EXECUTION_READY, observation_version=3,
                   delivery_state=HostedMt5Workspace.DeliveryState.CONNECTED,
                   proj_process_running=True, proj_ipc_available=True, proj_connected=True,
                   proj_account_match=True, proj_trade_allowed=True)
        HostedMt5Workspace.objects.filter(pk=good.pk).update(last_decision_at=timezone.now())
        m = CD.observation_health_metrics()
        self.assertEqual(m["workspace_total"], 3)
        self.assertEqual(m["never_observed"], 2)
        self.assertEqual(m["stuck_authorized"], 1)
        self.assertEqual(m["stuck_none"], 1)
        self.assertGreaterEqual(m["reason_code_counts"].get(CD.NO_RUNTIME_POPULATED, 0), 2)
        # snapshot_age is a read-only cross-app read (fail-open) — present (dict) or None, never raises.
        self.assertTrue(m["snapshot_age"] is None or isinstance(m["snapshot_age"], dict))


class CommandTests(TestCase):
    """The operator command runs read-only, prints reason codes + metrics, and leaks no secret."""

    def test_command_runs_and_is_redacted(self):
        u = _user("cmd")
        _ws(_acct(u, login=_SECRET_LOGIN), canonical_state=S.WAITING_FOR_LOGIN,
            delivery_state=HostedMt5Workspace.DeliveryState.AUTHORIZED,
            currently_attached_login=_SECRET_LOGIN)
        out = StringIO()
        call_command("hosted_observation_diagnose", stdout=out)
        text = out.getvalue()
        self.assertIn("no_runtime_populated", text)
        self.assertIn("fleet health metrics", text)
        for forbidden in (_SECRET_LOGIN, "guvfx_u_", r"C:\GuvFX", "password"):
            self.assertNotIn(forbidden, text)

    def test_command_single_account(self):
        acct = _acct(_user("cmd2"), login="910910")
        _ws(acct, canonical_state=S.WAITING_FOR_LOGIN)
        out = StringIO()
        call_command("hosted_observation_diagnose", account=acct.id, stdout=out)
        self.assertIn("reason=", out.getvalue())
