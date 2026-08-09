"""Minimum-hardening tests (WS-K) — lifecycle logging, single-instance guard, launch enforcement, the signed
readiness probe (8 states + cadence + hysteresis), monitoring metric/alert computation, and alert delivery.

Bundle modules (``agent_lifecycle``, ``agent``) are imported from ``deploy/beta-agent`` exactly as the B2
service tests do; backend modules import normally. Everything is exercised OFF the Windows host with injected
transports/fs/clock — no network, no real agent, no #12/#1.
"""
import json
import logging
import os
import sys
import tempfile
import time as _time
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from . import agent_alert_sink as sink_mod
from . import agent_health_probe as probe
from . import agent_monitoring as mon
from . import agent_status_presenter as presenter
from .mgmt_client import provision_url
from .mgmt_protocol import PROTOCOL_VERSION, SUPPORTED_OPERATIONS

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BUNDLE = os.path.join(_REPO, "deploy", "beta-agent")
if _BUNDLE not in sys.path:
    sys.path.insert(0, _BUNDLE)

import agent as agent_mod          # noqa: E402
import agent_lifecycle as life     # noqa: E402

KEYRING = {"k1": "agent-secret-key"}
BETA_ROOT = r"C:\GuvFX\beta\accounts"


# ────────────────────────── WS-C/D — lifecycle primitives ──────────────────────────
class LifecycleEventTests(SimpleTestCase):
    def test_event_is_allowlisted_and_secret_safe(self):
        ev = life.build_event("AGENT_STARTING", now=100.0, fields={
            "pid": 7, "supervised": True, "password": "hunter2", "bind_host": "10.0.0.1",
            "keyring_json": "k", "detail": "loaded token=ABC"})
        self.assertEqual(ev["event"], "AGENT_STARTING")
        self.assertEqual(ev["ts"], 100.0)
        self.assertEqual(ev["pid"], 7)
        self.assertNotIn("password", ev)          # not allow-listed → dropped
        self.assertNotIn("keyring_json", ev)      # not allow-listed → dropped
        self.assertEqual(ev["detail"], "[REDACTED]")   # allow-listed but secret-looking → scrubbed

    def test_unknown_event_is_flagged_not_dropped(self):
        ev = life.build_event("WEIRD", now=1.0)
        self.assertEqual(ev["event"], "WEIRD")
        self.assertIn("unknown-event", ev["detail"])

    def test_append_event_never_raises_on_bad_path(self):
        life.append_event(os.path.join(tempfile.gettempdir(), "no", "such", "dir", "x.jsonl"),
                          {"a": 1})  # must not raise


class LaunchClassificationTests(SimpleTestCase):
    def test_unsupervised_manual_launch(self):
        c = life.classify_launch({})
        self.assertFalse(c["supervised"])
        self.assertEqual(c["startup_reason"], "unsanctioned_or_manual")
        self.assertFalse(life.launch_permitted(c))

    def test_supervised_requires_token_and_identity(self):
        c = life.classify_launch({"BETA_AGENT_SUPERVISED_TOKEN": "t", "BETA_AGENT_SERVICE_IDENTITY": "svc"})
        self.assertTrue(c["supervised"])
        self.assertTrue(life.launch_permitted(c))
        # token without identity is NOT supervised
        c2 = life.classify_launch({"BETA_AGENT_SUPERVISED_TOKEN": "t"})
        self.assertFalse(c2["supervised"])

    def test_override_permits_but_stays_unsupervised(self):
        c = life.classify_launch({"BETA_AGENT_LAUNCH_OVERRIDE": "1"})
        self.assertFalse(c["supervised"])       # override does NOT claim supervision
        self.assertTrue(c["override"])
        self.assertTrue(life.launch_permitted(c))


class SingleInstanceGuardTests(SimpleTestCase):
    def _fs(self):
        store = {}
        return store, (lambda p: (False if p in store else store.__setitem__(p, "x") or True)), \
            (lambda p: store.get(p, "")), (lambda p, s: store.__setitem__(p, s)), \
            (lambda p: store.pop(p, None))

    def test_first_acquire_then_live_holder_raises(self):
        store, oe, rt, wt, rm = self._fs()
        r = life.acquire_single_instance("L", pid=100, now=1.0, pid_alive=lambda p: False,
                                         open_excl=oe, read_text=rt, write_text=wt, remove=rm)
        self.assertTrue(r["acquired"])
        with self.assertRaises(life.InstanceGuardError):
            life.acquire_single_instance("L", pid=200, now=2.0, pid_alive=lambda p: True,
                                         open_excl=oe, read_text=rt, write_text=wt, remove=rm)

    def test_stale_dead_holder_is_reclaimed(self):
        store, oe, rt, wt, rm = self._fs()
        life.acquire_single_instance("L", pid=100, now=1.0, pid_alive=lambda p: False,
                                     open_excl=oe, read_text=rt, write_text=wt, remove=rm)
        r2 = life.acquire_single_instance("L", pid=300, now=3.0, pid_alive=lambda p: False,
                                          open_excl=oe, read_text=rt, write_text=wt, remove=rm)
        self.assertTrue(r2["reclaimed_stale"])


