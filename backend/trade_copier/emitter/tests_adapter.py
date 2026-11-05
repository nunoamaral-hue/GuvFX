"""ADR-0077 — read-only MT5 adapter: no order surface, missing SL/TP preserved, open_time from `time`,
reason codes, no network (injected opener). Synthetic only."""
import json

from django.test import SimpleTestCase

from trade_copier.emitter.mt5_adapter import (BridgeHttpAdapter, FakeSnapshotAdapter, IdentityResult,
                                              PositionSnapshotAdapter, SnapshotResult)

_ORDER_WORDS = ("order", "close", "modify", "cancel", "send", "place", "write", "execute", "delete")


def _opener_for(payload_by_path):
    def opener(req, timeout):
        path = req.full_url.split("://", 1)[-1].split("/", 1)[-1]
        return json.dumps(payload_by_path.get("/" + path, {}))
    return opener


class AdapterSurfaceTests(SimpleTestCase):
    def test_no_order_or_mutation_method_exists(self):
        for cls in (PositionSnapshotAdapter, BridgeHttpAdapter, FakeSnapshotAdapter):
            public = [m for m in dir(cls) if not m.startswith("_")]
            for m in public:
                self.assertFalse(any(w in m.lower() for w in _ORDER_WORDS),
                                 f"adapter {cls.__name__} exposes a mutation-looking method: {m}")
            self.assertEqual(sorted(m for m in public if m.startswith("read_")),
                             ["read_identity", "read_positions"])


class BridgeHttpAdapterTests(SimpleTestCase):
    def _adapter(self, positions=None, ok=True, extra=None):
        pos_payload = {"ok": ok, "account_login": "55442", "account_server": "Tradersway-Live", "trade_mode": 2,
                       "positions": positions if positions is not None else []}
        if extra:
            pos_payload.update(extra)
        return BridgeHttpAdapter(base_url="http://127.0.0.1:8806", token="t" * 40,
                                 opener=_opener_for({"/mt5/positions": pos_payload,
                                                     "/mt5/snapshots/account": {"ok": True, "account_login": "55442",
                                                                                "account_server": "Tradersway-Live",
                                                                                "trade_mode": 2}}))

    def test_reads_positions_with_open_time_and_missing_sltp(self):
        a = self._adapter(positions=[{"ticket": 10, "symbol": "XAUUSD", "type": 0, "volume": 0.1,
                                      "sl": 0.0, "tp": 0.0, "price_open": 2000.0, "time": 1762300000}])
        r = a.read_positions()
        self.assertTrue(r.ok)
        p = r.positions[0]
        self.assertEqual(p.ticket, 10)
        self.assertEqual(p.side, "BUY")
        self.assertIsNone(p.sl)                     # 0.0 == no level → None (never invented as 0)
        self.assertIsNone(p.tp)
        self.assertIsNotNone(p.open_time)           # open_time parsed from the `time` epoch field
        self.assertEqual(p.price_open, 2000.0)

    def test_identity_read_does_not_need_positions(self):
        r = self._adapter().read_identity()
        self.assertTrue(r.ok)
        self.assertEqual(r.account_server, "Tradersway-Live")

    def test_failure_reason_codes(self):
        self.assertEqual(self._adapter(ok=False).read_positions().reason, "positions_not_ok")
        bad = self._adapter(extra={"positions": "nope"}).read_positions()
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, "positions_not_a_list")
        dup = self._adapter(positions=[{"ticket": 1, "symbol": "X", "type": 0, "volume": 0.1},
                                       {"ticket": 1, "symbol": "X", "type": 0, "volume": 0.1}]).read_positions()
        self.assertEqual(dup.reason, "duplicate_ticket")

    def test_read_error_fails_closed(self):
        def boom(req, timeout):
            raise OSError("down")
        a = BridgeHttpAdapter(base_url="http://127.0.0.1:8806", token="t" * 40, opener=boom)
        self.assertEqual(a.read_positions().reason, "positions_read_error")
        self.assertEqual(a.read_identity().reason, "identity_read_error")
