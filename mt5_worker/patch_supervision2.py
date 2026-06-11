"""RX-2B fix: move _rx2_supervision_snapshot definition BEFORE the blocking
`if __name__ == '__main__'` server start (it was appended after and never defined)."""
import io, os, sys, time

PATH = r"C:\GuvFX\mt5_signal_bridge.py"
with io.open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

marker = "def _rx2_supervision_snapshot():"
main_anchor = 'if __name__ == "__main__":'

if marker not in src:
    print("FUNC_NOT_PRESENT")
    sys.exit(2)
if main_anchor not in src:
    print("MAIN_ANCHOR_NOT_FOUND")
    sys.exit(2)

backup = PATH + ".bak.rx2b_%d" % int(time.time())
with io.open(backup, "w", encoding="utf-8") as f:
    f.write(src)

# 1) Cut the previously-appended function block (from its def to EOF).
idx = src.index(marker)
# include any leading blank lines before the def
cut_start = src.rfind("\n\n", 0, idx)
if cut_start == -1:
    cut_start = idx
src_head = src[:cut_start].rstrip() + "\n"

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

# 2) Insert the function definition immediately before the main block.
new_src = src_head.replace(main_anchor, func.lstrip("\n") + "\n" + main_anchor, 1)
if new_src.count(marker) != 1:
    print("UNEXPECTED_DEF_COUNT:%d" % new_src.count(marker))
    sys.exit(2)

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(new_src)
print("PATCHED2_OK backup=%s" % os.path.basename(backup))