# ────────────────────────── WS-D — NEGOTIATE agent_supervised ──────────────────────────
def _server_cfg(state_db, *, host="127.0.0.1", port=0, log_dir=None, extra=None):
    cfg = {"bind_host": host, "expected_bind_host": host, "bind_port": port,
           "keyring": KEYRING, "key_id": "k1", "beta_root": BETA_ROOT,
           "tombstone_base": r"C:\GuvFX\beta\tombstones", "state_db": state_db, "manifest_path": "",
           "log_dir": log_dir, "max_body_bytes": 16384, "max_connections": 8,
           "request_timeout_s": 5, "drain_timeout_s": 5}
    if extra:
        cfg.update(extra)
    return cfg


class _FakeWin:
    def real_path(self, p): return None
    def run_task(self, *a, **k): return None


class NegotiateSupervisedFieldTests(SimpleTestCase):
    def _agent(self, supervised):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        from stores import RuntimeLockManager, SqliteStore
        return agent_mod.build_agent(
            _server_cfg(f.name), win=_FakeWin(), store=SqliteStore(f.name),
            locks=RuntimeLockManager(), enforce_integrity=False, agent_supervised=supervised)

    def _negotiate(self, a):
        from lib import mgmt_protocol as proto
        req = proto.sign_request(provisioning_job_id=1, runtime_uuid=proto.NIL_UUID, operation="NEGOTIATE",
                                 correlation_id="c", keyring=KEYRING, key_id="k1",
                                 now=int(_time.time()), ttl_seconds=30)
        return a.handle(req)

    def test_supervised_true_is_advertised(self):
        r = self._negotiate(self._agent(True))
        self.assertEqual(r["outcome"], "ok")
        self.assertIs(r["agent_supervised"], True)

    def test_supervised_false_is_advertised(self):
        r = self._negotiate(self._agent(False))
        self.assertIs(r["agent_supervised"], False)

    def test_supervised_none_is_omitted(self):
        r = self._negotiate(self._agent(None))
        self.assertNotIn("agent_supervised", r)   # un-instrumented agent never claims supervision
        self.assertEqual(set(r["supported_operations"]), set(SUPPORTED_OPERATIONS))


class AgentServerLaunchEnforcementTests(SimpleTestCase):
    def _srv(self, *, env, refuse):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        d = tempfile.mkdtemp()
        cfg = _server_cfg(f.name, log_dir=d, extra={"refuse_unsupervised_launch": refuse})
        return agent_mod.AgentServer(cfg, win=_FakeWin(), enforce_integrity=False, env=env), d

    def test_refuse_unsupervised_launch_when_gated(self):
        srv, d = self._srv(env={}, refuse=True)
        with self.assertRaises(RuntimeError):
            srv.start()
        # a durable AGENT_LAUNCH_REJECTED line was written
        log_path = os.path.join(d, "agent_lifecycle.jsonl")
        self.assertTrue(os.path.exists(log_path))
        events = [json.loads(x)["event"] for x in open(log_path, encoding="utf-8") if x.strip()]
        self.assertIn("AGENT_LAUNCH_REJECTED", events)

    def test_unsupervised_serves_when_not_gated_but_reports_unsupervised(self):
        srv, d = self._srv(env={}, refuse=False)
        srv.start()
        try:
            self.assertFalse(srv.supervised)
            log_path = os.path.join(d, "agent_lifecycle.jsonl")
            events = [json.loads(x)["event"] for x in open(log_path, encoding="utf-8") if x.strip()]
            self.assertIn("AGENT_LISTENING", events)
            self.assertIn("AGENT_READY", events)
        finally:
            srv.stop()
        events = [json.loads(x)["event"] for x in open(os.path.join(d, "agent_lifecycle.jsonl"),
                                                        encoding="utf-8") if x.strip()]
        self.assertIn("AGENT_STOPPING", events)
        self.assertIn("AGENT_STOPPED", events)

    def test_supervised_env_permits_launch(self):
        srv, d = self._srv(env={"BETA_AGENT_SUPERVISED_TOKEN": "t", "BETA_AGENT_SERVICE_IDENTITY": "svc"},
                           refuse=True)
        srv.start()
        try:
            self.assertTrue(srv.supervised)
        finally:
            srv.stop()


# ────────────────── adversarial-review fixes (2026-08-06) ──────────────────
class ExclusiveBindTests(SimpleTestCase):
    def test_server_does_not_set_so_reuseaddr(self):
        # allow_reuse_address MUST be False so the exclusive OS bind is the real hard single-instance guard
        # (SO_REUSEADDR would let a 2nd process hijack :8791 on Windows — adversarial finding #1).
        self.assertIs(agent_mod.BoundedThreadingHTTPServer.allow_reuse_address, False)


