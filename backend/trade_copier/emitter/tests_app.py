"""ADR-0076 — emitter runner: modes, gates, hybrid change detection, restart/idempotency, bounded spool.
Synthetic FakeSnapshotAdapter + fake transport; no network; Django-free paths. Zero order surface."""
import os
import tempfile

from django.test import SimpleTestCase

from trade_copier import bridge_emitter as be
from trade_copier.emitter.app import EmitterApp
from trade_copier.emitter.config import ConfigError, load_config
from trade_copier.emitter.mt5_adapter import FakeSnapshotAdapter, IdentityResult

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
ENV = {"COPIER_INGEST_HMAC_KEY": SECRET, "GUVFX_AGENT_TOKEN": "t" * 40}
BASE = dict(account_id=46, broker_login="55442", broker_server="Tradersway-Live", host_id="host-A",
            bridge_base_url="http://127.0.0.1:8806", ingest_url="https://api.example.invalid/x/",
            build_sha="fb4ee402", terminal_fingerprint="tfp", bridge_id="127.0.0.1:8806")


def cfg(**over):
    return load_config({**BASE, "attestation_allowed": "1", "observation_allowed": "1", "delivery_allowed": "1", **over})


class _FakeTransport:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, envelope):
        self.sent.append(envelope)
        return be.SendResult(ok=self.ok, status=(200 if self.ok else 500), reason=("ok" if self.ok else "boom"))


def _pos(ticket=1, volume=0.1, sl=None, tp=None, open_time=None):
    return be.ObservedMasterPosition(ticket=ticket, symbol="XAUUSD", side="BUY", volume=volume, sl=sl, tp=tp,
                                     open_time=open_time)


def _app(adapter, *, transport=None, spool=None, config=None):
    return EmitterApp(config or cfg(), adapter=adapter, transport=transport or _FakeTransport(), env=ENV, spool=spool)


class GateTests(SimpleTestCase):
    def test_all_modes_inert_when_gates_off(self):
        a = _app(FakeSnapshotAdapter(positions=[_pos()]), config=load_config(BASE))  # gates default OFF
        self.assertEqual(a.attest()["reason"], "attestation_gate_off")
        self.assertEqual(a.observe_once()["reason"], "observation_gate_off")
        self.assertEqual(a.run_loop(max_cycles=3)["reason"], "observation_gate_off")

    def test_missing_identity_prevents_start(self):
        with self.assertRaises(ConfigError):
            EmitterApp(load_config({**BASE, "broker_server": ""}), adapter=FakeSnapshotAdapter(), env=ENV)

    def test_run_loop_inert_without_fast_interval(self):
        a = _app(FakeSnapshotAdapter(positions=[_pos()]))     # observation on, but fast_interval default 0
        self.assertEqual(a.run_loop(max_cycles=3)["reason"], "fast_loop_not_configured")


class AttestModeTests(SimpleTestCase):
    def test_attest_reads_no_positions(self):
        class _NoPos(FakeSnapshotAdapter):
            def read_positions(self_inner):
                raise AssertionError("attest must not read positions")
        a = _app(_NoPos(identity=IdentityResult(True, "ok", "55442", "Tradersway-Live", 2)))
        out = a.attest()
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "attest")
        self.assertIn("envelope", out)
        self.assertEqual(out["key_id"], "copier-ingest")      # key IDENTITY only
        self.assertNotIn("key_fingerprint", out)              # no secret-derived value surfaced
        self.assertNotIn(SECRET, str(out))                    # secret never surfaced

    def test_attest_fails_closed_on_observed_identity_mismatch(self):
        # The live terminal the emitter reached is a DIFFERENT account/server → attest must NOT emit a usable
        # envelope (consistent with observe_once; prevents a wrong-terminal attestation existing at all).
        a = _app(FakeSnapshotAdapter(identity=IdentityResult(True, "ok", "99999", "IS6Technologies-Live", 2)))
        out = a.attest()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "identity_mismatch")
        self.assertNotIn("envelope", out)

    def test_attest_fails_closed_without_key(self):
        a = EmitterApp(cfg(), adapter=FakeSnapshotAdapter(), transport=_FakeTransport(), env={})  # no key env
        self.assertEqual(a.attest()["reason"], "attestation_key_unavailable")


