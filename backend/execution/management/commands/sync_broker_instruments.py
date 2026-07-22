"""
GFX-PKT-BROKER-SYMBOL-REGISTRY — populate an account's BrokerInstrument cache from the broker.

The authoritative source of an account's tradeable symbols is its MT5 terminal, exposed by the
bridge ``GET /mt5/symbols`` (``mt5.symbols_get()``). This command fetches that list and upserts
``execution.BrokerInstrument`` rows so the symbol registry can resolve provider symbols against
what the broker actually offers. Read-only w.r.t. trading: it places NO order, only syncs the
symbol cache.

Usage::

    python manage.py sync_broker_instruments --account 1                 # fetch from the bridge
    python manage.py sync_broker_instruments --account 1 --dry-run
    python manage.py sync_broker_instruments --account 1 --from-json syms.json   # offline seed

The network fetch lives here (NOT in execution.broker_symbols, which stays network-free); the
pure upsert is ``upsert_broker_instruments`` so it can be unit-tested without a live bridge.
"""
import json
import os
import urllib.error
import urllib.request

from django.core.management.base import BaseCommand, CommandError

from execution.broker_symbols import base_symbol, normalize_symbol
from execution.models import BrokerInstrument
from trading.models import TradingAccount

_TIMEOUT = int(os.getenv("BROKER_SYMBOLS_SYNC_TIMEOUT_SECONDS", "15") or 15)


def _tradeable(sym: dict) -> bool:
    """A symbol is trade-enabled when visible and its trade_mode is not 'disabled'/0."""
    if sym.get("visible") is False:
        return False
    mode = sym.get("trade_mode", sym.get("trade_calc_mode"))
    if mode in (0, "0", "SYMBOL_TRADE_MODE_DISABLED", "disabled"):
        return False
    return True


def upsert_broker_instruments(account, raw_symbols) -> dict:
    """Upsert BrokerInstrument rows for ``account`` from ``raw_symbols`` (list of dicts with at
    least ``name``). Returns counts. Pure DB — no network, no order. Symbols not in the new set
    are marked ``enabled=False`` (kept for audit), never deleted."""
    counts = {"seen": 0, "created": 0, "updated": 0, "disabled": 0}
    seen_names = set()
    for sym in raw_symbols or []:
        name = normalize_symbol(sym.get("name"))
        if not name:
            continue
        counts["seen"] += 1
        seen_names.add(name)
        meta = {k: sym.get(k) for k in ("digits", "trade_mode", "contract_size", "tick_size",
                                        "point", "path", "currency_profit") if k in sym}
        obj, created = BrokerInstrument.objects.update_or_create(
            account=account, broker_symbol=name,
            defaults={"base_symbol": base_symbol(name), "enabled": _tradeable(sym), "metadata": meta},
        )
        counts["created" if created else "updated"] += 1
    # Symbols no longer offered by the broker -> disabled (fail-closed), preserved for audit.
    stale = BrokerInstrument.objects.filter(account=account, enabled=True).exclude(
        broker_symbol__in=seen_names)
    counts["disabled"] = stale.update(enabled=False)
    return counts


def _fetch_symbols(account) -> list:
    """Fetch the broker symbol list from the bridge ``GET /mt5/symbols`` (the only network call).

    The bridge's endpoints authenticate via the ``X-GuvFX-Agent-Token`` header, validated against the
    bridge's **agent** token only — NOT the ``X-Worker-Token`` used elsewhere. Sending the wrong header 401s.

    WS1 (post-rotation hardening): ``MT5_WORKER_TOKEN`` was removed from this chain. It is the WORKER
    credential standing in for the AGENT credential — exactly the cross-credential substitution Permanent
    Rule 3 forbids, and it only ever worked while the two secrets happened to hold the same value. Since the
    bridge no longer accepts the worker token for inbound auth, that fallback is now guaranteed to 401.
    """
    base_url = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or "").rstrip("/")
    token = (
        os.getenv("GUVFX_WINDOWS_AGENT_TOKEN")
        or os.getenv("GUVFX_AGENT_TOKEN")
        or ""
    )
    if not base_url:
        raise CommandError("GUVFX_WINDOWS_AGENT_BASE_URL is not set")
    windows_username = None
    if getattr(account, "mt5_instance_id", None):
        windows_username = getattr(account.mt5_instance, "windows_username", None)
    req = urllib.request.Request(f"{base_url}/mt5/symbols", method="GET", headers={
        "X-GuvFX-Agent-Token": token, **({"X-Windows-Username": windows_username} if windows_username else {}),
    })
    # Fail-closed on any bridge error: raise a clean CommandError (not a raw traceback in the
    # nightly cron log) and leave the existing cache untouched — a failed sync never opens a symbol.
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads((resp.read() or b"{}").decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise CommandError(
            f"bridge GET /mt5/symbols failed: HTTP {exc.code} — check the bridge is up and "
            f"GUVFX_WINDOWS_AGENT_TOKEN matches (auth header X-GuvFX-Agent-Token). Cache left unchanged."
        ) from exc
    except urllib.error.URLError as exc:
        raise CommandError(
            f"bridge GET /mt5/symbols unreachable: {exc.reason}. Cache left unchanged."
        ) from exc
    return payload.get("symbols", payload if isinstance(payload, list) else [])


class Command(BaseCommand):
    help = "Sync an account's BrokerInstrument cache from the broker (bridge GET /mt5/symbols)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, required=True)
        parser.add_argument("--from-json", type=str, default=None,
                            help="Read the symbols list from a JSON file instead of the bridge.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        try:
            account = TradingAccount.objects.get(pk=opts["account"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"account {opts['account']} not found")
        if opts["from_json"]:
            with open(opts["from_json"]) as fh:
                data = json.load(fh)
            raw = data.get("symbols", data) if isinstance(data, dict) else data
        else:
            raw = _fetch_symbols(account)
        if opts["dry_run"]:
            self.stdout.write(f"broker-symbols: DRY-RUN acct#{account.id} would sync {len(raw)} symbols")
            return
        counts = upsert_broker_instruments(account, raw)
        self.stdout.write(
            "broker-symbols: acct#{a} seen={seen} created={created} updated={updated} "
            "disabled={disabled} (cache synced; no order)".format(a=account.id, **counts)
        )
