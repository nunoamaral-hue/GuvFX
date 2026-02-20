"""
Data loading utilities for GuvFX backtest harness.

Supports:
  - load_bars_from_csv: MT5-exported CSV files
  - load_bars_from_agent: live fetch from Windows OHLC agent
  - align_multi_timeframe: ensure M5/H1/H4/D1 alignment

All functions return the standard bar format:
  [{"time": "2026-01-01T00:00:00Z", "open": 1.1234, "high": 1.1250,
    "low": 1.1220, "close": 1.1240, "tick_volume": 1234}, ...]
"""

from __future__ import annotations

import csv
import json
import logging
import os
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def load_bars_from_csv(
    filepath: str,
    date_col: str = "time",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    volume_col: str = "tick_volume",
) -> List[Dict[str, Any]]:
    """
    Load OHLC bars from a CSV file (MT5 export format).

    The CSV is expected to have headers matching the column names.
    Column names are configurable for different export formats.

    Parameters
    ----------
    filepath : path to CSV file
    date_col, open_col, etc. : column names in the CSV

    Returns
    -------
    List of bar dicts sorted oldest -> newest.
    """
    bars: List[Dict[str, Any]] = []

    with open(filepath, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                bar = {
                    "time": row[date_col].strip(),
                    "open": float(row[open_col]),
                    "high": float(row[high_col]),
                    "low": float(row[low_col]),
                    "close": float(row[close_col]),
                    "tick_volume": int(float(row.get(volume_col, "0") or "0")),
                }
                bars.append(bar)
            except (ValueError, KeyError) as e:
                logger.warning("[DATA] Skipping invalid CSV row: %s (%s)", row, e)
                continue

    # Sort by time ascending
    bars.sort(key=lambda b: b["time"])

    logger.info("[DATA] Loaded %d bars from %s", len(bars), filepath)
    return bars


def load_bars_from_agent(
    symbol: str,
    timeframe: str,
    count: int = 500,
    agent_url: Optional[str] = None,
    agent_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch OHLC bars from the Windows OHLC agent.

    Uses the same agent URL / token chain as signal_engine.fetch_rates().

    Parameters
    ----------
    symbol : e.g. "EURUSD"
    timeframe : e.g. "M5", "H1", "H4", "D1"
    count : number of bars (max 500)
    agent_url : override GUVFX_WINDOWS_AGENT_BASE_URL
    agent_token : override GUVFX_AGENT_TOKEN

    Returns
    -------
    List of bar dicts sorted oldest -> newest.
    """
    url_base = (
        agent_url
        or os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL")
        or os.getenv("GUVFX_AGENT_URL")
        or os.getenv("WINDOWS_AGENT_BASE")
        or ""
    ).rstrip("/")
    token = (
        agent_token
        or os.getenv("GUVFX_AGENT_TOKEN")
        or os.getenv("WINDOWS_AGENT_TOKEN")
        or ""
    ).strip()

    if not url_base:
        raise RuntimeError(
            "No agent URL configured. Set GUVFX_WINDOWS_AGENT_BASE_URL, "
            "GUVFX_AGENT_URL, or pass agent_url parameter."
        )

    url = f"{url_base}/mt5/snapshots/rates?symbol={symbol}&timeframe={timeframe}&count={count}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-GuvFX-Agent-Token"] = token

    req = urllib.request.Request(url, method="GET", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)

            if not data.get("ok"):
                raise RuntimeError(f"Agent error for {symbol}/{timeframe}: {data.get('error', 'unknown')}")

            bars = data.get("data", [])
            if not isinstance(bars, list):
                raise RuntimeError(f"Invalid response: expected list")

            logger.info("[DATA] Fetched %d %s bars for %s from agent", len(bars), timeframe, symbol)
            return bars

    except urllib.error.URLError as e:
        raise RuntimeError(f"Connection to agent failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response: {e}")


def align_multi_timeframe(
    bars_dict: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Ensure multi-timeframe bar alignment.

    Given a dict like {"M5": [...], "M15": [...], "H1": [...], "H4": [...]},
    trims all timeframes so they cover the same date range (based on the
    narrowest time window).

    Parameters
    ----------
    bars_dict : dict of timeframe -> list of bars

    Returns
    -------
    dict of timeframe -> trimmed list of bars
    """
    if not bars_dict:
        return {}

    # Find the narrowest time window across all timeframes
    global_start = None
    global_end = None

    for tf, bars in bars_dict.items():
        if not bars:
            continue
        tf_start = bars[0]["time"]
        tf_end = bars[-1]["time"]

        if global_start is None or tf_start > global_start:
            global_start = tf_start
        if global_end is None or tf_end < global_end:
            global_end = tf_end

    if global_start is None or global_end is None:
        return bars_dict

    # Trim each timeframe to the common window
    aligned = {}
    for tf, bars in bars_dict.items():
        trimmed = [
            b for b in bars
            if global_start <= b["time"] <= global_end
        ]
        aligned[tf] = trimmed
        logger.info(
            "[DATA] Aligned %s: %d -> %d bars (window: %s to %s)",
            tf, len(bars), len(trimmed), global_start, global_end,
        )

    return aligned