class AdvisoryLockTests(SimpleTestCase):
    def test_start_proceeds_when_lock_conflict_is_advisory(self):
        # A single-instance lock held by a (mocked) foreign LIVE pid must NOT veto startup — the exclusive
        # bind arbitrates; a false/stale lock can never brick a start on a free port (finding #2).
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        d = tempfile.mkdtemp()
        cfg = _server_cfg(f.name, log_dir=d)
        # pre-create the lock file as if a foreign pid holds it
        lock = os.path.join(d, f"agent_instance_{cfg['bind_port']}.lock")
        # bind_port is 0 here so name uses 0; write the lock at that resolved path
        with open(lock, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": 999999, "ts": 1.0}))
        srv = agent_mod.AgentServer(cfg, win=_FakeWin(), enforce_integrity=False, env={})
        with mock.patch.object(agent_mod, "_pid_alive", return_value=True):
            srv.start()   # must NOT raise
        try:
            self.assertIsNotNone(srv._httpd)
            events = [json.loads(x)["event"] for x in open(os.path.join(d, "agent_lifecycle.jsonl"),
                                                            encoding="utf-8") if x.strip()]
            self.assertIn("AGENT_DEGRADED", events)   # conflict was recorded, not fatal
            self.assertIn("AGENT_READY", events)
        finally:
            srv.stop()


class CrashDetectionTests(SimpleTestCase):
    def test_abnormal_serve_exit_emits_crash_and_flags(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        d = tempfile.mkdtemp()
        srv = agent_mod.AgentServer(_server_cfg(f.name, log_dir=d), win=_FakeWin(),
                                    enforce_integrity=False, env={})

        class _FakeHttpd:
            def serve_forever(self):
                raise RuntimeError("boom")   # abnormal death
            def shutdown(self): pass
            def server_close(self): pass

        srv.make_server = lambda: _FakeHttpd()
        srv.start()
        for _ in range(50):
            if srv.crashed:
                break
            _time.sleep(0.01)
        self.assertTrue(srv.crashed)
        events = [json.loads(x)["event"] for x in open(os.path.join(d, "agent_lifecycle.jsonl"),
                                                        encoding="utf-8") if x.strip()]
        self.assertIn("AGENT_CRASHED", events)

    def test_clean_stop_is_not_a_crash(self):
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        srv = agent_mod.AgentServer(_server_cfg(f.name), win=_FakeWin(), enforce_integrity=False, env={})
        srv.start()
        srv.stop()
        self.assertFalse(srv.crashed)

    def test_crashed_flag_published_only_after_agent_crashed_written(self):
        # Ordering contract (deterministic, no timing): the OBSERVABLE `crashed` flag must be published ONLY
        # AFTER the AGENT_CRASHED lifecycle event is durably written — never before (the exact CI race, where
        # a poller saw crashed=True while the record was still unwritten). Spy on `_emit` to capture the flag
        # state at the instant AGENT_CRASHED is emitted: `crashed` must still be False there, while the
        # single-emit dedup claim is already set.
        f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False); f.close()
        d = tempfile.mkdtemp()
        srv = agent_mod.AgentServer(_server_cfg(f.name, log_dir=d), win=_FakeWin(),
                                    enforce_integrity=False, env={})

        class _FakeHttpd:
            def serve_forever(self):
                raise RuntimeError("boom")
            def shutdown(self): pass
            def server_close(self): pass

        srv.make_server = lambda: _FakeHttpd()
        captured = {}
        real_emit = srv._emit

        def _spy_emit(event, **fields):
            if event == "AGENT_CRASHED":
                captured["crashed_at_emit"] = srv._crashed
                captured["recorded_at_emit"] = srv._crash_recorded
            return real_emit(event, **fields)

        srv._emit = _spy_emit
        srv.start()
        for _ in range(200):
            if srv.crashed:
                break
            _time.sleep(0.01)
        self.assertTrue(srv.crashed)
        # At the moment AGENT_CRASHED was written the observable flag was NOT yet set (published only after);
        # the dedup claim was already made.
        self.assertIs(captured.get("crashed_at_emit"), False)
        self.assertIs(captured.get("recorded_at_emit"), True)
        # And once `crashed` is observable, the record is already durable on disk (no crashed-but-no-record).
        events = [json.loads(x)["event"] for x in open(os.path.join(d, "agent_lifecycle.jsonl"),
                                                        encoding="utf-8") if x.strip()]
        self.assertIn("AGENT_CRASHED", events)


# ────────────────────────── WS-B — readiness probe ──────────────────────────
def _handshake(*, supervised=True, ops=None, proto_v=PROTOCOL_VERSION, version="beta-agent-1.0.0",
               manifest="2026-08-06.1"):
    ops = ops if ops is not None else list(SUPPORTED_OPERATIONS)
    h = {"outcome": "ok", "protocol_version": proto_v, "agent_version": version,
         "manifest_version": manifest, "supported_operations": ops}
    if supervised is not None:
        h["agent_supervised"] = supervised
    return h


@override_settings(BETA_AGENT_BASE_URL="http://10.0.0.2:8791", BETA_AGENT_KEYRING=json.dumps(KEYRING), BETA_AGENT_KEY_ID="k1")  # noqa: E501
class ReadinessProbeTests(SimpleTestCase):
    def _probe(self, transport):
        return probe.probe_agent_readiness(transport=transport, now_fn=lambda: 1000.0,
                                           clock=iter([0.0, 0.05]).__next__)

    def test_healthy(self):
        r = self._probe(lambda url, req: _handshake(supervised=True))
        self.assertEqual(r.state, probe.HEALTHY)
        self.assertEqual(r.band, probe.BAND_HEALTHY)
        self.assertTrue(r.is_healthy)
        self.assertTrue(r.validate_login_available)

    def test_unsupervised_is_unavailable(self):
        r = self._probe(lambda url, req: _handshake(supervised=False))
        self.assertEqual(r.state, probe.UNSUPERVISED)
        self.assertEqual(r.band, probe.BAND_UNAVAILABLE)

    def test_supervision_unknown_is_degraded(self):
        r = self._probe(lambda url, req: _handshake(supervised=None))
        self.assertEqual(r.state, probe.SUPERVISION_UNKNOWN)
        self.assertEqual(r.band, probe.BAND_DEGRADED)

    def test_ready_unarmed_when_validate_login_absent(self):
        ops = [o for o in SUPPORTED_OPERATIONS if o != "VALIDATE_LOGIN"]
        r = self._probe(lambda url, req: _handshake(supervised=True, ops=ops))
        self.assertEqual(r.state, probe.READY_UNARMED)

    def test_incompatible_protocol(self):
        r = self._probe(lambda url, req: _handshake(proto_v=PROTOCOL_VERSION + 99))
        self.assertEqual(r.state, probe.INCOMPATIBLE)

    def test_unreachable_on_connect_failure(self):
        def t(url, req):
            raise probe._ProbeUnreachable()
        r = self._probe(t)
        self.assertEqual(r.state, probe.UNREACHABLE)
        self.assertEqual(r.band, probe.BAND_UNAVAILABLE)

    def test_listening_no_negotiate_on_read_timeout(self):
        def t(url, req):
            raise probe._ProbeReadTimeout()
        r = self._probe(t)
        self.assertEqual(r.state, probe.LISTENING_NO_NEGOTIATE)

    def test_listening_no_negotiate_on_denied_handshake(self):
        r = self._probe(lambda url, req: {"outcome": "denied", "reason_code": "bad_signature"})
        self.assertEqual(r.state, probe.LISTENING_NO_NEGOTIATE)
        self.assertEqual(r.reason, "bad_signature")

    def test_reason_is_sanitised(self):
        r = self._probe(lambda url, req: {"outcome": "denied", "reason_code": "bad sig; rm -rf /\n"})
        self.assertNotIn(" ", r.reason)
        self.assertNotIn("/", r.reason)


class ProbeUnconfiguredTests(SimpleTestCase):
    @override_settings(BETA_AGENT_BASE_URL="", BETA_AGENT_KEYRING="", BETA_AGENT_KEY_ID="")
    def test_unconfigured_is_failclosed(self):
        r = probe.probe_agent_readiness(transport=lambda u, q: _handshake(), now_fn=lambda: 5.0)
        self.assertEqual(r.state, probe.UNCONFIGURED)
        self.assertEqual(r.band, probe.BAND_UNAVAILABLE)
        self.assertFalse(r.is_healthy)


class CadenceAndTrackerTests(SimpleTestCase):
    def test_cadence(self):
        self.assertEqual(probe.next_probe_delay_seconds(probe.BAND_HEALTHY), 60)
        self.assertEqual(probe.next_probe_delay_seconds(probe.BAND_DEGRADED), 30)
        self.assertEqual(probe.next_probe_delay_seconds(probe.BAND_UNAVAILABLE, 0), 30)
        self.assertEqual(probe.next_probe_delay_seconds(probe.BAND_UNAVAILABLE, 2), 120)
        self.assertEqual(probe.next_probe_delay_seconds(probe.BAND_UNAVAILABLE, 50),
                         probe.CADENCE_UNAVAILABLE_CAP_S)

    def test_recovery_needs_consecutive_successes(self):
        t = probe.ReadinessTracker()
        s = t.observe(probe.BAND_UNAVAILABLE)
        self.assertTrue(s["alerting"])
        s = t.observe(probe.BAND_HEALTHY)         # ONE success — not enough
        self.assertTrue(s["alerting"])
        s = t.observe(probe.BAND_HEALTHY)         # second consecutive — clears
        self.assertFalse(s["alerting"])
        self.assertEqual(t.up_down_up, 1)         # flap counted

    def test_backoff_grows_with_consecutive_unavailable(self):
        t = probe.ReadinessTracker()
        d1 = t.observe(probe.BAND_UNAVAILABLE)["next_delay_s"]
        d2 = t.observe(probe.BAND_UNAVAILABLE)["next_delay_s"]
        self.assertGreater(d2, d1)


# ────────────────────────── WS-E — monitoring ──────────────────────────
class _Attempt:
    def __init__(self, reason_code="", status="INVALID", created_at=1000.0):
        self.reason_code = reason_code
        self.status = status
        self._ts = created_at

    @property
    def created_at(self):
        class _T:
            def __init__(self, ts): self._ts = ts
            def timestamp(self): return self._ts
        return _T(self._ts)


class WindowRatesTests(SimpleTestCase):
    def test_rates_and_window_filter(self):
        now = 10000.0
        attempts = [
            _Attempt("validation_ipc_unavailable", "INVALID", now - 10),
            _Attempt("server_unavailable", "INVALID", now - 20),
            _Attempt("", "VALID", now - 30),
            _Attempt("validation_ipc_unavailable", "INVALID", now - 999999),  # outside window → excluded
        ]
        r = mon.window_rates(attempts, now=now, window_seconds=3600)
        self.assertEqual(r["attempts_total"], 3)
        self.assertEqual(r["failures_total"], 2)
        self.assertAlmostEqual(r["ipc_failure_rate"], 1 / 3)
        self.assertAlmostEqual(r["broker_failure_rate"], 1 / 3)

    def test_empty_window_no_div_by_zero(self):
        r = mon.window_rates([], now=1.0)
        self.assertEqual(r["attempts_total"], 0)
        self.assertEqual(r["ipc_failure_rate"], 0)


class EvaluateAlertsTests(SimpleTestCase):
    def _r(self, state):
        return probe._make(state, supervised=(state == probe.HEALTHY or None), vla=True, reason="x",
                           cid="c", ms=1, now=1.0, layers={})

    def test_unsupervised_fires_high(self):
        alerts = mon.evaluate_alerts(self._r(probe.UNSUPERVISED), mon.window_rates([], now=1.0))
        names = [a.name for a in alerts]
        self.assertIn("agent_unsupervised_listener", names)
        self.assertEqual(alerts[0].severity, "HIGH")

    def test_healthy_no_alerts(self):
        alerts = mon.evaluate_alerts(self._r(probe.HEALTHY), mon.window_rates([], now=1.0))
        self.assertEqual(alerts, [])

    def test_stale_probe_pages(self):
        alerts = mon.evaluate_alerts(self._r(probe.HEALTHY), mon.window_rates([], now=1.0),
                                     readiness_stale=True)
        self.assertIn("readiness_probe_stale", [a.name for a in alerts])

    def test_rate_alert_requires_min_samples(self):
        now = 100.0
        few = [_Attempt("validation_ipc_unavailable", "INVALID", now)]        # 1 sample < min
        self.assertEqual(mon.evaluate_alerts(self._r(probe.HEALTHY),
                                             mon.window_rates(few, now=now)), [])
        many = [_Attempt("validation_ipc_unavailable", "INVALID", now) for _ in range(5)]
        alerts = mon.evaluate_alerts(self._r(probe.HEALTHY), mon.window_rates(many, now=now))
        self.assertIn("mt5_ipc_failure_rate_high", [a.name for a in alerts])

    def test_snapshot_shape(self):
        snap = mon.compute_snapshot(self._r(probe.HEALTHY), [], now=1.0)
        self.assertEqual(snap["band"], probe.BAND_HEALTHY)
        self.assertIn("rates", snap)
        self.assertIn("alerts", snap)

    def test_crash_loop_alert_fires_from_flap_count(self):
        # ADR-0013-addendum crash-loop-paging contract: up->down->up flaps actually produce agent_crash_loop.
        rates = mon.window_rates([], now=1.0)
        none = mon.evaluate_alerts(self._r(probe.HEALTHY), rates, crash_loop_restarts=1)
        self.assertNotIn("agent_crash_loop", [a.name for a in none])   # below threshold
        fired = mon.evaluate_alerts(self._r(probe.HEALTHY), rates, crash_loop_restarts=2)
        crash = [a for a in fired if a.name == "agent_crash_loop"]
        self.assertTrue(crash)
        self.assertEqual((crash[0].severity, crash[0].runbook), ("HIGH", "restart-procedure"))

    def test_tracker_up_down_up_feeds_crash_loop(self):
        # end-to-end: the tracker's up_down_up count is exactly what evaluate_alerts consumes.
        t = probe.ReadinessTracker()
        for band in (probe.BAND_UNAVAILABLE, probe.BAND_HEALTHY, probe.BAND_HEALTHY,
                     probe.BAND_UNAVAILABLE, probe.BAND_HEALTHY, probe.BAND_HEALTHY):
            t.observe(band)
        self.assertEqual(t.up_down_up, 2)
        fired = mon.evaluate_alerts(self._r(probe.HEALTHY), mon.window_rates([], now=1.0),
                                    crash_loop_restarts=t.up_down_up)
        self.assertIn("agent_crash_loop", [a.name for a in fired])


# ────────────────────────── WS-F — alert delivery ──────────────────────────
class AlertSinkTests(SimpleTestCase):
    def _alert(self, name="agent_down", sev="HIGH", detail="d"):
        return mon.Alert(name, sev, "UNAVAILABLE", "agent-unavailable", detail)

    def test_null_sink_reports_no_channel(self):
        r = sink_mod.NullAlertSink().deliver(self._alert(), now=1.0)
        self.assertFalse(r.delivered)
        self.assertEqual(r.reason, "no_channel_configured")

    def test_logging_sink_requires_named_owner(self):
        with self.assertRaises(ValueError):
            sink_mod.LoggingAlertSink(owner="UNASSIGNED")
        with self.assertRaises(ValueError):
            sink_mod.LoggingAlertSink(owner="")

    def test_logging_sink_delivers_and_debounces(self):
        s = sink_mod.LoggingAlertSink(owner="oncall-nuno", debounce_seconds=900)
        with self.assertLogs("guvfx.validation_agent.alerts", level="ERROR") as cm:
            r1 = s.deliver(self._alert(), now=1000.0)
        self.assertTrue(r1.delivered)
        self.assertTrue(any("agent_down" in m for m in cm.output))
        r2 = s.deliver(self._alert(), now=1001.0)     # within debounce
        self.assertTrue(r2.suppressed)
        self.assertFalse(r2.delivered)
        # after the window, delivers again
        with self.assertLogs("guvfx.validation_agent.alerts", level="ERROR"):
            r3 = s.deliver(self._alert(), now=1000.0 + 901)
        self.assertTrue(r3.delivered)

    @override_settings(AGENT_ALERT_SINK="logging", AGENT_ALERT_OWNER="oncall-nuno")
    def test_factory_builds_logging_when_owner_set(self):
        s = sink_mod.build_alert_sink()
        self.assertIsInstance(s, sink_mod.LoggingAlertSink)

    @override_settings(AGENT_ALERT_SINK="logging", AGENT_ALERT_OWNER="")
    def test_factory_falls_back_to_null_without_owner(self):
        s = sink_mod.build_alert_sink()
        self.assertIsInstance(s, sink_mod.NullAlertSink)

    @override_settings(AGENT_ALERT_SINK="", AGENT_ALERT_OWNER="")
    def test_factory_defaults_to_null(self):
        self.assertIsInstance(sink_mod.build_alert_sink(), sink_mod.NullAlertSink)

    def test_deliver_alerts_never_raises(self):
        class _Boom(sink_mod.AlertSink):
            channel = "boom"
            def deliver(self, alert, *, now, correlation_id=""):
                raise RuntimeError("kaboom")
        out = sink_mod.deliver_alerts(_Boom(), [self._alert()], now=1.0)
        self.assertEqual(out[0]["delivered"], False)
        self.assertEqual(out[0]["reason"], "deliver_raised")

    def test_describe_contract_present(self):
        d = sink_mod.LoggingAlertSink(owner="oncall-nuno").describe()
        for k in ("channel", "owner", "test", "retry", "on_failure", "ack"):
            self.assertIn(k, d)


# ────────────────────────── WS-G — Operations status presenter ──────────────────────────
class StatusPresenterTests(SimpleTestCase):
    def _snapshot(self, state):
        r = probe._make(state, supervised=(False if state == probe.UNSUPERVISED else None), vla=True,
                        reason="unsupervised_listener", cid="corr-123", ms=5, now=1.0, layers={})
        return mon.compute_snapshot(r, [], now=1.0)

    def test_customer_view_hides_all_operational_detail(self):
        # An UNSUPERVISED agent (the most sensitive internal state) must never leak to the customer.
        snap = self._snapshot(probe.UNSUPERVISED)
        cust = presenter.present_agent_status(snap, audience=presenter.CUSTOMER)
        self.assertTrue(presenter.customer_payload_is_safe(cust))
        self.assertFalse(cust["available"])
        blob = json.dumps(cust).lower()
        for forbidden in ("unsupervised", "corr-123", "reason", "alert", "supervis", "listener", "8791"):
            self.assertNotIn(forbidden, blob)
        # never blames the customer
        self.assertIn("nothing you need to change", cust["message"])

    def test_customer_healthy_is_available(self):
        cust = presenter.present_agent_status(self._snapshot(probe.HEALTHY), audience=presenter.CUSTOMER)
        self.assertTrue(cust["available"])

    def test_operator_view_has_full_detail(self):
        op = presenter.present_agent_status(self._snapshot(probe.UNSUPERVISED),
                                            audience=presenter.OPERATOR)
        self.assertEqual(op["state"], probe.UNSUPERVISED)
        self.assertIn("agent_unsupervised_listener", [a["name"] for a in op["alerts"]])
        self.assertEqual(op["correlation_id"], "corr-123")

    def test_unknown_audience_fails_safe_to_customer(self):
        out = presenter.present_agent_status(self._snapshot(probe.UNSUPERVISED), audience="anything-else")
        self.assertTrue(presenter.customer_payload_is_safe(out))
        self.assertIn("available", out)


# ────────────────────────── WS-A/J/L — artefacts ──────────────────────────
class ArtefactTests(SimpleTestCase):
    _DOCS = os.path.join(_REPO, "docs", "operations", "validation-agent")

    def test_supervised_winsw_xml_is_valid_and_has_restart_floor(self):
        import xml.etree.ElementTree as ET
        path = os.path.join(_BUNDLE, "winsw", "GuvFXBetaAgent.supervised.xml")
        root = ET.parse(path).getroot()
        self.assertEqual(root.tag, "service")
        self.assertEqual(root.findtext("startmode"), "Automatic")
        self.assertEqual(root.findtext("delayedAutoStart"), "true")
        onfailure = root.findall("onfailure")
        # bounded-backoff FLOOR: >=3 tiers, ALL restart (never 'none' — RR-1 adversarial correction)
        self.assertGreaterEqual(len(onfailure), 3)
        self.assertTrue(all(o.get("action") == "restart" for o in onfailure))
        # launch markers injected so classify_launch() reports supervised
        envs = {e.get("name"): e.get("value") for e in root.findall("env")}
        self.assertIn("BETA_AGENT_SERVICE_IDENTITY", envs)
        self.assertIn("BETA_AGENT_SUPERVISED_TOKEN", envs)
        # hard-refuse ships OFF (must not brick manual recovery before the service is proven)
        self.assertEqual(envs.get("BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH"), "0")

    def test_dark_install_only_xml_is_preserved(self):
        # the historical DARK profile must NOT have been rewritten (RULE 5 / Historical-preserve)
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(_BUNDLE, "winsw", "GuvFXBetaAgent.xml")).getroot()
        self.assertEqual(root.findtext("startmode"), "Manual")
        self.assertEqual(root.find("onfailure").get("action"), "none")

    def test_repo_audit_classes_are_known(self):
        audit = json.load(open(os.path.join(self._DOCS, "repo-audit-min-hardening.json"), encoding="utf-8"))
        allowed = set(audit["classes"])
        self.assertTrue(audit["findings"])
        for f in audit["findings"]:
            self.assertIn(f["class"], allowed, f"unknown audit class {f['class']} for {f['site']}")

    def test_deployment_package_states_not_applied_and_blast_radius(self):
        txt = open(os.path.join(self._DOCS, "deployment-min-hardening.md"), encoding="utf-8").read()
        self.assertIn("PREPARED, NOT APPLIED", txt)
        for guard in (":8788", "#12", "#1", "Rollback"):
            self.assertIn(guard, txt)

    def test_runbook_index_has_unsupervised_listener(self):
        idx = json.load(open(os.path.join(self._DOCS, "runbook-index.json"), encoding="utf-8"))
        ids = {r["id"] for r in idx["runbooks"]}
        self.assertIn("unsupervised-listener", ids)
        self.assertGreaterEqual(len(idx["runbooks"]), 12)