class DetectionTests(SimpleTestCase):
    def _run(self, adapter, transport):
        return _app(adapter, transport=transport)

    def test_open_without_sltp_then_add_change_remove_sl(self):
        t = _FakeTransport()
        ad = FakeSnapshotAdapter(positions=[_pos(sl=None, tp=None)])
        app = self._run(ad, t)
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # OPEN (no SL/TP accepted)
        ad.set_positions([_pos(sl=1990.0)])
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # SL added → one modification
        ad.set_positions([_pos(sl=1995.0)])
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # SL changed → one revision
        ad.set_positions([_pos(sl=None)])
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # SL removed → detected
        # per-ticket revision advanced monotonically across the four changes
        self.assertEqual(app._spool.load().per_ticket_revision["1"], 4)

    def test_tp_changes_detected(self):
        t = _FakeTransport()
        ad = FakeSnapshotAdapter(positions=[_pos()])
        app = self._run(ad, t)
        app.observe_once()
        ad.set_positions([_pos(tp=2100.0)])
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # TP added

    def test_partial_close_reduced_volume(self):
        t = _FakeTransport()
        ad = FakeSnapshotAdapter(positions=[_pos(volume=1.0)])
        app = self._run(ad, t)
        app.observe_once()
        ad.set_positions([_pos(volume=0.6)])
        app.observe_once()
        last = t.sent[-1]["payload"]["revisions"][-1]
        self.assertAlmostEqual(last["volume"], 0.6)

    def test_full_close_emits_once_then_nothing(self):
        t = _FakeTransport()
        ad = FakeSnapshotAdapter(positions=[_pos()])
        app = self._run(ad, t)
        app.observe_once()
        ad.set_positions([])
        self.assertEqual(app.observe_once()["revisions_emitted"], 1)        # full close
        self.assertEqual(app.observe_once()["revisions_emitted"], 0)        # already gone → nothing

    def test_unchanged_emits_nothing(self):
        t = _FakeTransport()
        ad = FakeSnapshotAdapter(positions=[_pos(sl=1990.0)])
        app = self._run(ad, t)
        app.observe_once()
        self.assertEqual(app.observe_once()["revisions_emitted"], 0)        # identical snapshot

    def test_restart_preserves_revision_state(self):
        path = os.path.join(tempfile.mkdtemp(), "spool.json")
        ad = FakeSnapshotAdapter(positions=[_pos()])
        app1 = _app(ad, spool=be.FileSpool(path))
        app1.observe_once()                                                 # rev 1
        ad.set_positions([_pos(sl=1990.0)])
        app2 = _app(FakeSnapshotAdapter(positions=[_pos(sl=1990.0)]), spool=be.FileSpool(path))  # restart
        app2.observe_once()
        self.assertEqual(app2._spool.load().per_ticket_revision["1"], 2)    # resumed from rev 1 → rev 2

    def test_network_failure_queues_then_retry_delivers_once(self):
        t = _FakeTransport(ok=False)
        ad = FakeSnapshotAdapter(positions=[_pos()])
        app = self._run(ad, t)
        out = app.observe_once()
        self.assertGreaterEqual(app._spool.load().pending.__len__(), 1)     # failed send → queued (bounded)
        t.ok = True
        app._emitter.flush()                                                # retry delivers
        self.assertEqual(app._spool.load().pending.__len__(), 0)
        again = app._emitter.flush()
        self.assertEqual(again["sent"], 0)                                  # no duplicate re-send

    def test_corrupt_spool_fails_closed_not_silent_reset(self):
        # A corrupt durable spool must NOT silently reset the revision ledger and report healthy: it quarantines
        # the file, raises, and the runner fails closed with spool_corruption surfaced.
        path = os.path.join(tempfile.mkdtemp(), "spool.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json ")
        app = _app(FakeSnapshotAdapter(positions=[_pos()]), spool=be.FileSpool(path))
        out = app.observe_once()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "spool_corruption")
        self.assertTrue(out["diagnostics"]["spool_corruption"])
        self.assertTrue(os.path.exists(path + ".corrupt-") or any(
            f.startswith("spool.json.corrupt-") for f in os.listdir(os.path.dirname(path))))

    def test_bounded_spool_reports_overflow(self):
        app = _app(FakeSnapshotAdapter(), transport=_FakeTransport(ok=False), config=cfg(max_pending=2))
        for i in range(5):
            app.adapter.set_positions([_pos(ticket=1000 + i)])
            app.observe_once()
        state = app._spool.load()
        self.assertLessEqual(len(state.pending), 2)
        self.assertGreaterEqual(state.dropped, 1)                           # overflow recorded, not silent
        self.assertIsNotNone(app.diagnostics()["dead_lettered"])
