"""Execution-free administrative command: ensure ONE MT5 symbol is visible in an account's OWN terminal.

Companion to ``sync_broker_instruments`` (ADR-0063). It makes a single Market Watch symbol visible on the
account's OWN identity-verified per-tenant terminal so a later account-scoped catalogue sync can capture it.
It is strictly administrative:

  * places / checks / modifies / closes NO order; creates NO ExecutionJob, Trade or copier record;
  * works while every Copy Trading execution flag is OFF (it imports nothing from the execution order path);
  * resolves ONLY the account's OWN bridge (``resolve_account_snapshot_base`` — never a global/sibling bridge);
  * proves the observed MT5 identity is THIS account's (``verify_snapshot_identity``) BEFORE and AFTER the op;
  * is dry-run by DEFAULT — the mutating call is sent only with ``--commit``;
  * fails closed on every ambiguity and PROVES the postcondition (``visible=true``) before claiming success;
  * emits a concise, secret-free audit line (correlation id, masked login, server, symbol, before/after).

Usage::

    python manage.py ensure_symbol_visible --account 50 --symbol XAUUSD.std            # dry-run (default)
    python manage.py ensure_symbol_visible --account 50 --symbol XAUUSD.std --commit   # perform the reveal

The network fetch/POST lives here; identity resolution reuses the shared ``snapshot_transport`` primitives the
read paths already use. No order path is imported — see ``tests_ensure_symbol_visible_command`` negative control.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.core.management.base import BaseCommand, CommandError

from execution.snapshot_transport import (
    account_windows_username, resolve_account_snapshot_base, verify_snapshot_identity,
)
from trading.models import TradingAccount

logger = logging.getLogger("execution.ensure_symbol_visible")

_TIMEOUT = int(os.getenv("BROKER_SYMBOLS_SYNC_TIMEOUT_SECONDS", "15") or 15)
# A single explicit broker symbol only — reject blank/whitespace/wildcard/list/bulk at the boundary.
_SYMBOL_DISALLOWED_CHARS = set("*?,;%\t\r\n ")


def _agent_token() -> str:
    return os.getenv("GUVFX_WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_AGENT_TOKEN") or ""


def _mask(login) -> str:
    s = str(login or "")
    return ("***" + s[-4:]) if len(s) > 4 else ("***" if s else "")


def _get_json(url: str, token: str) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"X-GuvFX-Agent-Token": token})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads((resp.read() or b"{}").decode() or "{}")


def _post_json(url: str, token: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"X-GuvFX-Agent-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads((resp.read() or b"{}").decode() or "{}")


def _resolve_and_verify(account, token: str, *, stage: str):
    """Resolve the account's OWN bridge and PROVE the observed MT5 identity is this account's.

    Returns ``(base_url, observed_dict)`` or raises ``CommandError`` (fail-closed). NEVER resolves a global or
    sibling bridge: a hosted account with no READY endpoint fails closed. Mirrors the identity firewall used by
    ``sync_broker_instruments._fetch_symbols`` but WITHOUT fetching the symbol list.
    """
    global_base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or "").rstrip("/")
    st = resolve_account_snapshot_base(account, global_base_url=global_base)
    if not st.ok:
        raise CommandError(f"[{stage}] per-tenant bridge unresolved for account {account.id}: {st.reason_code}")
    wu = account_windows_username(account)
    try:
        adata = _get_json(f"{st.base_url}/mt5/snapshots/account?username={urllib.parse.quote(wu)}", token)
    except (urllib.error.URLError, OSError, ValueError) as exc:  # unreachable, timeout, malformed JSON
        raise CommandError(f"[{stage}] identity read failed for account {account.id}: {exc}") from exc
    inner = adata.get("data") if isinstance(adata.get("data"), dict) else {}
    obs_login = (adata.get("account_login") or inner.get("account_login")
                 or inner.get("login") or adata.get("login"))
    obs_server = (adata.get("account_server") or inner.get("account_server")
                  or inner.get("server") or adata.get("server"))
    obs_tmode = adata.get("trade_mode") if adata.get("trade_mode") is not None else inner.get("trade_mode")
    legacy = not st.per_tenant
    # Stricter than the sync read: this administrative op pins login AND server (require_server=True). The
    # verify_snapshot_identity server branch is skipped only when a side is genuinely unknown, so this never
    # over-rejects a hosted account whose server is a placeholder while still refusing a real server mismatch.
    idc = verify_snapshot_identity(account, obs_login, obs_server, require_server=True,
                                   observed_trade_mode=obs_tmode, require_environment=True)
    if not idc.ok or (legacy and not str(obs_server or "").strip()):
        raise CommandError(f"[{stage}] bridge identity firewall refused for account {account.id}: {idc.reason_code}")
    return st.base_url, {"login": obs_login, "server": obs_server, "trade_mode": obs_tmode}


class Command(BaseCommand):
    help = ("Ensure ONE MT5 symbol is visible in an account's OWN identity-verified terminal "
            "(execution-free; dry-run by default; --commit to perform).")

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, required=True)
        parser.add_argument("--symbol", type=str, required=True)
        parser.add_argument("--commit", action="store_true",
                            help="Perform the reveal. Default is dry-run (identity + current visibility only).")

    def handle(self, *args, **opts):
        cid = uuid.uuid4().hex[:12]
        symbol = str(opts["symbol"] or "").strip()
        if (not symbol) or symbol == "*" or any(ch in _SYMBOL_DISALLOWED_CHARS for ch in symbol):
            raise CommandError("invalid --symbol: pass exactly one broker symbol (no blank/whitespace/wildcard/list)")
        token = _agent_token()
        if not token:
            raise CommandError("GUVFX_WINDOWS_AGENT_TOKEN not set. Refusing (fail-closed).")
        try:
            account = TradingAccount.objects.get(pk=opts["account"])
        except TradingAccount.DoesNotExist:
            raise CommandError(f"account {opts['account']} not found")
        # Ambiguous identity -> refuse. NOTE (ADR-0063): ``is_active`` is execution-AUTHORISATION state, not
        # connectivity/validity — legitimate live-monitoring accounts (e.g. #50) run is_active=False — so it is
        # deliberately NOT a gate here. The load-bearing protection is the identity firewall below
        # (observed login == account_number, plus server), which refuses a wrong/stale/foreign terminal.
        if not str(getattr(account, "account_number", "") or "").strip():
            raise CommandError(f"account {account.id} has no broker account_number — ambiguous identity; refusing")

        base, before_obs = _resolve_and_verify(account, token, stage="pre")

        # Current visibility (read-only) via the account's OWN bridge.
        try:
            syms = _get_json(f"{base}/mt5/symbols", token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CommandError(f"symbol read failed for account {account.id}: {exc}") from exc
        rows = syms.get("symbols", []) if isinstance(syms, dict) else (syms if isinstance(syms, list) else [])
        match = next((s for s in rows if str(s.get("name") or "") == symbol), None)
        if match is None:
            raise CommandError(f"symbol {symbol!r} not present on account {account.id}'s terminal — fail-closed")
        before_visible = bool(match.get("visible"))
        audit = {"cid": cid, "account": account.id, "login": _mask(before_obs.get("login")),
                 "server": before_obs.get("server"), "symbol": symbol,
                 "before_visible": before_visible, "trade_mode": match.get("trade_mode")}

        if not opts["commit"]:
            logger.info("ensure_symbol_visible DRY-RUN %s", audit)
            self.stdout.write(json.dumps({"mode": "dry-run", "would_change": not before_visible, **audit}))
            return

        # COMMIT: ask the account's OWN bridge to reveal exactly this one symbol.
        try:
            res = _post_json(f"{base}/mt5/ensure-symbol-visible", token, {"symbol": symbol})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CommandError(f"ensure-symbol-visible call failed for account {account.id}: {exc}") from exc
        if not res.get("ok"):
            raise CommandError(
                f"bridge refused ensure_symbol_visible for account {account.id}: {res.get('error')}")
        # Prove identity AGAIN (drift guard) before trusting any post-state.
        _base_after, after_obs = _resolve_and_verify(account, token, stage="post")
        # PROVE the postcondition INDEPENDENTLY: re-read the account's OWN symbols and confirm the exact symbol
        # is now visible, rather than trusting only the mutating call's self-reported ``after_visible``.
        try:
            syms2 = _get_json(f"{base}/mt5/symbols", token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise CommandError(f"post-commit symbol read failed for account {account.id}: {exc}") from exc
        rows2 = syms2.get("symbols", []) if isinstance(syms2, dict) else (syms2 if isinstance(syms2, list) else [])
        match2 = next((s for s in rows2 if str(s.get("name") or "") == symbol), None)
        if match2 is None or not bool(match2.get("visible")):
            raise CommandError(
                f"postcondition not proven: {symbol} not visible on account {account.id} after commit — fail-closed")
        audit.update({"changed": bool(res.get("changed")), "after_visible": True,
                      "post_login": _mask(after_obs.get("login"))})
        logger.info("ensure_symbol_visible COMMIT %s", audit)
        self.stdout.write(json.dumps({"mode": "commit", **audit}))