# ──────────── WORKSTREAM C (2026-08-06) — readiness-probe /provision route contract + drift guard ────────────
# Regression cover for the runtime-certification GATE-10 defect: the readiness probe's default transport
# posted to the BARE base_url, so the agent (which serves ONLY ``POST /provision``) answered ``unknown_route``
# (404) and the probe could never reach HEALTHY. The provisioning transport was correct all along. These tests
# pin the corrected route on the probe transport and guard the two transports against ever drifting again.
def _capture_transport_url(transport_fn, base_url, *, exc=None, handshake=None):
    """Invoke a REAL transport (probe or provisioning) with ``requests.post`` mocked; return a dict with the
    ``url`` it POSTed to, the ``payload`` sent, and any exception ``raised`` (the transport's own timeout
    classification). ``exc`` makes the mocked post raise; ``handshake`` overrides the returned NEGOTIATE dict.
    """
    captured = {"url": None, "payload": None, "timeout": None, "raised": None, "result": None}

    class _Resp:
        def json(self):
            return handshake if handshake is not None else _handshake()

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        if exc is not None:
            raise exc
        return _Resp()

    with mock.patch("requests.post", fake_post):
        try:
            captured["result"] = transport_fn(base_url, {"operation": "NEGOTIATE"})
        except Exception as e:                       # noqa: BLE001 — classification is asserted by callers
            captured["raised"] = e
    return captured


