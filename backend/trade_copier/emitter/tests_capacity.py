"""ADR-0077 Phase-11 — synthetic emitter latency evidence. Per-stage + end-to-end (one account). EVIDENCE only:
production cadence is NOT claimed from synthetic numbers; a host measurement is required before any sub-second
interval (documented in the install/acceptance doc)."""
import json
import time

from django.test import SimpleTestCase

from trade_copier import bridge_emitter as be
from trade_copier.emitter.app import EmitterApp
from trade_copier.emitter.config import load_config
from trade_copier.emitter.mt5_adapter import FakeSnapshotAdapter

SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4"
N = 50
BASE = dict(account_id=46, broker_login="55442", broker_server="Tradersway-Live", host_id="h",
            bridge_base_url="http://127.0.0.1:8806", ingest_url="https://x.invalid/x/",
            observation_allowed="1", delivery_allowed="1")


def _pct(xs, p):
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


class _T:
    def __call__(self, env):
        return be.SendResult(ok=True, status=200)


class CapacityTests(SimpleTestCase):
    def test_emitter_latency_profile(self):
        ad = FakeSnapshotAdapter(positions=[be.ObservedMasterPosition(ticket=1, symbol="XAUUSD", side="BUY",
                                                                      volume=1.0)])
        app = EmitterApp(load_config(BASE), adapter=ad, transport=_T(),
                         env={"COPIER_INGEST_HMAC_KEY": SECRET}, spool=be.MemorySpool())
        snap, observe, e2e = [], [], []
        for i in range(N):
            # vary SL each cycle so detection + a signed revision happen every iteration
            ad.set_positions([be.ObservedMasterPosition(ticket=1, symbol="XAUUSD", side="BUY", volume=1.0,
                                                        sl=1990.0 + i)])
            t0 = time.perf_counter()
            ad.read_positions()
            t1 = time.perf_counter()
            app.observe_once()                 # read + classify + sign + spool + deliver (one full cycle)
            t2 = time.perf_counter()
            snap.append((t1 - t0) * 1000.0)
            e2e.append((t2 - t0) * 1000.0)
            observe.append((t2 - t1) * 1000.0)
        profile = {k: {"p50": round(_pct(v, 50), 3), "p95": round(_pct(v, 95), 3), "p99": round(_pct(v, 99), 3)}
                   for k, v in (("snapshot_ms", snap), ("classify_sign_spool_deliver_ms", observe),
                                ("end_to_end_ms", e2e))}
        print("\nADR-0077 emitter capacity (one account, n=%d): %s" % (N, json.dumps(profile)))
        self.assertTrue(all(x >= 0 for x in e2e))
        self.assertLess(_pct(e2e, 99), 1000.0)   # generous ceiling; NOT a production-cadence claim
