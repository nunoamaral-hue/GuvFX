"""RX-2B: additively add GET /mt5/supervision to the signal bridge. Idempotent."""
import io, os, sys, time

PATH = r"C:\GuvFX\mt5_signal_bridge.py"

with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

if "/mt5/supervision" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

backup = PATH + ".bak.rx2_%d" % int(time.time())
with io.open(backup, "w", encoding="utf-8") as f:
    f.write(src)

route_anchor = '            elif path == "/health":'
route_new = (
    '            elif path == "/mt5/supervision":\n'
    '                self._send_json_response(_rx2_supervision_snapshot())\n'
    + route_anchor
)
if src.count(route_anchor) != 1:
    print("ANCHOR_COUNT_UNEXPECTED:%d" % src.count(route_anchor))
    sys.exit(2)
src = src.replace(route_anchor, route_new, 1)

func = '''

def _rx2_supervision_snapshot():
    """RX-2B reliability supervision: read-only MT5 terminal/broker/tick state.
    Always returns ok=True if the endpoint ran; the DATA reflects health."""
    import time as _t
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
src = src + func

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(src)
print("PATCHED_OK backup=%s" % os.path.basename(backup))
