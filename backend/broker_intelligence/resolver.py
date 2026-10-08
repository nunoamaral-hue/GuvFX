"""WP3 — To:-alias → BrokerAccount resolver (read-only).

Maps an inbound message's recipient addresses to the ``BrokerEmailAlias`` (and, if the alias is ACTIVE/bound, its
``TradingAccount``). This is the ONLY coupling from ingestion to the trading domain and it is strictly READ-ONLY —
a lookup, never a write, never an order. An unknown/unmatched address → (None, None); a PENDING (Journey-B, unbound)
or RETIRED alias resolves the alias but NOT an account (so evidence is still captured + attributed, but a removed/
unbound account is never treated as live).
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

from .models import BrokerEmailAlias


def _local_part(address: str) -> str:
    """The opaque local-part of an email address, lower-cased and stripped of any display-name/angle brackets."""
    a = (address or "").strip().strip("<>").strip()
    if "@" not in a:
        return ""
    return a.rsplit("@", 1)[0].strip().lower()


def resolve_alias(to_addresses: Iterable[str]) -> Optional[BrokerEmailAlias]:
    """The first address whose opaque local-part matches a ``BrokerEmailAlias.alias_local`` (globally unique),
    or None. Match is on the opaque local-part only — never the display name. Read-only."""
    for addr in to_addresses or ():
        lp = _local_part(addr)
        if not lp:
            continue
        alias = BrokerEmailAlias.objects.filter(alias_local=lp).first()
        if alias is not None:
            return alias
    return None


def resolve_account(alias: Optional[BrokerEmailAlias]):
    """The bound ``TradingAccount`` for an alias, but ONLY when the alias is ACTIVE and bound. A PENDING (unbound)
    or RETIRED (tombstoned) alias returns None — evidence is still attributed to the alias, but no live account is
    implied. Read-only."""
    if alias is None:
        return None
    if alias.status != BrokerEmailAlias.Status.ACTIVE:
        return None
    return alias.trading_account


def resolve(to_addresses: Iterable[str]) -> Tuple[Optional[BrokerEmailAlias], object]:
    """Convenience: (alias, account) in one read-only call."""
    alias = resolve_alias(to_addresses)
    return alias, resolve_account(alias)
