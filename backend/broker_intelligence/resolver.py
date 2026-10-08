"""WP3 — To:-alias → BrokerAccount resolver (read-only).

Maps an inbound message's recipient addresses to the ``BrokerEmailAlias`` (and, if the alias is ACTIVE/bound, its
``TradingAccount``). This is the ONLY coupling from ingestion to the trading domain and it is strictly READ-ONLY —
a lookup, never a write, never an order. An unknown/unmatched address → (None, None); a PENDING (Journey-B, unbound)
or RETIRED alias resolves the alias but NOT an account (so evidence is still captured + attributed, but a removed/
unbound account is never treated as live).
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

from .models import BrokerEmailAlias, BrokerEmailIdentity


def _local_part(address: str) -> str:
    """The opaque local-part of an email address, lower-cased and stripped of any display-name/angle brackets."""
    a = _bare_address(address)
    if "@" not in a:
        return ""
    return a.rsplit("@", 1)[0].strip().lower()


def _bare_address(address: str) -> str:
    """The bare ``local@domain`` from a header value, dropping any display name / angle brackets. Lower-cased."""
    a = (address or "").strip().lower()
    if "<" in a and ">" in a:                      # 'Name <local@domain>' -> 'local@domain'
        a = a[a.rfind("<") + 1:a.rfind(">")].strip()
    return a.strip("<>").strip()


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


def identities_for(to_addresses: Iterable[str]):
    """ALL ``BrokerEmailIdentity`` rows matching any of the given bare addresses (case-insensitive), de-duplicated.
    One email legitimately maps to MANY rows (unique is per email+broker), so this returns a list, never a single
    pk-ordered pick. Read-only."""
    out, seen_addr, seen_pk = [], set(), set()
    for addr in to_addresses or ():
        bare = _bare_address(addr)
        if not bare or bare in seen_addr:
            continue
        seen_addr.add(bare)
        for identity in BrokerEmailIdentity.objects.filter(email__iexact=bare):
            if identity.pk not in seen_pk:
                seen_pk.add(identity.pk)
                out.append(identity)
    return out


def resolve_identity(to_addresses: Iterable[str]) -> Optional[BrokerEmailIdentity]:
    """A single matching identity for attribution/display (the first match). NOT used for account resolution — that
    is broker-blind and unsafe for a shared address; use :func:`resolve` which fails closed on ambiguity."""
    matches = identities_for(to_addresses)
    return matches[0] if matches else None


def resolve(to_addresses: Iterable[str]) -> Tuple[Optional[BrokerEmailAlias], object]:
    """Read-only resolution to (alias-or-None, account-or-None). The GuvFX opaque alias is tried first (ACTIVE ⇒ its
    account). Otherwise the account is resolved from broker-registration identities, FAIL-CLOSED on ambiguity:
      * gather the distinct accounts carried by the VERIFIED identities for these addresses;
      * EXACTLY ONE distinct verified account ⇒ resolve it;
      * ZERO ⇒ no account (nothing verified-and-bound yet);
      * MORE THAN ONE ⇒ UNRESOLVED → no account (never guess, never pick by pk, no cross-user leak).
    Resolution is deliberately broker-BLIND-SAFE: it never relies on row order and never lets a PENDING/other-broker
    row for the same address shadow or mis-select a verified one. A PENDING/UNRESOLVED/RETIRED identity and a
    PENDING/RETIRED alias both yield no account (routing/ownership must be verified first, §4)."""
    alias = resolve_alias(to_addresses)
    if alias is not None:
        return alias, resolve_account(alias)
    verified = [i for i in identities_for(to_addresses)
                if i.status == BrokerEmailIdentity.Status.VERIFIED and i.trading_account_id is not None]
    distinct_accounts = {i.trading_account_id for i in verified}
    if len(distinct_accounts) == 1:
        return None, verified[0].trading_account        # the unique verified account — safe
    return None, None                                    # 0 ⇒ none; >1 ⇒ ambiguous, fail closed
