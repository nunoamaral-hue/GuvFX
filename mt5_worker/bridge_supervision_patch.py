"""
RX-2B — canonical, idempotent patch that adds the read-only supervision
endpoint `GET /mt5/supervision` to the Windows signal bridge
(C:\\GuvFX\\mt5_signal_bridge.py).

This is the single source-controlled deployment material for the endpoint
(the bridge file itself lives only on the Windows host). Applying it to a
fresh, unpatched bridge produces the correct, working endpoint in one step.

Guarantees:
  * idempotent — re-running on an already-patched file is a no-op
  * `/health` is left unchanged (the existing watchdog depends on it)
  * the supervision handler is defined BEFORE the blocking
    `if __name__ == "__main__":` server start, and imports MetaTrader5
    locally (the bridge imports it per-handler, not at module level)
  * no change to the order/rates path

Usage (on the Windows host):
    "C:\\Program Files\\Python311\\python.exe" C:\\GuvFX\\bridge_supervision_patch.py
Then restart the bridge (or let GuvFX_BridgeWatchdog restart it) and verify:
    GET http://localhost:8788/mt5/supervision   (X-GuvFX-Agent-Token header)
    GET http://localhost:8788/health             (must still return ok:true)
See RX2_BRIDGE_SUPERVISION.md for full redeploy instructions.
"""
import io
import sys
import time

PATH = r"C:\GuvFX\mt5_signal_bridge.py"
ROUTE_ANCHOR = '            elif path == "/health":'
MAIN_ANCHOR = 'if __name__ == "__main__":'

ROUTE_BRANCH = (
    '            elif path == "/mt5/supervision":\n'
    '                self._send_json_response(_rx2_supervision_snapshot())\n'
)

FUNC = '''def _rx2_supervision_snapshot():
    """RX-2B reliability supervision: read-only MT5 terminal/broker/tick state.
    Returns ok=True if the endpoint ran; the DATA fields reflect health."""
    import time as _t
    import MetaTrader5 as mt5
    out = {"ok": True, "server_up": True, "mt5_initialized": False,
           "broker_connected": False, "trade_allowed": False,
           "account_login": None, "equity": None, "last_tick_age_s": None,
           "server_time": int(_t.time())}
    try:
        init_kwargs = {}
        if MT5_TERMINAL_PATH:
            init_kwargs["path"] = MT5_TERMINAL_PATH
        if not mt5.initialize(**init_kwargs):
            out["error"] = "mt5_init_failed:%s" % str(mt5.last_error())
            return out
        out["mt5_initialized"] = True
        ti = mt5.terminal_info()
        if ti is not None:
            out["broker_connected"] = bool(getattr(ti, "connected", False))
            out["trade_allowed"] = bool(getattr(ti, "trade_allowed", False))
        ai = mt5.account_info()
        if ai is not None:
            out["account_login"] = getattr(ai, "login", None)
            out["equity"] = getattr(ai, "equity", None)
        try:
            tick = mt5.symbol_info_tick("EURUSD")
            if tick is not None and getattr(tick, "time", 0):
                out["last_tick_age_s"] = max(0, int(_t.time()) - int(tick.time))
        except Exception:
            pass
        return out
    except Exception as e:
        out["ok"] = False
        out["error"] = "%s:%s" % (type(e).__name__, e)
        return out


'''


def main():
    src = io.open(PATH, "r", encoding="utf-8").read()
    if "/mt5/supervision" in src:
        print("ALREADY_PATCHED")
        return 0
    if src.count(ROUTE_ANCHOR) != 1:
        print("ROUTE_ANCHOR_UNEXPECTED:%d" % src.count(ROUTE_ANCHOR))
        return 2
    if MAIN_ANCHOR not in src:
        print("MAIN_ANCHOR_NOT_FOUND")
        return 2

    io.open(PATH + ".bak.rx2_%d" % int(time.time()), "w", encoding="utf-8").write(src)

    # 1) add the route branch just before the /health branch
    src = src.replace(ROUTE_ANCHOR, ROUTE_BRANCH + ROUTE_ANCHOR, 1)
    # 2) define the handler BEFORE the blocking main (module level)
    src = src.replace(MAIN_ANCHOR, FUNC + MAIN_ANCHOR, 1)

    io.open(PATH, "w", encoding="utf-8").write(src)
    print("PATCHED_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