class ProbeRouteContractTests(SimpleTestCase):
    """The readiness-probe default transport MUST target the agent's single ``/provision`` route."""

    def test_bare_base_url_targets_provision(self):
        cap = _capture_transport_url(probe._default_transport, "http://host:8791")
        self.assertEqual(cap["url"], "http://host:8791/provision")

    def test_trailing_slash_appends_provision_once(self):
        cap = _capture_transport_url(probe._default_transport, "http://host:8791/")
        self.assertEqual(cap["url"], "http://host:8791/provision")
        self.assertEqual(cap["url"].count("/provision"), 1)

    def test_provision_appended_exactly_once_for_any_trailing_slashes(self):
        for base in ("http://host:8791", "http://host:8791/", "http://host:8791///"):
            url = _capture_transport_url(probe._default_transport, base)["url"]
            self.assertTrue(url.endswith("/provision"), base)
            self.assertEqual(url.count("/provision"), 1, base)

    def test_connect_error_classifies_unreachable_at_provision_route(self):
        import requests
        cap = _capture_transport_url(probe._default_transport, "http://host:8791",
                                     exc=requests.exceptions.ConnectionError())
        self.assertIsInstance(cap["raised"], probe._ProbeUnreachable)      # PR#290 classification unchanged
        self.assertEqual(cap["url"], "http://host:8791/provision")

    def test_read_timeout_classifies_no_negotiate_at_provision_route(self):
        import requests
        cap = _capture_transport_url(probe._default_transport, "http://host:8791",
                                     exc=requests.exceptions.ReadTimeout())
        self.assertIsInstance(cap["raised"], probe._ProbeReadTimeout)      # PR#290 classification unchanged
        self.assertEqual(cap["url"], "http://host:8791/provision")

    @override_settings(BETA_AGENT_BASE_URL="http://10.0.0.2:8791", BETA_AGENT_KEYRING=json.dumps(KEYRING), BETA_AGENT_KEY_ID="k1")  # noqa: E501
    def test_probe_completes_signed_negotiate_via_real_route_and_is_healthy(self):
        captured = {}

        class _Resp:
            def json(self):
                return _handshake(supervised=True)

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["payload"] = json
            return _Resp()

        with mock.patch("requests.post", fake_post):
            r = probe.probe_agent_readiness(now_fn=lambda: 1000.0, clock=iter([0.0, 0.05]).__next__)
        self.assertEqual(r.state, probe.HEALTHY)
        self.assertEqual(captured["url"], "http://10.0.0.2:8791/provision")
        # The probe issues ONLY a signed NEGOTIATE handshake — never a credentialed / broker-login op, so it
        # cannot perform broker validation, launch MT5, or create a validation attempt.
        self.assertEqual(captured["payload"]["operation"], "NEGOTIATE")
        self.assertNotIn(captured["payload"]["operation"], ("VALIDATE_LOGIN",))
        self.assertIn("signature", captured["payload"])


