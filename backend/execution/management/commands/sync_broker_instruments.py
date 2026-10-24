"""
GFX-PKT-BROKER-SYMBOL-REGISTRY — populate an account's BrokerInstrument cache from ITS OWN broker terminal.

The authoritative source of an account's tradeable symbols is its MT5 terminal, exposed by the bridge
``GET /mt5/symbols`` (``mt5.symbols_get()``). This command resolves the account's OWN per-tenant,
identity-verified bridge, confirms the bridge-reported identity matches the requested account, fetches that
list, and upserts ``execution.BrokerInstrument`` rows so the symbol registry can resolve provider symbols
against what the broker actually offers. Read-only w.r.t. trading: it places NO order, only syncs the cache.

P0 DATA-ISOLATION (fixed): the destination is the account's OWN ``HostedExecutionEndpoint`` bridge
(``resolve_account_snapshot_base``) and the read is gated on the bridge's OBSERVED session identity
(``verify_snapshot_identity``) — NEVER the module-global agent, which co-resident is a sibling tenant's
bridge. A mismatch/unresolved/empty/timed-out read writes ZERO rows and leaves the existing cache untouched.

Usage::

    python manage.py sync_broker_instruments --account 46                # fetch from the account's own bridge
    python manage.py sync_broker_instruments --account 46 --dry-run
    python manage.py sync_broker_instruments --account 1 --from-json syms.json   # offline seed (identity skipped)

The network fetch lives here (NOT in execution.broker_symbols, which stays network-free); the pure upsert is
``upsert_broker_instruments`` so it can be unit-tested without a live bridge.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from execution.broker_symbols import base_symbol
from execution.models import BrokerInstrument
from execution.snapshot_transport import (
    account_windows_username, resolve_account_snapshot_base, verify_snapshot_identity,
)
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


def upsert_broker_instruments(account, raw_symbols, *, provenance=None) -> dict:
    """Upsert BrokerInstrument rows for ``account`` from ``raw_symbols`` (list of dicts with at least ``name``).

    EXACT broker_symbol case is preserved — a broker suffix like ``.std`` is authoritative and must never be
    upper-cased or collapsed (``XAUUSD`` and ``XAUUSD.std`` stay distinct); ``base_symbol`` is the
    suffix-stripped base used for provider matching. The whole refresh (upserts + stale-disable) is ATOMIC, so
    a partial failure rolls back entirely rather than leaving a half-updated cache. ``provenance`` (source
    identity + capture time) is recorded on each row's metadata for audit/freshness. Pure DB — no network, no
    order. Symbols not in the new set are marked ``enabled=False`` (kept for audit), never deleted.
    """
    counts = {"seen": 0, "created": 0, "updated": 0, "disabled": 0}
    seen_names = set()
    prov = dict(provenance or {})
    with transaction.atomic():
        for sym in raw_symbols or []:
            name = str(sym.get("name") or "").strip()      # EXACT broker symbol (authoritative; case preserved)
            if not name:
                continue
            counts["seen"] += 1
            seen_names.add(name)
            meta = {k: sym.get(k) for k in (
                "digits", "trade_mode", "contract_size", "tick_size", "tick_value", "point", "path",
                "currency_base", "currency_profit", "currency_margin", "volume_min", "volume_step", "volume_max",
            ) if k in sym}
            if prov:
                meta["_provenance"] = prov                  # source identity + capture time (audit/freshness)
            _obj, created = BrokerInstrument.objects.update_or_create(
                account=account, broker_symbol=name,
                defaults={"base_symbol": base_symbol(name), "enabled": _tradeable(sym), "metadata": meta},
            )
            counts["created" if created else "updated"] += 1
        # Symbols no longer offered by the broker -> disabled (fail-closed), preserved for audit.
        stale = BrokerInstrument.objects.filter(account=account, enabled=True).exclude(broker_symbol__in=seen_names)
        counts["disabled"] = stale.update(enabled=False)
    return counts


def _agent_token() -> str:
    return os.getenv("GUVFX_WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or ""


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": token})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads((resp.read() or b"{}").decode() or "{}")


def _fetch_symbols(account):
    """Fetch the broker symbol list from the account's OWN identity-verified per-tenant bridge.

    Returns ``(symbols, provenance)``. Raises ``CommandError`` (leaving any existing cache untouched) on an
    unresolved transport, an identity mismatch, or a bridge error/timeout — a failed sync never opens a symbol
    and never stores another terminal's data under this account.
    """
    token = _agent_token()
    if not token:
        raise CommandError("GUVFX_WINDOWS_AGENT_TOKEN not set. Cache left unchanged.")
    global_base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or "").rstrip("/")
    st = resolve_account_snapshot_base(account, global_base_url=global_base)
    if not st.ok:
        raise CommandError(
            f"per-tenant bridge unresolved for account {account.id}: {st.reason_code}. Cache left unchanged.")
    base = st.base_url
    wu = account_windows_username(account)

    # 1) IDENTITY FIREWALL FIRST — confirm the bridge serves THIS account before trusting any symbols.
    try:
        adata = _get_json(f"{base}/mt5/snapshots/account?username={urllib.parse.quote(wu)}", token)
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise CommandError(
            f"identity read failed for account {account.id}: {exc}. Cache left unchanged.") from exc
    inner = adata.get("data") if isinstance(adata.get("data"), dict) else {}
    obs_login = (adata.get("account_login") or inner.get("account_login")
                 or inner.get("login") or adata.get("login"))
    obs_server = (adata.get("account_server") or inner.get("account_server")
                  or inner.get("server") or adata.get("server"))
    obs_trade_mode = (adata.get("trade_mode") if adata.get("trade_mode") is not None else inner.get("trade_mode"))
    legacy = not st.per_tenant   # endpoint-less legacy global agent must additionally match the server name
    idc = verify_snapshot_identity(account, obs_login, obs_server, require_server=legacy,
                                   observed_trade_mode=obs_trade_mode, require_environment=True)
    if not idc.ok or (legacy and not str(obs_server or "").strip()):
        raise CommandError(
            f"bridge identity firewall refused for account {account.id}: {idc.reason_code}. Zero rows written.")

    # 2) fetch symbols from the SAME identity-verified base (never the global agent).
    try:
        payload = _get_json(f"{base}/mt5/symbols", token)
    except urllib.error.HTTPError as exc:
        raise CommandError(
            f"bridge GET /mt5/symbols failed for account {account.id}: HTTP {exc.code}. Cache left unchanged."
        ) from exc
    except urllib.error.URLError as exc:
        raise CommandError(
            f"bridge GET /mt5/symbols unreachable for account {account.id}: {exc.reason}. Cache left unchanged."
        ) from exc
    symbols = payload.get("symbols", payload if isinstance(payload, list) else [])
    provenance = {
        "source_login_tail": ("***" + str(obs_login)[-4:]) if obs_login else "",
        "source_server": obs_server or "",
        "per_tenant": bool(st.per_tenant),
        "observed_at": timezone.now().isoformat(),
    }
    return symbols, provenance


class Command(BaseCommand):
    help = "Sync an account's BrokerInstrument cache from ITS OWN identity-verified bridge (GET /mt5/symbols)."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, required=True)
        parser.add_argument("--from-json", type=str, default=None,
                            help="Read the symbols list from a JSON file instead of the bridge (identity skipped).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        try:
            account = TradingAccount.objects.get(pk=opts["account"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"account {opts['account']} not found")
        provenance = None
        if opts["from_json"]:
            with open(opts["from_json"]) as fh:
                data = json.load(fh)
            raw = data.get("symbols", data) if isinstance(data, dict) else data
            provenance = {"source": "from_json", "observed_at": timezone.now().isoformat()}
        else:
            raw, provenance = _fetch_symbols(account)
        if opts["dry_run"]:
            self.stdout.write(f"broker-symbols: DRY-RUN acct#{account.id} would sync {len(raw)} symbols")
            return
        # Never replace a previously valid cache with an empty response (fail-closed; cache left unchanged).
        if not raw:
            raise CommandError(
                f"broker-symbols: acct#{account.id} empty symbol set — refusing to erase a valid cache.")
        counts = upsert_broker_instruments(account, raw, provenance=provenance)
        self.stdout.write(
            "broker-symbols: acct#{a} seen={seen} created={created} updated={updated} "
            "disabled={disabled} (identity-verified; cache synced; no order)".format(a=account.id, **counts)
        )
