"""
Management command to refresh auto HTF (D1) zones for a TBP strategy.

Usage:
    # Dry run (prints JSON, does not write)
    python manage.py refresh_htf_zones --strategy-id 12

    # Write to strategy.filters.auto_zones + zones_meta
    python manage.py refresh_htf_zones --strategy-id 12 --write

    # Custom parameters
    python manage.py refresh_htf_zones --strategy-id 12 --symbols EURUSD,GBPUSD \\
        --days 120 --pivot-strength 2 --atr-period 14 --atr-mult 0.8 --max-zones 3 --write
"""

import json
import logging
import os
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from strategies.models import Strategy
from strategies.zone_generator import generate_zones_for_symbol

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD"]


def _fetch_d1_bars(symbol: str, count: int) -> list:
    """
    Fetch D1 OHLC bars from the Windows OHLC agent.

    Uses the same agent URL / token chain as signal_engine.fetch_rates().
    """
    agent_url = (
        os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL")
        or os.getenv("GUVFX_AGENT_URL")
        or os.getenv("WINDOWS_AGENT_BASE")
        or ""
    ).rstrip("/")
    # Token MUST match the base URL above. snapshots/rates lives on the 8788
    # bridge (GUVFX_WINDOWS_AGENT_BASE_URL) which authenticates with
    # GUVFX_WINDOWS_AGENT_TOKEN — not the legacy 8787 GUVFX_AGENT_TOKEN.
    agent_token = (
        os.getenv("GUVFX_WINDOWS_AGENT_TOKEN")
        or os.getenv("GUVFX_AGENT_TOKEN")
        or os.getenv("WINDOWS_AGENT_TOKEN")
        or ""
    ).strip()

    if not agent_url:
        raise CommandError(
            "GUVFX_WINDOWS_AGENT_BASE_URL not configured "
            "(set env var or GUVFX_AGENT_URL fallback)"
        )

    url = f"{agent_url}/mt5/snapshots/rates?symbol={symbol}&timeframe=D1&count={count}"
    headers = {"Content-Type": "application/json"}
    if agent_token:
        headers["X-GuvFX-Agent-Token"] = agent_token

    req = urllib.request.Request(url, method="GET", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
            if not data.get("ok"):
                raise CommandError(f"Agent error for {symbol}: {data.get('error', 'unknown')}")
            bars = data.get("data", [])
            if not isinstance(bars, list):
                raise CommandError(f"Invalid response for {symbol}: expected list")
            return bars
    except urllib.error.URLError as e:
        raise CommandError(f"Connection to agent failed for {symbol}: {e}")
    except urllib.error.HTTPError as e:
        raise CommandError(f"HTTP error for {symbol}: {e.code} {e.reason}")
    except json.JSONDecodeError as e:
        raise CommandError(f"Invalid JSON for {symbol}: {e}")


class Command(BaseCommand):
    help = "Refresh auto HTF (D1) supply/demand/pivot zones for a TBP strategy"

    def add_arguments(self, parser):
        parser.add_argument(
            "--strategy-id",
            type=int,
            required=True,
            help="Strategy ID to refresh zones for",
        )
        parser.add_argument(
            "--symbols",
            type=str,
            default=None,
            help="Comma-separated symbols (default: pairs_enabled from strategy filters, "
                 "fallback EURUSD,GBPUSD)",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=120,
            help="Number of D1 bars to fetch (default: 120)",
        )
        parser.add_argument(
            "--pivot-strength",
            type=int,
            default=2,
            help="Pivot detection strength (bars each side, default: 2)",
        )
        parser.add_argument(
            "--atr-period",
            type=int,
            default=14,
            help="ATR lookback period (default: 14)",
        )
        parser.add_argument(
            "--atr-mult",
            type=float,
            default=0.8,
            help="Zone width multiplier of ATR (default: 0.8)",
        )
        parser.add_argument(
            "--max-zones",
            type=int,
            default=3,
            help="Maximum zones per symbol (default: 3 → supply, pivot, demand)",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            default=False,
            help="Write zones to strategy.filters.auto_zones (default: dry-run only)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Explicit dry-run flag (same as omitting --write)",
        )

    def handle(self, *args, **options):
        strategy_id = options["strategy_id"]
        days = options["days"]
        pivot_strength = options["pivot_strength"]
        atr_period = options["atr_period"]
        atr_mult = options["atr_mult"]
        max_zones = options["max_zones"]
        write = options["write"] and not options["dry_run"]

        # Load strategy
        try:
            strategy = Strategy.objects.get(id=strategy_id)
        except Strategy.DoesNotExist:
            raise CommandError(f"Strategy {strategy_id} not found")

        filters = strategy.filters or {}

        # Resolve symbols
        symbols_arg = options.get("symbols")
        if symbols_arg:
            symbols = [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
        else:
            symbols = filters.get("pairs_enabled") or DEFAULT_SYMBOLS

        self.stdout.write(
            f"[AUTO_ZONES] strategy={strategy_id} symbols={symbols} "
            f"days={days} pivot_strength={pivot_strength} "
            f"atr_period={atr_period} atr_mult={atr_mult} max_zones={max_zones} "
            f"write={write}"
        )

        all_auto_zones = {}
        combined_meta = {}

        for symbol in symbols:
            self.stdout.write(f"  Fetching D1 bars for {symbol} (count={days})...")
            try:
                bars = _fetch_d1_bars(symbol, count=days)
            except CommandError as e:
                self.stderr.write(f"  [ERROR] {e}")
                continue

            self.stdout.write(f"  Got {len(bars)} bars for {symbol}")

            zones, meta = generate_zones_for_symbol(
                bars=bars,
                symbol=symbol,
                atr_period=atr_period,
                atr_mult=atr_mult,
                pivot_strength=pivot_strength,
                max_zones=max_zones,
            )

            if not zones:
                self.stderr.write(
                    f"  [WARN] No zones generated for {symbol}: {meta}"
                )
                continue

            all_auto_zones[symbol] = zones

            # Build per-symbol log line
            supply = [z for z in zones if z["zone_type"] == "supply"]
            demand = [z for z in zones if z["zone_type"] == "demand"]
            pivot = [z for z in zones if z["zone_type"] == "pivot"]

            supply_str = ", ".join(f'[{z["low"]:.5f}–{z["high"]:.5f}]' for z in supply) or "none"
            demand_str = ", ".join(f'[{z["low"]:.5f}–{z["high"]:.5f}]' for z in demand) or "none"
            pivot_str = ", ".join(f'[{z["low"]:.5f}–{z["high"]:.5f}]' for z in pivot) or "none"

            log_line = (
                f"[AUTO_ZONES] strategy={strategy_id} symbol={symbol} "
                f"generated supply={supply_str} demand={demand_str} pivot={pivot_str} "
                f"generated_at={meta.get('generated_at', '?')}"
            )
            self.stdout.write(log_line)
            print(log_line)  # Also to stdout for cron capture

            # Merge per-symbol meta into combined
            combined_meta = meta  # Last symbol meta wins for shared fields

        if not all_auto_zones:
            self.stderr.write("[AUTO_ZONES] No zones generated for any symbol")
            return

        if not write:
            # Dry-run: just print the JSON
            output = {
                "auto_zones": all_auto_zones,
                "zones_meta": combined_meta,
            }
            self.stdout.write("\n--- DRY RUN: computed zones (not written) ---")
            self.stdout.write(json.dumps(output, indent=2))
            self.stdout.write("--- end ---\n")
            return

        # Write to strategy.filters (merge, do NOT wipe existing keys)
        filters["auto_zones"] = all_auto_zones
        filters["zones_meta"] = combined_meta
        strategy.filters = filters
        strategy.save(update_fields=["filters"])

        self.stdout.write(
            f"[AUTO_ZONES] WRITTEN strategy={strategy_id} "
            f"symbols={list(all_auto_zones.keys())} "
            f"zones_meta.generated_at={combined_meta.get('generated_at')}"
        )

        # Verify manual zones are untouched
        manual_zones = filters.get("zones")
        if manual_zones:
            self.stdout.write(
                f"[AUTO_ZONES] Manual zones (filters.zones) preserved: "
                f"{list(manual_zones.keys())}"
            )
