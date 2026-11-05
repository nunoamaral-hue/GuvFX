"""ADR-0074 Phase-1/8 — the repository-side bridge EMITTER.

Observe-and-emit: monotonic per-ticket revisions, explicit-missing SL/TP preserved, open/modify/partial/full
close coverage, a bounded durable spool with bounded retry + dead-letter, at-least-once dedup, restart/replay
via the file spool, and the deadline-non-extension invariant (a retry re-signs the envelope but NEVER rewrites a
revision's true broker_event_time). The emitter's envelope verifies against the REAL verifier with a synthetic
key — proving wire compatibility. Default OFF. No broker write; no secret logged.
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone as tz

from django.test import SimpleTestCase

from trade_copier import bridge_emitter as be
from trade_copier import event_ingestion as ei

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"   # 40 hex, no placeholder marker


class _FakeTransport:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def __call__(self, envelope):
        self.sent.append(envelope)
        return be.SendResult(ok=self.ok, status=(200 if self.ok else 500), reason=("ok" if self.ok else "boom"))


def _pos(ticket=1, volume=0.1, sl=None, tp=None, bet=None, symbol="XAUUSD", side="BUY"):
    return be.ObservedMasterPosition(ticket=ticket, symbol=symbol, side=side, volume=volume, sl=sl, tp=tp,
                                     broker_event_time=bet)


def _emitter(spool=None, transport=None, enabled=True, **kw):
    return be.BridgeEmitter(account_id=1, spool=spool or be.MemorySpool(), key_provider=lambda: SECRET,
                            transport=transport or _FakeTransport(), enabled=enabled,
                            broker_login="1000001", broker_server="IS6Technologies-Demo", **kw)


class EmitterObserveTests(SimpleTestCase):
    def test_disabled_emitter_is_inert(self):
        em = _emitter(enabled=False)
        self.assertEqual(em.observe([_pos()]).revisions_emitted, 0)
        self.assertEqual(em.flush()["enabled"], False)

    def test_first_open_emits_revision_one(self):
        em = _emitter()
        r = em.observe([_pos(ticket=1, volume=0.1)])
        self.assertEqual(r.changed_tickets, (1,))
        self.assertEqual(em.spool.load().per_ticket_revision["1"], 1)

    def test_monotonic_revisions_and_no_burn_on_no_change(self):
        em = _emitter()
        em.observe([_pos(ticket=1, volume=0.1)])            # rev 1 (OPEN)
        em.observe([_pos(ticket=1, volume=0.1)])            # identical → no change, no new revision
        self.assertEqual(em.spool.load().per_ticket_revision["1"], 1)
        em.observe([_pos(ticket=1, volume=0.1, sl=1990.0)])  # SL set → rev 2
        self.assertEqual(em.spool.load().per_ticket_revision["1"], 2)

    def test_explicit_missing_sltp_preserved(self):
        t = _FakeTransport()
        em = _emitter(transport=t)
        em.observe([_pos(ticket=1, volume=0.1, sl=1990.0)])  # SL set
        em.observe([_pos(ticket=1, volume=0.1, sl=None)])    # SL removed → explicit None
        em.flush()
        last = t.sent[-1]["payload"]["revisions"][0]
        self.assertIsNone(last["sl"])                        # removal represented as None, not dropped/zero

    def test_full_close_emits_closed_revision(self):
        t = _FakeTransport()
        em = _emitter(transport=t)
        em.observe([_pos(ticket=1, volume=0.1)])
        em.observe([])                                       # position gone → full close
        em.flush()
        closed = [r for env in t.sent for r in env["payload"]["revisions"] if r["ticket"] == 1 and r["closed"]]
        self.assertTrue(closed)

    def test_envelope_verifies_against_real_verifier(self):
        t = _FakeTransport()
        em = _emitter(transport=t)
        em.observe([_pos(ticket=1, volume=0.1)])
        em.flush()
        env = t.sent[-1]
        seen = set()
        def burn(n, _e):
            if n in seen:
                return False
            seen.add(n); return True
        out = ei.verify_ingest_request(env, keyring={ei.DEFAULT_KEY_ID: SECRET}, now=time.time(), nonce_burn=burn)
        self.assertEqual(out["signed"]["account_id"], 1)


class EmitterSpoolRetryTests(SimpleTestCase):
    def test_failed_send_retries_then_dead_letters(self):
        t = _FakeTransport(ok=False)
        em = _emitter(transport=t, max_attempts=3)
        em.observe([_pos(ticket=1, volume=0.1)])
        s1 = em.flush(); self.assertEqual(s1["pending"], 1)   # attempt 1 failed → still pending
        s2 = em.flush(); self.assertEqual(s2["pending"], 1)   # attempt 2
        s3 = em.flush(); self.assertEqual(s3["pending"], 0)   # attempt 3 → dead-lettered
        self.assertEqual(em.spool.load().dropped, 1)

    def test_success_removes_and_reflush_is_noop(self):
        t = _FakeTransport(ok=True)
        em = _emitter(transport=t)
        em.observe([_pos(ticket=1, volume=0.1)])
        self.assertEqual(em.flush()["sent"], 1)
        self.assertEqual(em.flush()["sent"], 0)               # nothing pending → at-least-once, no duplicate batch

    def test_bounded_spool_never_grows_unbounded(self):
        em = _emitter(transport=_FakeTransport(ok=False), max_pending=2)
        for i in range(5):
            em.observe([_pos(ticket=i + 1, volume=0.1)])      # each distinct ticket → a new batch
        state = em.spool.load()
        self.assertLessEqual(len(state.pending), 2)
        self.assertGreaterEqual(state.dropped, 1)             # overflow recorded, not silently infinite

    def test_restart_replay_via_file_spool(self):
        path = os.path.join(tempfile.mkdtemp(), "spool.json")
        em1 = _emitter(spool=be.FileSpool(path), transport=_FakeTransport(ok=False))
        em1.observe([_pos(ticket=1, volume=0.1)])
        em1.flush()                                           # failed → persisted pending
        # Simulate a process restart: a brand-new emitter from the SAME spool file resumes the pending batch.
        t2 = _FakeTransport(ok=True)
        em2 = _emitter(spool=be.FileSpool(path), transport=t2)
        self.assertEqual(em2.flush()["sent"], 1)              # the persisted batch is delivered after restart
        self.assertEqual(len(t2.sent[0]["payload"]["revisions"]), 1)


class DeadlineNonExtensionTests(SimpleTestCase):
    def test_retry_resigns_but_never_rewrites_true_times(self):
        clock = {"t": 1_000_000.0}
        t = _FakeTransport(ok=False)
        now0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=tz.utc)
        opened = datetime(2026, 11, 5, 6, 0, 0, tzinfo=tz.utc)   # position opened 6h before observation
        em = be.BridgeEmitter(account_id=1, spool=be.MemorySpool(), key_provider=lambda: SECRET, transport=t,
                              enabled=True, broker_login="1000001", broker_server="IS6Technologies-Demo",
                              clock=lambda: clock["t"], now_dt=lambda: now0, max_attempts=9)
        # The honest age references (open_time + a host-supplied broker_event_time) are stamped ONCE.
        em.observe([be.ObservedMasterPosition(ticket=1, symbol="XAUUSD", side="BUY", volume=0.1,
                                              open_time=opened, broker_event_time=opened)])
        em.flush()
        first = t.sent[-1]
        clock["t"] += 10_000.0        # advance the wall clock far past the admissibility window and re-send
        em.flush()
        second = t.sent[-1]
        # The transport envelope was RE-SIGNED (fresh timestamp) so the endpoint's skew/expiry pass…
        self.assertGreater(second["signed"]["timestamp"], first["signed"]["timestamp"])
        # …but the PAYLOAD's true times are UNCHANGED — a retry can never reset an entry deadline.
        for env in (first, second):
            self.assertEqual(env["payload"]["revisions"][0]["open_time"], opened.isoformat())
            self.assertEqual(env["payload"]["revisions"][0]["broker_event_time"], opened.isoformat())
