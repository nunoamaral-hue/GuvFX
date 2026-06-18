"""
SCE Replay Harness — run the REAL SCE engine on offline OHLC, route any signal
into Flow A suppression. Shadow Mode: no live data, no agent, no execution.

What it does (and does NOT do):
  * Loads offline OHLC fixtures (no Windows agent, no 8788 token).
  * Injects them by monkeypatching ``strategies.signal_engine.fetch_rates``.
  * Stubs the DB-touching risk/audit calls (no DB, no state mutation).
  * Calls the UNMODIFIED ``evaluate_sce`` (existing SCE logic only).
  * Maps any BUY/SELL SignalResult → Flow A candidate → suppression (logged).
  * Installs a socket guard that PROVES no network egress occurs.
  * Never calls the signal_engine dispatcher → never creates an ExecutionJob.

The SCE engine stack currently lives only in the `strange-ptolemy` worktree, so
its backend is placed first on sys.path (engine import) while this branch's
backend supplies `flow_a`. Override the engine path with SCE_BACKEND_PATH.

Run:
    SCE_BACKEND_PATH=/path/to/strange-ptolemy/backend \
      python backend/flow_a/replay/run_sce_replay.py
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import types
from datetime import datetime, timezone as dttz

THIS_BACKEND = pathlib.Path(__file__).resolve().parents[2]   # .../backend (this branch)
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
DEFAULT_ENGINE_BACKEND = "/Users/nunoamaral/.claude-worktrees/guvfx/strange-ptolemy/backend"
ENGINE_BACKEND = pathlib.Path(os.environ.get("SCE_BACKEND_PATH", DEFAULT_ENGINE_BACKEND))

SYMBOL = "EURUSD"
EXEC_TF = "H1"
PINNED_NOW = datetime(2026, 6, 6, 22, 0, tzinfo=dttz.utc)


def _banner(n, t):
    print("=" * 66)
    print(f"{n}. {t}")


def _load_fixtures():
    bars = {}
    for tf in ("H1", "H4"):
        p = FIXTURES / f"{SYMBOL}_{tf}.json"
        bars[tf] = json.loads(p.read_text())
    return bars


def main():
    # --- Shadow safety: ensure no live agent config is present -------------
    for v in ("GUVFX_WINDOWS_AGENT_BASE_URL", "GUVFX_AGENT_URL", "WINDOWS_AGENT_BASE",
              "GUVFX_WINDOWS_AGENT_TOKEN", "GUVFX_AGENT_TOKEN", "WINDOWS_AGENT_TOKEN"):
        os.environ.pop(v, None)
    # Dummy settings env so the worktree settings import (never connects to DB).
    os.environ.setdefault("DJANGO_SECRET_KEY", "replay-harness-not-a-secret")
    os.environ.setdefault("DB_NAME", "replay")
    os.environ.setdefault("DB_USER", "replay")
    os.environ.setdefault("DB_PASSWORD", "replay")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guvfx_backend.settings")

    # sys.path: engine backend FIRST (full strategies+engines), this backend for flow_a.
    sys.path.insert(0, str(ENGINE_BACKEND))
    sys.path.insert(1, str(THIS_BACKEND))

    import django
    django.setup()

    # --- Load fixtures ----------------------------------------------------
    _banner(1, "REPLAY BARS LOADED (offline fixtures — no agent)")
    bars = _load_fixtures()
    for tf, b in bars.items():
        print(f"  {SYMBOL} {tf}: {len(b)} bars  first={b[0]['time']}  last={b[-1]['time']}")

    # --- Injection seam + DB stubs ---------------------------------------
    import strategies.signal_engine as se
    from strategies.engines import sce_engine

    fetch_calls = {"n": 0, "args": []}

    def fixture_fetch(account, symbol, timeframe, count=300):
        fetch_calls["n"] += 1
        fetch_calls["args"].append((symbol, timeframe, count))
        data = bars.get(timeframe, [])
        return data[-count:] if count else data

    se.fetch_rates = fixture_fetch  # lazy-imported inside evaluate_sce

    # Stub DB-touching calls in the engine's namespace (no DB, no mutation).
    sce_engine.check_risk_gates = lambda **kw: (True, "REPLAY_OK")
    sce_engine.record_signal_event = lambda **kw: None
    sce_engine.increment_daily_trade_count = lambda *a, **k: None

    # --- Duck-typed context (no ORM) -------------------------------------
    strategy = types.SimpleNamespace(filters={}, risk_per_trade_pct=1.0,
                                     name="SCE Replay")
    account = types.SimpleNamespace(balance=10000.0, is_demo=True,
                                    windows_username="replay")
    assignment = types.SimpleNamespace(strategy=strategy, account=account,
                                       risk_per_trade_override_pct=None)

    # --- Network guard: PROVE no egress ----------------------------------
    net = {"attempts": 0}
    _orig_connect = socket.socket.connect

    def _blocked_connect(self, addr):
        net["attempts"] += 1
        raise RuntimeError(f"network blocked in replay (attempted {addr})")

    socket.socket.connect = _blocked_connect

    # --- Run the REAL SCE engine -----------------------------------------
    _banner(2, "SCE EVALUATED FROM REPLAY DATA (existing engine, unmodified)")
    try:
        result = sce_engine.evaluate_sce(assignment, SYMBOL, PINNED_NOW,
                                         bar_close_time=bars["H1"][-1]["time"])
    finally:
        socket.socket.connect = _orig_connect

    print(f"  fetch_rates calls (fixtures only): {fetch_calls['n']}  {fetch_calls['args']}")

    _banner(3, "SIGNAL RESULT / NO-SIGNAL REASON")
    print(f"  ok={result.ok}  signal_type={result.signal_type}  reason={result.reason!r}")
    if result.signal_type in ("BUY", "SELL"):
        print(f"  entry={result.entry_price} sl={result.sl_price} tp={result.tp_price} lots={result.lots}")
    else:
        # surface the deepest reject reason from diagnostics if present
        d = result.details or {}
        for k in ("bias_diag", "bos_diag", "pullback_diag", "rejection_diag"):
            if isinstance(d.get(k), dict) and d[k].get("reject"):
                print(f"    {k}.reject = {d[k]['reject']}")

    # --- Route any signal into Flow A suppression ------------------------
    _banner(4, "FLOW A SUPPRESSION ROUTING")
    from flow_a.suppression import emit_shadow_candidate
    candidate = None
    suppression_record = None
    if result.signal_type in ("BUY", "SELL"):
        from flow_a.replay.adapter import signal_result_to_candidate
        candidate = signal_result_to_candidate(
            result, symbol=SYMBOL, timeframe=EXEC_TF, risk_per_trade_pct="1.0")
        suppression_record = emit_shadow_candidate(candidate, run_id="sce-replay-001")
        print(f"  candidate: {json.dumps(candidate.to_dict())}")
        print(f"  suppression: {json.dumps(suppression_record)}")
    else:
        print("  (no BUY/SELL signal — nothing to route; nothing suppressed)")

    # --- Suppression + safety verification -------------------------------
    _banner(5, "SUPPRESSION & SAFETY VERIFICATION")
    exec_app_loaded = django.apps.apps.is_installed("execution")
    job_created = bool(suppression_record and suppression_record.get("execution_job_created"))
    print(f"  execution app installed in runtime : {exec_app_loaded}")
    print(f"  ExecutionJob created               : {job_created}")
    print(f"  dispatcher invoked                 : False (evaluate_sce called directly)")
    print(f"  network connect attempts           : {net['attempts']}  (0 == no egress)")
    print(f"  agent env present                  : "
          f"{any(os.environ.get(v) for v in ('GUVFX_WINDOWS_AGENT_BASE_URL','GUVFX_AGENT_TOKEN'))}")

    ok = (not job_created) and net["attempts"] == 0 and fetch_calls["n"] >= 1
    print("=" * 66)
    print("PASS — replay ran on offline data; execution suppressed; no job; "
          "no network." if ok else "FAIL — invariant breach.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
