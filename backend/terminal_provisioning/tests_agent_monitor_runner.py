"""Monitoring-Runner WS-B/C/D/G/I/K — the runner orchestration, the durable state model, the ops-evidence
presenter, and the two management commands.

The runner is the packet's core safety boundary; these tests prove: it is inert when disabled; it never
false-pages a misconfigured monitor as agent-down; hysteresis + cooldown survive across passes; recovery
fires exactly once; a real delivery failure is surfaced (exit 40) but a NULL/dry-run channel is not; the
single-flight lock refuses overlap (exit 50); and it never touches a broker/customer surface.
"""
from __future__ import annotations

import types
from unittest import mock

from django.core.management import call_command
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings

from terminal_provisioning import agent_health_probe as probe
from terminal_provisioning import agent_monitor_runner as runner
from terminal_provisioning.agent_alert_sink import DeliveryResult, NullAlertSink
from terminal_provisioning.models import AgentMonitorState


def _fake_state(**over):
    base = dict(current_state="", previous_state="", current_band="", supervised=None,
                consecutive_healthy=0, consecutive_unavailable=0, flap_count=0, alerting=False,
                last_reason="", last_alerts={}, last_delivery="", last_probe_at=None, last_healthy_at=None,
                last_transition_at=None, run_count=0)
    base.update(over)
    return types.SimpleNamespace(**base)


def _ready(state):
    band = probe._BAND[state]
    return probe.AgentReadiness(state=state, band=band, supervised=(state == probe.HEALTHY),
                               validate_login_available=(state == probe.HEALTHY), reason=f"r_{state.lower()}",
                               correlation_id="corr", elapsed_ms=1, probed_at=0.0, layers={})


class _RecordingSink(NullAlertSink):
    channel = "recording"

    def __init__(self, *, result=None):
        self.calls = []
        self._result = result

    def deliver(self, alert, *, now, correlation_id=""):
        self.calls.append(alert.name)
        if self._result is not None:
            return self._result
        return DeliveryResult(delivered=True, channel=self.channel)


_ENABLED = runner.MonitorConfig(enabled=True, cooldown_seconds=900, probe_interval_seconds=60)