class TransportRouteDriftGuardTests(SimpleTestCase):
    """Guard: the readiness-probe transport and the provisioning transport can never build a different agent
    URL again — both derive from ``provision_url``."""

    def test_probe_and_provisioning_transports_build_identical_url(self):
        from . import beta_worker
        prov_transport = beta_worker.make_http_transport()
        for base in ("http://10.0.0.2:8791", "http://10.0.0.2:8791/", "http://h:1///"):
            probe_url = _capture_transport_url(probe._default_transport, base)["url"]
            prov_url = _capture_transport_url(prov_transport, base)["url"]
            self.assertEqual(probe_url, prov_url, base)
            self.assertEqual(probe_url, provision_url(base), base)

    def test_provision_url_helper_is_idempotent_on_trailing_slash(self):
        self.assertEqual(provision_url("http://h:1"), "http://h:1/provision")
        self.assertEqual(provision_url("http://h:1/"), "http://h:1/provision")
        self.assertEqual(provision_url("http://h:1///"), "http://h:1/provision")


class ProbeCreatesNoValidationAttemptTests(TestCase):
    """Explicit DB-backed proof (WORKSTREAM C items 6-8): a full readiness probe creates NO
    ``BrokerAccountValidationAttempt`` — it never touches the broker-validation path, launches no MT5, and
    contacts no broker."""

    @override_settings(BETA_AGENT_BASE_URL="http://10.0.0.2:8791", BETA_AGENT_KEYRING=json.dumps(KEYRING), BETA_AGENT_KEY_ID="k1")  # noqa: E501
    def test_probe_creates_no_broker_validation_attempt(self):
        from trading.models import BrokerAccountValidationAttempt
        before = BrokerAccountValidationAttempt.objects.count()

        class _Resp:
            def json(self):
                return _handshake(supervised=True)

        with mock.patch("requests.post", lambda url, json=None, timeout=None: _Resp()):
            r = probe.probe_agent_readiness(now_fn=lambda: 1000.0, clock=iter([0.0, 0.05]).__next__)
        self.assertEqual(r.state, probe.HEALTHY)
        self.assertEqual(BrokerAccountValidationAttempt.objects.count(), before)
