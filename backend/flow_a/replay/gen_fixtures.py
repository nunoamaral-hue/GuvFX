"""
Deterministic OHLC fixture generator for the SCE replay harness.

Produces offline replay bars in the standard GuvFX bar format
(``research.data_loader``):

    {"time": "...Z", "open": .., "high": .., "low": .., "close": .., "tick_volume": ..}

No randomness, no network — purely synthetic, reproducible bars. This is
*fixture data only*; it is NOT market data and NOT strategy logic. It exists so
the real SCE engine can be exercised offline.

Design: a clean bullish trend on H4 (for BULL bias) and a bullish H1 series with
a late impulse → shallow pullback → bullish push, to give SCE's BOS/pullback/
rejection stages a realistic shot. Whatever SCE decides is the evidence.

Run:
    python backend/flow_a/replay/gen_fixtures.py
"""
from __future__ import annotations

import json
import math
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "fixtures"

# Fixed epoch base (no Date.now — determinism). 2026-06-01T00:00:00Z.
_BASE_EPOCH = 1_780_000_000


def _iso(epoch: int) -> str:
    # Minimal ISO-8601 Zulu formatter without datetime.now().
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _bar(epoch, o, h, l, c, vol=1000):
    return {
        "time": _iso(epoch),
        "open": round(o, 5),
        "high": round(h, 5),
        "low": round(l, 5),
        "close": round(c, 5),
        "tick_volume": vol,
    }


def gen_series(n, base, trend, wave_amp, wave_period, tf_seconds, spread=0.0006):
    bars = []
    prev_close = base
    for i in range(n):
        mid = base + trend * i + wave_amp * math.sin(i / wave_period)
        o = prev_close
        c = mid
        hi = max(o, c) + spread / 2
        lo = min(o, c) - spread / 2
        bars.append(_bar(_BASE_EPOCH + i * tf_seconds, o, hi, lo, c))
        prev_close = c
    return bars


def gen_zigzag(n, base, up, down, leg, tf_seconds, wick=0.0003):
    """Clean zig-zag uptrend: alternating up/down legs (up>down → net rise).

    Produces strict, well-separated fractal pivots (no plateau ties) with a
    Higher-High / Higher-Low structure for BULL bias.
    """
    slope_up = up / leg
    slope_down = down / leg
    bars = []
    c = base
    going_up = True
    step = 0
    for i in range(n):
        o = c
        c = c + (slope_up if going_up else -slope_down)
        # Deterministic per-bar wick jitter (< per-bar move) breaks plateau ties
        # so fractal pivots are strict, without overriding the zig-zag structure.
        jh = 0.00003 * ((i * 53) % 7)
        jl = 0.00003 * ((i * 29) % 7)
        hi = max(o, c) + wick + jh
        lo = min(o, c) - wick - jl
        bars.append(_bar(_BASE_EPOCH + i * tf_seconds, o, hi, lo, c))
        step += 1
        if step >= leg:
            step = 0
            going_up = not going_up
    return bars


def add_impulse_pullback_rejection(bars, tf_seconds):
    """Shape the tail into impulse → pullback → bullish rejection (H1)."""
    n = len(bars)
    last_t = _BASE_EPOCH + (n - 1) * tf_seconds
    c0 = bars[-1]["close"]
    tail = []
    # impulse up (new higher high)
    seq = [c0 + 0.0010, c0 + 0.0022, c0 + 0.0036, c0 + 0.0052]  # strong push (BOS)
    # pullback ~50% of the last leg
    seq += [c0 + 0.0044, c0 + 0.0034, c0 + 0.0030]              # retrace, hold
    # bullish rejection candle + entry
    seq += [c0 + 0.0046, c0 + 0.0058]                            # strong bull close
    prev = c0
    for k, c in enumerate(seq, start=1):
        o = prev
        hi = max(o, c) + 0.0004
        lo = min(o, c) - 0.0004
        tail.append(_bar(last_t + k * tf_seconds, o, hi, lo, c))
        prev = c
    return bars + tail


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    # H4: zig-zag uptrend (up-legs > down-legs) → clean HH/HL pivots + BULL bias.
    h4 = gen_zigzag(132, base=1.0500, up=0.0090, down=0.0045, leg=6,
                    tf_seconds=4 * 3600)
    # H1: zig-zag uptrend, then an impulse/pullback/rejection tail.
    h1 = gen_zigzag(210, base=1.0500, up=0.0060, down=0.0030, leg=6,
                    tf_seconds=3600)
    h1 = add_impulse_pullback_rejection(h1, tf_seconds=3600)

    (OUT / "EURUSD_H4.json").write_text(json.dumps(h4, indent=0))
    (OUT / "EURUSD_H1.json").write_text(json.dumps(h1, indent=0))
    print(f"wrote EURUSD_H4.json ({len(h4)} bars), EURUSD_H1.json ({len(h1)} bars) -> {OUT}")


if __name__ == "__main__":
    main()