# ────────────────────────── run_once ──────────────────────────
class RunOnceTests(SimpleTestCase):
    def test_disabled_is_inert(self):
        st = _fake_state()
        sink = _RecordingSink()
        o = runner.run_once(state=st, sink=sink, now=1.0,
                            config=runner.MonitorConfig(enabled=False),
                            synthetic_readiness=_ready(probe.UNREACHABLE))
        self.assertEqual(o.status, runner.STATUS_DISABLED)
        self.assertEqual(o.exit_code, 0)
        self.assertEqual(sink.calls, [])                 # nothing probed, nothing delivered
        self.assertEqual(st.run_count, 0)

    def test_unconfigured_is_config_error_not_a_false_agent_down(self):
        st = _fake_state()
        sink = _RecordingSink()
        o = runner.run_once(state=st, sink=sink, now=1.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.UNCONFIGURED))
        self.assertEqual(o.status, runner.STATUS_CONFIG_ERROR)
        self.assertEqual(o.exit_code, 20)
        self.assertEqual(sink.calls, [])                 # a misconfigured MONITOR must not page "agent down"

    def test_healthy_no_alert(self):
        st = _fake_state()
        sink = _RecordingSink()
        o = runner.run_once(state=st, sink=sink, now=1.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.HEALTHY))
        self.assertEqual(o.status, runner.STATUS_HEALTHY)
        self.assertEqual(o.exit_code, 0)
        self.assertEqual(sink.calls, [])
        self.assertTrue(st.last_healthy_at)

    def test_unreachable_fires_and_delivers(self):
        st = _fake_state()
        sink = _RecordingSink()
        o = runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.UNREACHABLE))
        self.assertEqual(o.status, runner.STATUS_AGENT_UNHEALTHY)
        self.assertEqual(o.exit_code, 10)
        self.assertIn("agent_down", sink.calls)
        self.assertTrue(st.alerting)

    def test_durable_cooldown_suppresses_repeat(self):
        st = _fake_state()
        sink = _RecordingSink()
        runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.UNREACHABLE))
        sink.calls.clear()
        o2 = runner.run_once(state=st, sink=sink, now=1030.0, config=_ENABLED,
                             synthetic_readiness=_ready(probe.UNREACHABLE))
        self.assertEqual(sink.calls, [])                 # within the 900s window
        self.assertEqual(o2.alerts_delivered, 0)

    def test_recovery_fires_once_after_two_healthy(self):
        st = _fake_state()
        sink = _RecordingSink()
        runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.UNREACHABLE))
        runner.run_once(state=st, sink=sink, now=1060.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))   # 1 healthy — still alerting
        self.assertTrue(st.alerting)
        sink.calls.clear()
        runner.run_once(state=st, sink=sink, now=1120.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))   # 2 healthy — recovered
        self.assertFalse(st.alerting)
        self.assertEqual(sink.calls, ["agent_recovered"])
        # the cooldown map is NEVER blanket-cleared: recovery stamps its OWN cooldown and the outage entry
        # ages out per-name (so a rapid re-flap is covered by crash-loop, not an agent_down storm).
        self.assertIn("agent_recovered", st.last_alerts)
        self.assertIn("agent_down", st.last_alerts)

    def test_recovery_is_cooldown_suppressed_on_rapid_reflap(self):
        st = _fake_state()
        sink = _RecordingSink()
        # outage -> 2 healthy => recovery #1 delivered and stamped
        runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.UNREACHABLE))
        runner.run_once(state=st, sink=sink, now=1060.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))
        runner.run_once(state=st, sink=sink, now=1120.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))
        # flap back down, then recover again inside the cooldown window
        runner.run_once(state=st, sink=sink, now=1180.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.UNREACHABLE))
        runner.run_once(state=st, sink=sink, now=1240.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))
        sink.calls.clear()
        runner.run_once(state=st, sink=sink, now=1300.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.HEALTHY))   # recovery #2 within 900s cooldown
        self.assertNotIn("agent_recovered", sink.calls)   # suppressed — no recovery storm

    def test_real_delivery_failure_is_exit_40(self):
        st = _fake_state()
        sink = _RecordingSink(result=DeliveryResult(delivered=False, channel="x", reason="http_500"))
        o = runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.UNREACHABLE))
        self.assertEqual(o.status, runner.STATUS_ALERT_DELIVERY_FAILURE)
        self.assertEqual(o.exit_code, 40)
        self.assertEqual(st.last_delivery, "failed")

    def test_null_channel_is_not_a_delivery_failure(self):
        st = _fake_state()
        o = runner.run_once(state=st, sink=NullAlertSink(), now=1000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.UNREACHABLE))
        # agent unhealthy + no channel configured => exit 10 (agent-unhealthy), NOT 40 (delivery failure)
        self.assertEqual(o.exit_code, 10)

    def test_crash_loop_alert_when_flapping(self):
        # seed a prior outage (band UNAVAILABLE, one prior up->down->up) then a HEALTHY probe => 2nd flap.
        st = _fake_state(current_band=probe.BAND_UNAVAILABLE, alerting=True, flap_count=1)
        sink = _RecordingSink()
        o = runner.run_once(state=st, sink=sink, now=1000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.HEALTHY))
        self.assertIn("agent_crash_loop", [a["name"] for a in o.alerts_fired])
        # a HIGH alert fired on a HEALTHY-band probe must NOT report exit 0 (status from alerts, not band).
        self.assertEqual(o.status, runner.STATUS_AGENT_UNHEALTHY)
        self.assertEqual(o.exit_code, 10)

    def test_unconfigured_preserves_hysteresis_fields(self):
        # a transient config-error must not corrupt the durable band/alerting/flap state (else the next real
        # HEALTHY probe would fabricate an up->down->up flap).
        st = _fake_state(current_band=probe.BAND_HEALTHY, consecutive_healthy=3, flap_count=1)
        runner.run_once(state=st, sink=_RecordingSink(), now=1.0, config=_ENABLED,
                        synthetic_readiness=_ready(probe.UNCONFIGURED))
        self.assertEqual(st.current_band, probe.BAND_HEALTHY)   # untouched
        self.assertEqual(st.consecutive_healthy, 3)
        self.assertEqual(st.flap_count, 1)
        self.assertEqual(st.current_state, probe.UNCONFIGURED)

    def test_flap_counter_decays_after_sustained_health(self):
        # a high lifetime flap count must NOT keep firing crash-loop forever once the agent is stable.
        st = _fake_state(current_band=probe.BAND_HEALTHY, flap_count=9,
                         consecutive_healthy=runner.FLAP_DECAY_HEALTHY_STREAK - 1)
        o = runner.run_once(state=st, sink=_RecordingSink(), now=1000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.HEALTHY))
        self.assertEqual(st.flap_count, 0)
        self.assertNotIn("agent_crash_loop", [a["name"] for a in o.alerts_fired])

    def test_stale_detection_off_by_default(self):
        st = _fake_state(last_probe_at=_dt(0.0))
        o = runner.run_once(state=st, sink=_RecordingSink(), now=100000.0, config=_ENABLED,
                            synthetic_readiness=_ready(probe.HEALTHY))
        self.assertFalse(o.stale)

    def test_stale_detection_on_flags_a_coverage_gap(self):
        cfg = runner.MonitorConfig(enabled=True, probe_interval_seconds=60, stale_detection_enabled=True)
        st = _fake_state(last_probe_at=_dt(0.0))
        o = runner.run_once(state=st, sink=_RecordingSink(), now=100000.0, config=cfg,
                            synthetic_readiness=_ready(probe.HEALTHY))
        self.assertTrue(o.stale)


