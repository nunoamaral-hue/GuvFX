"""trade_copier.symbols — follower symbol resolution (packet §8).

Resolve the destination symbol for a follower given the master's symbol:

  * SAME broker (same broker key) → identity (the same symbol name).
  * CROSS broker → require a VERIFIED ``CopierSymbolMap`` row; absent one, fail CLOSED. We NEVER
    select a destination symbol merely because its name resembles the master's (packet §8).

Whether the resolved symbol is actually tradeable on the follower (broker instrument sync,
freeze/stop levels, filling mode) is confirmed at EXECUTION time; this resolver only picks the
authorized destination symbol.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolResolution:
    ok: bool
    follower_symbol: str
    reason: str
    same_broker: bool


def broker_key(account) -> str:
    """A stable broker identifier for an account: the bound broker server name, else broker_name."""
    bs = getattr(account, "broker_server", None)
    name = getattr(bs, "server_name", "") if bs is not None else ""
    if name:
        return str(name)
    return str(getattr(account, "broker_name", "") or "")


def resolve_follower_symbol(master_account, master_symbol: str, follower_account) -> SymbolResolution:
    mkey = broker_key(master_account)
    fkey = broker_key(follower_account)
    sym = str(master_symbol or "")
    if not mkey or not fkey or not sym:
        return SymbolResolution(False, "", "broker_or_symbol_key_missing", False)

    if mkey.casefold() == fkey.casefold():
        return SymbolResolution(True, sym, "same_broker_identity", True)

    # Cross-broker: a verified equivalence row is REQUIRED (fail-closed otherwise).
    from trade_copier.models import CopierSymbolMap
    row = (CopierSymbolMap.objects
           .filter(master_broker__iexact=mkey, master_symbol__iexact=sym,
                   follower_broker__iexact=fkey, verified=True)
           .first())
    if row is None:
        return SymbolResolution(False, "", "symbol_map_unverified", False)
    return SymbolResolution(True, row.follower_symbol, "verified_map", False)