def _dt(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


# ────────────────────────── state_evidence ──────────────────────────
class StateEvidenceTests(SimpleTestCase):
    def test_evidence_has_no_secret_fields(self):
        st = _fake_state(current_state=probe.HEALTHY, current_band=probe.BAND_HEALTHY, run_count=5)
        ev = runner.state_evidence(st, config=_ENABLED, now=10.0, sink_channel="telegram", sink_owner="nuno")
        blob = repr(ev).lower()
        for forbidden in ("token", "chat_id", "keyring", "password", "secret", "bot"):
            self.assertNotIn(forbidden, blob)
        for key in ("current_state", "current_band", "run_count", "alert_channel", "last_probe_age_seconds"):
            self.assertIn(key, ev)


# ────────────────────────── durable model ──────────────────────────
class AgentMonitorStateModelTests(TestCase):
    def test_singleton_pk_forced(self):
        s = AgentMonitorState(current_state="HEALTHY")
        s.save()
        self.assertEqual(s.pk, AgentMonitorState.SINGLETON_ID)
        self.assertEqual(AgentMonitorState.objects.count(), 1)

    def test_load_is_idempotent(self):
        a = AgentMonitorState.load()
        b = AgentMonitorState.load()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(AgentMonitorState.objects.count(), 1)


# ────────────────────────── run_agent_readiness_probe command ──────────────────────────
@override_settings(VALIDATION_AGENT_MONITORING_ENABLED=True, AGENT_ALERT_SINK="null")
class ProbeCommandTests(TestCase):
    def _run(self, **kw):
        try:
            call_command("run_agent_readiness_probe", **kw)
            return 0
        except SystemExit as e:
            return int(e.code)

    @override_settings(VALIDATION_AGENT_MONITORING_ENABLED=False)
    def test_disabled_exits_zero(self):
        self.assertEqual(self._run(**{"synthetic_state": probe.UNREACHABLE}), 0)

    def test_synthetic_unreachable_exit_10(self):
        # null sink => agent unhealthy but ran; no network, no delivery
        self.assertEqual(self._run(**{"synthetic_state": probe.UNREACHABLE, "dry_run": True}), 10)

    def test_synthetic_healthy_exit_0(self):
        self.assertEqual(self._run(**{"synthetic_state": probe.HEALTHY}), 0)

    def test_synthetic_never_persists_to_the_singleton(self):
        # a synthetic run must NOT mutate the durable row (it must be unable to mask a real outage).
        self._run(**{"synthetic_state": probe.UNREACHABLE})
        st = AgentMonitorState.objects.get(pk=AgentMonitorState.SINGLETON_ID)
        self.assertEqual(st.current_state, "")            # migration-seeded default, untouched
        self.assertEqual(st.run_count, 0)

    @override_settings(BETA_AGENT_BASE_URL="", BETA_AGENT_KEYRING="", BETA_AGENT_KEY_ID="")
    def test_real_unconfigured_persists_config_error_exit_20(self):
        # the real probe path with no agent config resolves UNCONFIGURED (no network) and persists it.
        self.assertEqual(self._run(), 20)
        st = AgentMonitorState.objects.get(pk=AgentMonitorState.SINGLETON_ID)
        self.assertEqual(st.current_state, probe.UNCONFIGURED)
        self.assertGreaterEqual(st.run_count, 1)

    def test_overlap_refused_exit_50(self):
        # a nowait lock-contention error on the real path => immediate exit 50
        with mock.patch.object(AgentMonitorState.objects, "select_for_update") as sfu:
            sfu.return_value.get.side_effect = DatabaseError("could not obtain lock on row of relation")
            self.assertEqual(self._run(), 50)

    def test_non_lock_db_error_is_probe_failure_exit_30(self):
        # a NON-lock DB error must NOT be masked as a routine overlap; it is a real monitor failure (exit 30)
        with mock.patch.object(AgentMonitorState.objects, "select_for_update") as sfu:
            sfu.return_value.get.side_effect = DatabaseError("server closed the connection unexpectedly")
            self.assertEqual(self._run(), 30)


# ────────────────────────── test_agent_alert_delivery command ──────────────────────────
class AlertDeliveryCommandTests(TestCase):
    @override_settings(AGENT_ALERT_SINK="null")
    def test_null_sink_is_exit_zero_and_no_state_change(self):
        before = AgentMonitorState.objects.count()
        try:
            call_command("test_agent_alert_delivery", **{"correlation_id": "t1"})
            code = 0
        except SystemExit as e:
            code = int(e.code)
        self.assertEqual(code, 0)                        # no_channel_configured is not a failure
        self.assertEqual(AgentMonitorState.objects.count(), before)   # NO state change

    @override_settings(AGENT_ALERT_SINK="logging", AGENT_ALERT_OWNER="nuno")
    def test_logging_sink_delivers_exit_zero(self):
        try:
            call_command("test_agent_alert_delivery", **{"severity": "MEDIUM"})
            code = 0
        except SystemExit as e:
            code = int(e.code)
        self.assertEqual(code, 0)


# ────────────────────────── agent_monitor_status command ──────────────────────────
class StatusCommandTests(TestCase):
    def test_status_json_runs(self):
        AgentMonitorState.load()
        call_command("agent_monitor_status", **{"json": True})   # must not raise
